"""download 用例测试（接口设计 §8，AC-26 ~ AC-30）。

用真 SQLite Repository + 假 Downloader / 假适配器：
- 假 Downloader 按预设脚本返回结果序列，并在"成功"时真实写出临时文件，
  使校验与归档路径走真实文件系统；
- 假适配器可编程返回刷新成功/失败。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from firmatlas.adapters.events import (
    ArtifactRefreshFailed,
    ArtifactRefreshRequest,
    ArtifactUrlRefreshed,
    RefreshFailureReason,
)
from firmatlas.app.download import DownloadReport, UnknownArtifactError, download_artifact
from firmatlas.domain.errors import ActiveDownloadExistsError
from firmatlas.domain.model import (
    DownloadErrorCode,
    DownloadFailed,
    DownloadStatus,
    DownloadSucceeded,
    OfficialChecksum,
    VerificationStatus,
)
from firmatlas.domain.timeutil import utc_now
from firmatlas.infra.artifact_store import ArtifactStore

CONTENT = b"firmware-bytes" * 100
CONTENT_SHA256 = hashlib.sha256(CONTENT).hexdigest()
CONTENT_MD5 = hashlib.md5(CONTENT).hexdigest()


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class ScriptedDownloader:
    """按脚本依次返回下载结果；结果为 DownloadSucceeded 时真实写出 dest 文件。"""

    def __init__(self, outcomes: list[DownloadSucceeded | DownloadFailed]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[str] = []  # 记录每次调用的 URL
        self.referers: list[str | None] = []  # 记录每次调用收到的 referer
        self.size_tolerances: list[int] = []  # 记录每次调用收到的 size_tolerance

    async def download(
        self, *, url, dest: Path, expected_size=None, on_progress=None, referer=None,
        size_tolerance=0,
    ):
        self.calls.append(url)
        self.referers.append(referer)
        self.size_tolerances.append(size_tolerance)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, DownloadSucceeded):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(CONTENT)
            if on_progress is not None:
                on_progress(len(CONTENT))
        return outcome


class ScriptedAdapter:
    """refresh_artifact_url 返回预设结果，并记录收到的请求。"""

    def __init__(self, result) -> None:
        self._result = result
        self.requests: list[ArtifactRefreshRequest] = []

    async def refresh_artifact_url(self, request: ArtifactRefreshRequest):
        self.requests.append(request)
        return self._result


def succeeded() -> DownloadSucceeded:
    return DownloadSucceeded(
        bytes_received=len(CONTENT),
        sha256=CONTENT_SHA256,
        etag='"tag1"',
        last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
    )


def failed_404() -> DownloadFailed:
    return DownloadFailed(
        error_code=DownloadErrorCode.HTTP_404, http_status=404,
        detail="HTTP 404", bytes_received=0,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def seeded_artifact_id(
    uow_factory, seeded_source, seeded_run, make_product_candidate
) -> str:
    """经真实 Repository 入库一棵产品子树，返回其 Artifact ID。"""
    with uow_factory.begin() as uow:
        product = make_product_candidate()
        p = uow.catalog.upsert_product(
            source_id=seeded_source.id, candidate=product,
            run_id=seeded_run.id, seen_at=utc_now(),
        )
        hw = product.hardware_revisions[0]
        h = uow.catalog.upsert_hardware_revision(
            product_id=p.entity_id, candidate=hw, run_id=seeded_run.id, seen_at=utc_now()
        )
        release = hw.releases[0]
        r = uow.catalog.upsert_release(
            hardware_revision_id=h.entity_id, candidate=release,
            run_id=seeded_run.id, seen_at=utc_now(),
        )
        a = uow.catalog.upsert_artifact(
            release_id=r.entity_id, candidate=release.artifacts[0],
            run_id=seeded_run.id, seen_at=utc_now(),
        )
        return a.entity_id


@pytest.fixture
def seeded_omada_artifact_id(
    uow_factory, make_source, make_product_candidate
) -> str:
    """入库一个 omada-global Artifact，供来源专用下载行为测试。"""
    source = make_source(
        vendor_key="omada",
        vendor_name="Omada",
        source_key="omada-global",
        name="Omada Worldwide 固件下载中心",
        region_code="WW",
        locale="en",
        base_url="https://support.omadanetworks.com/en/",
        adapter_key="omada_global",
    )
    with uow_factory.begin() as uow:
        uow.sources.ensure_seed_sources([source])
        run = uow.runs.create_run(source_id=source.id, started_at=utc_now())
        product = make_product_candidate(source_key="model-id:1402")
        saved_product = uow.catalog.upsert_product(
            source_id=source.id,
            candidate=product,
            run_id=run.id,
            seen_at=utc_now(),
        )
        revision = product.hardware_revisions[0]
        saved_revision = uow.catalog.upsert_hardware_revision(
            product_id=saved_product.entity_id,
            candidate=revision,
            run_id=run.id,
            seen_at=utc_now(),
        )
        release = revision.releases[0]
        saved_release = uow.catalog.upsert_release(
            hardware_revision_id=saved_revision.entity_id,
            candidate=release,
            run_id=run.id,
            seen_at=utc_now(),
        )
        saved_artifact = uow.catalog.upsert_artifact(
            release_id=saved_release.entity_id,
            candidate=release.artifacts[0],
            run_id=run.id,
            seen_at=utc_now(),
        )
    return saved_artifact.entity_id


@pytest.fixture
def seeded_checksum_artifact_id(
    uow_factory, seeded_source, seeded_run, make_product_candidate,
    make_artifact_candidate, make_release_candidate, make_revision_candidate,
):
    """入库带官方校验和的 Artifact，algorithm 由测试指定。"""

    def _seed(checksum: OfficialChecksum) -> str:
        artifact = make_artifact_candidate(official_checksum=checksum)
        product = make_product_candidate(
            hardware_revisions=(
                make_revision_candidate(releases=(make_release_candidate(artifacts=(artifact,)),)),
            )
        )
        with uow_factory.begin() as uow:
            p = uow.catalog.upsert_product(
                source_id=seeded_source.id, candidate=product,
                run_id=seeded_run.id, seen_at=utc_now(),
            )
            h = uow.catalog.upsert_hardware_revision(
                product_id=p.entity_id, candidate=product.hardware_revisions[0],
                run_id=seeded_run.id, seen_at=utc_now(),
            )
            r = uow.catalog.upsert_release(
                hardware_revision_id=h.entity_id,
                candidate=product.hardware_revisions[0].releases[0],
                run_id=seeded_run.id, seen_at=utc_now(),
            )
            a = uow.catalog.upsert_artifact(
                release_id=r.entity_id, candidate=artifact,
                run_id=seeded_run.id, seen_at=utc_now(),
            )
            return a.entity_id

    return _seed


def run_download(
    *, artifact_id, uow_factory, downloader, data_dir, adapter=None
) -> DownloadReport:
    store = ArtifactStore(data_dir)
    return asyncio.run(
        download_artifact(
            artifact_id=artifact_id,
            uow_factory=uow_factory,
            downloader=downloader,
            store=store,
            data_dir=data_dir,
            adapter=adapter,
        )
    )


def get_record(uow_factory, download_id):
    with uow_factory.begin() as uow:
        records = uow.downloads.list_downloads()
    return next(r for r in records if r.id == download_id)


# ---------------------------------------------------------------------------
# 成功归档
# ---------------------------------------------------------------------------


def test_success_without_official_checksum(uow_factory, seeded_artifact_id, data_dir):
    """无官方校验和：not_available，允许归档（AC-27）。"""
    downloader = ScriptedDownloader([succeeded()])

    report = run_download(
        artifact_id=seeded_artifact_id, uow_factory=uow_factory,
        downloader=downloader, data_dir=data_dir,
    )

    assert report.status is DownloadStatus.COMPLETED
    assert report.verification_status is VerificationStatus.NOT_AVAILABLE
    assert report.sha256 == CONTENT_SHA256
    assert report.final_relative_path is not None
    # 下载用例把来源站点根地址作为 Referer 传给下载器（部分厂商校验 Referer）
    assert downloader.referers == ["https://www.tp-link.com.cn/"]
    # 下载用例对 KB 粒度近似大小放宽 1 KB 容差，避免误判 size_mismatch
    assert downloader.size_tolerances == [1024]
    # 归档文件真实存在且内容一致
    final = data_dir / report.final_relative_path
    assert final.read_bytes() == CONTENT
    # 归档路径结构：firmware/厂商/地区/型号/硬件版本/固件版本/短ID__文件名
    parts = Path(report.final_relative_path).parts
    assert parts[0] == "firmware"
    assert parts[1:3] == ("tp-link", "CN")
    assert parts[6].startswith(seeded_artifact_id[:8] + "__")
    # 临时文件已清理（被移动走）
    assert not list((data_dir / "tmp" / "downloads").glob("*.part"))
    # 下载记录落库
    record = get_record(uow_factory, report.download_id)
    assert record.status is DownloadStatus.COMPLETED
    assert record.sha256 == CONTENT_SHA256
    assert record.final_relative_path == report.final_relative_path
    assert record.http_etag == '"tag1"'


def test_omada_uses_source_specific_size_tolerance(
    uow_factory,
    seeded_omada_artifact_id,
    data_dir,
) -> None:
    downloader = ScriptedDownloader([succeeded()])

    report = run_download(
        artifact_id=seeded_omada_artifact_id,
        uow_factory=uow_factory,
        downloader=downloader,
        data_dir=data_dir,
    )

    assert report.status is DownloadStatus.COMPLETED
    assert downloader.size_tolerances == [8 * 1024]


def test_success_with_matching_sha256(
    uow_factory, seeded_checksum_artifact_id, data_dir
):
    """官方 sha256 校验和一致：verified（AC-26）。"""
    artifact_id = seeded_checksum_artifact_id(OfficialChecksum("sha256", CONTENT_SHA256))
    downloader = ScriptedDownloader([succeeded()])

    report = run_download(
        artifact_id=artifact_id, uow_factory=uow_factory,
        downloader=downloader, data_dir=data_dir,
    )

    assert report.status is DownloadStatus.COMPLETED
    assert report.verification_status is VerificationStatus.VERIFIED


def test_success_with_matching_md5(uow_factory, seeded_checksum_artifact_id, data_dir):
    """官方 md5 校验和：重读临时文件计算比对。"""
    artifact_id = seeded_checksum_artifact_id(OfficialChecksum("MD5", CONTENT_MD5))
    downloader = ScriptedDownloader([succeeded()])

    report = run_download(
        artifact_id=artifact_id, uow_factory=uow_factory,
        downloader=downloader, data_dir=data_dir,
    )

    assert report.verification_status is VerificationStatus.VERIFIED


# ---------------------------------------------------------------------------
# 校验失败不归档（AC-28）
# ---------------------------------------------------------------------------


def test_checksum_mismatch_not_promoted(
    uow_factory, seeded_checksum_artifact_id, data_dir
):
    artifact_id = seeded_checksum_artifact_id(OfficialChecksum("sha256", "0" * 64))
    downloader = ScriptedDownloader([succeeded()])

    report = run_download(
        artifact_id=artifact_id, uow_factory=uow_factory,
        downloader=downloader, data_dir=data_dir,
    )

    assert report.status is DownloadStatus.FAILED
    assert report.verification_status is VerificationStatus.MISMATCH
    assert report.final_relative_path is None
    assert report.error_code == "checksum_mismatch"
    # 归档目录里没有任何文件，临时文件已删除
    assert not list((data_dir / "firmware").rglob("*"))
    assert not list((data_dir / "tmp" / "downloads").glob("*"))
    record = get_record(uow_factory, report.download_id)
    assert record.status is DownloadStatus.FAILED
    assert record.verification_status is VerificationStatus.MISMATCH


# ---------------------------------------------------------------------------
# 下载失败与中断
# ---------------------------------------------------------------------------


def test_download_failure_no_archive(uow_factory, seeded_artifact_id, data_dir):
    """HTTP 5xx 失败：failed 落库、无归档、无临时残留。"""
    downloader = ScriptedDownloader([
        DownloadFailed(
            error_code=DownloadErrorCode.HTTP_5XX, http_status=500,
            detail="HTTP 500", bytes_received=0,
        )
    ])

    report = run_download(
        artifact_id=seeded_artifact_id, uow_factory=uow_factory,
        downloader=downloader, data_dir=data_dir,
    )

    assert report.status is DownloadStatus.FAILED
    assert report.error_code == "http_5xx"
    assert not list((data_dir / "firmware").rglob("*"))
    record = get_record(uow_factory, report.download_id)
    assert record.status is DownloadStatus.FAILED
    assert record.error_code == "http_5xx"


def test_interrupted_download_marks_interrupted(uow_factory, seeded_artifact_id, data_dir):
    """中断（连接被重置等）：置 interrupted，不产生正常归档记录。"""
    downloader = ScriptedDownloader([
        DownloadFailed(
            error_code=DownloadErrorCode.INTERRUPTED, http_status=None,
            detail="下载中断: connection reset", bytes_received=1024,
        )
    ])

    report = run_download(
        artifact_id=seeded_artifact_id, uow_factory=uow_factory,
        downloader=downloader, data_dir=data_dir,
    )

    assert report.status is DownloadStatus.INTERRUPTED
    assert report.bytes_received == 1024
    assert not list((data_dir / "firmware").rglob("*"))
    record = get_record(uow_factory, report.download_id)
    assert record.status is DownloadStatus.INTERRUPTED


def test_size_mismatch_fails(uow_factory, seeded_artifact_id, data_dir):
    downloader = ScriptedDownloader([
        DownloadFailed(
            error_code=DownloadErrorCode.SIZE_MISMATCH, http_status=None,
            detail="大小不符", bytes_received=100,
        )
    ])

    report = run_download(
        artifact_id=seeded_artifact_id, uow_factory=uow_factory,
        downloader=downloader, data_dir=data_dir,
    )

    assert report.status is DownloadStatus.FAILED
    assert report.error_code == "size_mismatch"


# ---------------------------------------------------------------------------
# 地址刷新（AC-29）
# ---------------------------------------------------------------------------


def test_404_triggers_refresh_then_success(uow_factory, seeded_artifact_id, data_dir):
    """404 → 刷新成功 → 用新地址重试 → 归档；只刷新一次。"""
    downloader = ScriptedDownloader([failed_404(), succeeded()])
    adapter = ScriptedAdapter(
        ArtifactUrlRefreshed(download_url="https://example.com/fw/new-url.zip", url_expires_at=None)
    )

    report = run_download(
        artifact_id=seeded_artifact_id, uow_factory=uow_factory,
        downloader=downloader, data_dir=data_dir, adapter=adapter,
    )

    assert report.status is DownloadStatus.COMPLETED
    assert report.url_refreshed is True
    # 第二次下载用了新地址
    assert downloader.calls == [
        "https://example.com/fw/TL-WR841N_v14.zip",
        "https://example.com/fw/new-url.zip",
    ]
    # 两次下载（原始 + 刷新重试）都带上了来源站点 Referer
    assert downloader.referers == [
        "https://www.tp-link.com.cn/",
        "https://www.tp-link.com.cn/",
    ]
    assert downloader.size_tolerances == [1024, 1024]
    # 刷新请求带上了 Artifact 身份
    assert len(adapter.requests) == 1
    assert adapter.requests[0].artifact_source_key == "artifact-1"
    # 新地址落库到 artifacts 表；source_key 不变（AC-29）
    with uow_factory.begin() as uow:
        ctx = uow.catalog.get_artifact_context(seeded_artifact_id)
    assert ctx.artifact.download_url == "https://example.com/fw/new-url.zip"
    assert ctx.artifact.source_key == "artifact-1"
    # 下载记录反映刷新
    record = get_record(uow_factory, report.download_id)
    assert record.url_refresh_count == 1
    assert record.attempt_count == 2
    assert record.resolved_url == "https://example.com/fw/new-url.zip"


def test_omada_refresh_retry_keeps_source_specific_size_tolerance(
    uow_factory,
    seeded_omada_artifact_id,
    data_dir,
) -> None:
    downloader = ScriptedDownloader([failed_404(), succeeded()])
    adapter = ScriptedAdapter(
        ArtifactUrlRefreshed(
            download_url="https://static.tp-link.com/example/refreshed.zip",
            url_expires_at=None,
        )
    )

    report = run_download(
        artifact_id=seeded_omada_artifact_id,
        uow_factory=uow_factory,
        downloader=downloader,
        data_dir=data_dir,
        adapter=adapter,
    )

    assert report.status is DownloadStatus.COMPLETED
    assert report.url_refreshed is True
    assert downloader.size_tolerances == [8 * 1024, 8 * 1024]


def test_refresh_only_once(uow_factory, seeded_artifact_id, data_dir):
    """刷新后再 404：不再刷新第二次，最终 failed。"""
    downloader = ScriptedDownloader([failed_404(), failed_404()])
    adapter = ScriptedAdapter(
        ArtifactUrlRefreshed(download_url="https://example.com/fw/new-url.zip", url_expires_at=None)
    )

    report = run_download(
        artifact_id=seeded_artifact_id, uow_factory=uow_factory,
        downloader=downloader, data_dir=data_dir, adapter=adapter,
    )

    assert report.status is DownloadStatus.FAILED
    assert len(adapter.requests) == 1        # 只刷新了一次
    assert len(downloader.calls) == 2        # 原始 + 重试各一次


def test_refresh_failed_reports_original_error(uow_factory, seeded_artifact_id, data_dir):
    """刷新失败：不重试，错误信息包含刷新失败原因。"""
    downloader = ScriptedDownloader([failed_404()])
    adapter = ScriptedAdapter(
        ArtifactRefreshFailed(reason_code=RefreshFailureReason.NOT_FOUND, detail="来源已下架")
    )

    report = run_download(
        artifact_id=seeded_artifact_id, uow_factory=uow_factory,
        downloader=downloader, data_dir=data_dir, adapter=adapter,
    )

    assert report.status is DownloadStatus.FAILED
    assert report.error_code == "http_404"
    assert "来源已下架" in (report.error_message or "")
    assert len(downloader.calls) == 1


def test_no_adapter_no_refresh(uow_factory, seeded_artifact_id, data_dir):
    """未提供适配器：404 直接失败，不尝试刷新。"""
    downloader = ScriptedDownloader([failed_404()])

    report = run_download(
        artifact_id=seeded_artifact_id, uow_factory=uow_factory,
        downloader=downloader, data_dir=data_dir,
    )

    assert report.status is DownloadStatus.FAILED
    assert report.url_refreshed is False


def test_5xx_does_not_trigger_refresh(uow_factory, seeded_artifact_id, data_dir):
    """5xx 不属于失效地址，不触发刷新。"""
    downloader = ScriptedDownloader([
        DownloadFailed(
            error_code=DownloadErrorCode.HTTP_5XX, http_status=502,
            detail="HTTP 502", bytes_received=0,
        )
    ])
    adapter = ScriptedAdapter(
        ArtifactUrlRefreshed(
            download_url="https://example.com/should-not-happen", url_expires_at=None
        )
    )

    report = run_download(
        artifact_id=seeded_artifact_id, uow_factory=uow_factory,
        downloader=downloader, data_dir=data_dir, adapter=adapter,
    )

    assert report.status is DownloadStatus.FAILED
    assert adapter.requests == []


# ---------------------------------------------------------------------------
# 前置条件（AC-30 等）
# ---------------------------------------------------------------------------


def test_unknown_artifact_raises(uow_factory, data_dir):
    downloader = ScriptedDownloader([])
    with pytest.raises(UnknownArtifactError):
        run_download(
            artifact_id="does-not-exist", uow_factory=uow_factory,
            downloader=downloader, data_dir=data_dir,
        )


def test_concurrent_download_rejected(uow_factory, seeded_artifact_id, data_dir):
    """同一 Artifact 已有活动下载：第二次请求被拒绝（AC-30）。"""
    # 手工造一条 queued 记录占住 Artifact
    with uow_factory.begin() as uow:
        uow.downloads.create_download(artifact_id=seeded_artifact_id, requested_at=utc_now())

    downloader = ScriptedDownloader([succeeded()])
    with pytest.raises(ActiveDownloadExistsError):
        run_download(
            artifact_id=seeded_artifact_id, uow_factory=uow_factory,
            downloader=downloader, data_dir=data_dir,
        )
