"""download 用例：按 Artifact ID 下载固件，校验后原子归档（接口设计 §8）。

数据流（单个 Artifact）：
  1. 事务①：查 ArtifactContext，创建 DownloadRecord（同一 Artifact 已有
     活动任务时 create_download 抛 ActiveDownloadExistsError，AC-30）
  2. 事务②：记录置 downloading，临时路径 = tmp/downloads/{记录ID}.part
  3. Downloader 流式下载到临时文件（进度回调节流写库）
  4. 失败且为 403/404/410 且从未刷新过 → adapter.refresh_artifact_url
     最多一次（AC-29）；刷新成功则落库新地址并重试下载
  5. 下载成功 → 官方校验和比对：有则必比，不符不归档（AC-26 ~ AC-28）；
     无官方校验和记 not_available，允许归档
  6. 校验通过 → ArtifactStore.promote 原子归档 → 记录置 completed；
     任何失败路径都清理临时文件，绝不产生正常归档记录
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from firmatlas.adapters.events import (
    ArtifactRefreshRequest,
    ArtifactRefreshResult,
    ArtifactUrlRefreshed,
)
from firmatlas.app.ports import UnitOfWorkFactory
from firmatlas.domain.errors import FirmAtlasError
from firmatlas.domain.model import (
    ArtifactContext,
    DownloadErrorCode,
    DownloadFailed,
    DownloadOutcome,
    DownloadPatch,
    DownloadStatus,
    DownloadSucceeded,
    VerificationStatus,
)
from firmatlas.domain.timeutil import utc_now

# 触发地址刷新的错误码（需求分析 0x0D：失效地址）
_REFRESHABLE_CODES = frozenset(
    {DownloadErrorCode.HTTP_403, DownloadErrorCode.HTTP_404, DownloadErrorCode.HTTP_410}
)

# 进度落库的最小间隔（字节）：Downloader 回调间隔为 256 KiB，
# 再在用例层放大到 4 MiB，避免大文件下载时高频写 SQLite
_PROGRESS_DB_THRESHOLD = 4 * 1024 * 1024

# 大小校验容差（字节）：默认来源只需要覆盖 KB 粒度误差；Omada API 的 MB
# 使用 1000 KiB 且只保留两位小数，理论舍入误差接近 5 KiB，单独放宽到 8 KiB。
_DEFAULT_SIZE_TOLERANCE_BYTES = 1024
_SIZE_TOLERANCE_BY_SOURCE = {"omada-global": 8 * 1024}


class UnknownArtifactError(FirmAtlasError):
    """artifact_id 在目录中不存在。"""


class DownloaderPort(Protocol):
    """用例对下载器的最小要求（infra.downloader.Downloader 满足此签名）。"""

    async def download(
        self,
        *,
        url: str,
        dest: Path,
        expected_size: int | None = None,
        on_progress: Callable[[int], None] | None = None,
        referer: str | None = None,
        size_tolerance: int = 0,
    ) -> DownloadOutcome: ...


class ArtifactStorePort(Protocol):
    """用例对归档器的最小要求（infra.artifact_store.ArtifactStore 满足此签名）。"""

    def build_final_relative_path(
        self, ctx: ArtifactContext, original_filename: str | None
    ) -> PurePosixPath: ...

    def promote(self, *, tmp_path: Path, final_relative_path: PurePosixPath) -> Path: ...


class RefreshingAdapter(Protocol):
    """download 用例对适配器的最小要求（接口设计 §4.2）。"""

    async def refresh_artifact_url(
        self, request: ArtifactRefreshRequest
    ) -> ArtifactRefreshResult: ...


@dataclass(frozen=True)
class DownloadReport:
    """download 用例的返回值，供 CLI 打印。"""

    artifact_id: str
    download_id: str
    status: DownloadStatus
    verification_status: VerificationStatus
    final_relative_path: str | None    # 相对 data 目录，如 firmware/tp-link/CN/...
    bytes_received: int
    sha256: str | None
    url_refreshed: bool
    error_code: str | None
    error_message: str | None


async def download_artifact(
    *,
    artifact_id: str,
    uow_factory: UnitOfWorkFactory,
    downloader: DownloaderPort,
    store: ArtifactStorePort,
    data_dir: Path,
    adapter: RefreshingAdapter | None = None,
) -> DownloadReport:
    """下载单个 Artifact。

    抛出：
    - UnknownArtifactError：artifact_id 不在目录中
    - ActiveDownloadExistsError：同一 Artifact 已有活动下载（由仓库抛出，AC-30）
    """
    # --- 事务①：查上下文 + 创建下载记录 --------------------------------
    with uow_factory.begin() as uow:
        ctx = uow.catalog.get_artifact_context(artifact_id)
        if ctx is None:
            raise UnknownArtifactError(f"Artifact {artifact_id!r} 不存在，请先执行 firmatlas crawl")
        record = uow.downloads.create_download(artifact_id=artifact_id, requested_at=utc_now())

    tmp_rel = PurePosixPath("tmp/downloads") / f"{record.id}.part"
    tmp_path = data_dir / tmp_rel

    # --- 事务②：置 downloading ------------------------------------------
    url = ctx.artifact.download_url
    with uow_factory.begin() as uow:
        uow.downloads.transition(
            download_id=record.id,
            patch=DownloadPatch(
                status=DownloadStatus.DOWNLOADING,
                started_at=utc_now(),
                resolved_url=url,
                temporary_relative_path=str(tmp_rel),
                attempt_count=1,
            ),
        )

    # --- 下载（进度节流落库）---------------------------------------------
    # Referer 用来源站点根地址：部分厂商下载服务器校验 Referer，缺失即 403
    referer = ctx.source.base_url
    size_tolerance = _SIZE_TOLERANCE_BY_SOURCE.get(
        ctx.source.source_key, _DEFAULT_SIZE_TOLERANCE_BYTES
    )
    progress = _ProgressWriter(uow_factory=uow_factory, download_id=record.id)
    outcome = await downloader.download(
        url=url,
        dest=tmp_path,
        expected_size=ctx.artifact.advertised_size,
        on_progress=progress,
        referer=referer,
        size_tolerance=size_tolerance,
    )

    # --- 失效地址刷新：最多一次（AC-29）----------------------------------
    url_refreshed = False
    refresh_note: str | None = None
    if (
        isinstance(outcome, DownloadFailed)
        and outcome.error_code in _REFRESHABLE_CODES
        and adapter is not None
    ):
        refresh_result = await adapter.refresh_artifact_url(_build_refresh_request(ctx, url))
        if isinstance(refresh_result, ArtifactUrlRefreshed):
            url_refreshed = True
            url = refresh_result.download_url
            with uow_factory.begin() as uow:
                # 只更新地址字段，绝不改变 Artifact 身份（source_key）
                uow.catalog.update_artifact_url(
                    artifact_id=artifact_id,
                    download_url=url,
                    url_expires_at=refresh_result.url_expires_at,
                    resolved_at=utc_now(),
                )
                uow.downloads.transition(
                    download_id=record.id,
                    patch=DownloadPatch(
                        status=DownloadStatus.DOWNLOADING,
                        resolved_url=url,
                        url_refresh_count=1,
                        attempt_count=2,
                    ),
                )
            outcome = await downloader.download(
                url=url,
                dest=tmp_path,
                expected_size=ctx.artifact.advertised_size,
                on_progress=progress,
                referer=referer,
                size_tolerance=size_tolerance,
            )
        else:
            refresh_note = f"地址刷新失败（{refresh_result.reason_code}）: {refresh_result.detail}"

    # --- 失败收尾：清理临时文件，置 failed/interrupted --------------------
    if isinstance(outcome, DownloadFailed):
        tmp_path.unlink(missing_ok=True)
        status = (
            DownloadStatus.INTERRUPTED
            if outcome.error_code is DownloadErrorCode.INTERRUPTED
            else DownloadStatus.FAILED
        )
        message = outcome.detail if refresh_note is None else f"{outcome.detail}；{refresh_note}"
        with uow_factory.begin() as uow:
            uow.downloads.transition(
                download_id=record.id,
                patch=DownloadPatch(
                    status=status,
                    finished_at=utc_now(),
                    bytes_received=outcome.bytes_received,
                    error_code=str(outcome.error_code),
                    error_message=message,
                ),
            )
        return DownloadReport(
            artifact_id=artifact_id,
            download_id=record.id,
            status=status,
            verification_status=VerificationStatus.NOT_CHECKED,
            final_relative_path=None,
            bytes_received=outcome.bytes_received,
            sha256=None,
            url_refreshed=url_refreshed,
            error_code=str(outcome.error_code),
            error_message=message,
        )

    # --- 校验：有官方校验和必比，不符不归档（AC-26 ~ AC-28）----------------
    assert isinstance(outcome, DownloadSucceeded)
    verification = _verify_checksum(ctx, outcome.sha256, tmp_path)
    if verification is VerificationStatus.MISMATCH:
        tmp_path.unlink(missing_ok=True)
        checksum = ctx.artifact.official_checksum
        assert checksum is not None
        message = f"官方校验和不符（{checksum.algorithm}），文件未归档"
        with uow_factory.begin() as uow:
            uow.downloads.transition(
                download_id=record.id,
                patch=DownloadPatch(
                    status=DownloadStatus.FAILED,
                    verification_status=VerificationStatus.MISMATCH,
                    finished_at=utc_now(),
                    bytes_received=outcome.bytes_received,
                    sha256=outcome.sha256,
                    error_code="checksum_mismatch",
                    error_message=message,
                ),
            )
        return DownloadReport(
            artifact_id=artifact_id,
            download_id=record.id,
            status=DownloadStatus.FAILED,
            verification_status=VerificationStatus.MISMATCH,
            final_relative_path=None,
            bytes_received=outcome.bytes_received,
            sha256=outcome.sha256,
            url_refreshed=url_refreshed,
            error_code="checksum_mismatch",
            error_message=message,
        )

    # --- 归档：原子移动 + 置 completed ------------------------------------
    final_rel = store.build_final_relative_path(ctx, ctx.artifact.original_filename)
    store.promote(tmp_path=tmp_path, final_relative_path=final_rel)
    final_rel_str = str(PurePosixPath("firmware") / final_rel)
    with uow_factory.begin() as uow:
        uow.downloads.transition(
            download_id=record.id,
            patch=DownloadPatch(
                status=DownloadStatus.COMPLETED,
                verification_status=verification,
                finished_at=utc_now(),
                final_relative_path=final_rel_str,
                bytes_received=outcome.bytes_received,
                size_bytes=outcome.bytes_received,
                sha256=outcome.sha256,
                http_etag=outcome.etag,
                http_last_modified=outcome.last_modified,
            ),
        )
    return DownloadReport(
        artifact_id=artifact_id,
        download_id=record.id,
        status=DownloadStatus.COMPLETED,
        verification_status=verification,
        final_relative_path=final_rel_str,
        bytes_received=outcome.bytes_received,
        sha256=outcome.sha256,
        url_refreshed=url_refreshed,
        error_code=None,
        error_message=None,
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


class _ProgressWriter:
    """把下载进度节流写入下载记录（downloading → downloading 自变迁）。"""

    def __init__(self, *, uow_factory: UnitOfWorkFactory, download_id: str) -> None:
        self._uow_factory = uow_factory
        self._download_id = download_id
        self._last_written = 0

    def __call__(self, bytes_received: int) -> None:
        if bytes_received - self._last_written < _PROGRESS_DB_THRESHOLD:
            return
        self._last_written = bytes_received
        with self._uow_factory.begin() as uow:
            uow.downloads.transition(
                download_id=self._download_id,
                patch=DownloadPatch(
                    status=DownloadStatus.DOWNLOADING, bytes_received=bytes_received
                ),
            )


def _build_refresh_request(ctx: ArtifactContext, stale_url: str) -> ArtifactRefreshRequest:
    return ArtifactRefreshRequest(
        product_source_key=ctx.product.source_key,
        hardware_revision_source_key=ctx.hardware_revision.source_key,
        release_source_key=ctx.release.source_key,
        artifact_source_key=ctx.artifact.source_key,
        stale_url=stale_url,
        known_filename=ctx.artifact.original_filename,
        known_size=ctx.artifact.advertised_size,
        known_checksum=ctx.artifact.official_checksum,
    )


def _verify_checksum(
    ctx: ArtifactContext, sha256_hex: str, tmp_path: Path
) -> VerificationStatus:
    """比对官方校验和。

    - 无官方校验和 → not_available（允许归档）
    - 官方算法是 sha256 → 直接用下载时算好的值比对
    - 其他算法（如 md5）→ 重读临时文件计算后比对
    - 算法名不被 hashlib 支持 → 视同无可用校验和（not_available）
    """
    checksum = ctx.artifact.official_checksum
    if checksum is None:
        return VerificationStatus.NOT_AVAILABLE

    expected = checksum.value.strip().lower()
    algorithm = checksum.algorithm.strip().lower().replace("-", "")
    if algorithm == "sha256":
        actual = sha256_hex.lower()
    else:
        try:
            digest = hashlib.new(algorithm)
        except ValueError:
            return VerificationStatus.NOT_AVAILABLE
        with tmp_path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
        actual = digest.hexdigest().lower()

    return VerificationStatus.VERIFIED if actual == expected else VerificationStatus.MISMATCH
