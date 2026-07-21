"""大华国际站固件下载中心摄像机适配器。

通过大华国际站 API 枚举 Network Cameras / PTZ / PT / Intelligent Traffic /
Thermal / Explosion-Proof 六类摄像机固件。每个固件条目关联多个产品型号，
按 product_id 分组构建 ProductCandidate 树。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from urllib.parse import unquote, urlsplit

from firmatlas.adapters.dahua_global.classification import (
    CAMERA_CATEGORY_IDS,
    classify,
)
from firmatlas.adapters.dahua_global.firmware_parser import (
    FirmwareEntry,
    parse_firmware_list,
)
from firmatlas.adapters.events import (
    AdapterIssueSummary,
    ArtifactRefreshFailed,
    ArtifactRefreshRequest,
    ArtifactRefreshResult,
    ArtifactUrlRefreshed,
    DiscoveredProduct,
    DiscoveryCompleted,
    RefreshFailureReason,
    SkippedCandidate,
    SkipReason,
)
from firmatlas.domain.candidates import (
    UNSPECIFIED_REVISION,
    UNSPECIFIED_REVISION_SOURCE_KEY,
    FirmwareArtifactCandidate,
    FirmwareReleaseCandidate,
    HardwareRevisionCandidate,
    ProductCandidate,
)
from firmatlas.domain.model import ArtifactType, ProductFamily, ProductType
from firmatlas.infra.http_client import HttpFetcher

_API_BASE = "https://www.dahuasecurity.com"
_LIST_URL = f"{_API_BASE}/api/en/downloadCenter/firmware/list"
_EXPECTED_HOST = "www.dahuasecurity.com"
_ASSET_HOST = "materialfile.dahuasecurity.com"


class DahuaGlobalAdapter:
    """大华国际站摄像机固件适配器。"""

    source_key = "dahua-global"

    def __init__(self, http: HttpFetcher) -> None:
        self._http = http

    async def discover(self):
        products: dict[str, _ProductTree] = {}
        skipped: list[SkippedCandidate] = []
        issues: list[AdapterIssueSummary] = []
        parse_failures = 0

        for category_id in sorted(CAMERA_CATEGORY_IDS):
            classification = classify(category_id)
            if classification is None:
                continue

            page = 1
            while True:
                url = f"{_LIST_URL}?page={page}&child_menu_id={category_id}"
                try:
                    fetched = await self._http.get_json(url)
                except Exception as exc:
                    issues.append(
                        AdapterIssueSummary(
                            code="api_error",
                            detail=(
                                f"分类 {classification.source_category} "
                                f"(id={category_id}) 第 {page} 页请求失败: {exc}"
                            ),
                            source_url=url,
                        )
                    )
                    break

                raw_data = fetched.data
                if not isinstance(raw_data, dict):
                    parse_failures += 1
                    break

                response_data = raw_data.get("data")
                if not isinstance(response_data, dict):
                    parse_failures += 1
                    break

                raw_list = response_data.get("list")
                if not isinstance(raw_list, list):
                    break

                entries = parse_firmware_list(raw_list)
                if not entries:
                    break

                total = response_data.get("total", 0)
                if not isinstance(total, int) or total <= 0:
                    break

                category_failures = 0
                for entry in entries:
                    if not entry.products:
                        skipped.append(
                            SkippedCandidate(
                                stage="product",
                                reason_code=SkipReason.MISSING_IDENTITY,
                                detail=(
                                    f"固件 {entry.firmware_name} 没有关联产品"
                                ),
                                source_url=_LIST_URL,
                                raw_hint=entry.firmware_id,
                            )
                        )
                        category_failures += 1
                        continue

                    for prod in entry.products:
                        if prod.product_name.upper().startswith(("NVR", "DVR", "XVR", "HCVR")):
                            skipped.append(
                                SkippedCandidate(
                                    stage="product",
                                    reason_code=SkipReason.UNMAPPED_TYPE,
                                    detail=(
                                        f"产品 {prod.product_name} 为录像机，"
                                        f"不在摄像机采集范围内"
                                    ),
                                    source_url=_LIST_URL,
                                    raw_hint=f"pid:{prod.product_id}",
                                )
                            )
                            continue

                        product_key = _product_source_key(prod.product_id)
                        tree = products.setdefault(
                            product_key,
                            _ProductTree(
                                source_key=product_key,
                                product_id=prod.product_id,
                                product_name=prod.product_name,
                                source_category=classification.source_category,
                            ),
                        )
                        tree.add_firmware(entry)

                parse_failures += category_failures
                if page * 10 >= total:
                    break
                page += 1

        discovered_count = 0
        for tree in products.values():
            candidate = tree.to_candidate()
            if candidate is None:
                continue
            discovered_count += 1
            yield DiscoveredProduct(product=candidate)

        for event in skipped:
            yield event

        if discovered_count == 0:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="大华国际站 API 未生成可下载的摄像机产品",
                issues=tuple(issues),
            )
            return

        is_complete = parse_failures == 0 and not issues
        yield DiscoveryCompleted(
            is_complete=is_complete,
            incomplete_reason=(
                f"{parse_failures} 条固件解析失败" if parse_failures else None
            ),
            issues=tuple(issues),
        )

    async def refresh_artifact_url(
        self, request: ArtifactRefreshRequest
    ) -> ArtifactRefreshResult:
        """重新扫描固件列表，按 firmware_id 寻找同一 Artifact 的当前地址。"""
        if request.hardware_revision_source_key != UNSPECIFIED_REVISION_SOURCE_KEY:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=(
                    "dahua-global 未提供独立硬件版本，刷新请求的硬件版本身份不匹配: "
                    f"{request.hardware_revision_source_key}"
                ),
            )

        product_id = _product_id_from_source_key(request.product_source_key)
        if product_id is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=f"无法从 product_source_key 解析 product_id: {request.product_source_key}",
            )

        firmware_id = _firmware_id_from_source_key(request.artifact_source_key)
        if firmware_id is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=(
                    "无法从 artifact_source_key 解析 firmware_id: "
                    f"{request.artifact_source_key}"
                ),
            )

        for category_id in sorted(CAMERA_CATEGORY_IDS):
            try:
                entry = await _find_firmware_by_id(
                    self._http, category_id, firmware_id
                )
                if entry is not None:
                    return _build_refresh_result(
                        entry, request, product_id, firmware_id
                    )
            except Exception as exc:
                return ArtifactRefreshFailed(
                    reason_code=RefreshFailureReason.SOURCE_ERROR,
                    detail=f"刷新时请求大华固件列表失败: {exc}",
                )

        return ArtifactRefreshFailed(
            reason_code=RefreshFailureReason.NOT_FOUND,
            detail=f"大华当前固件列表未找到 firmware_id={firmware_id}，记录可能已下架",
        )


async def _find_firmware_by_id(
    http: HttpFetcher, category_id: int, firmware_id: str
) -> FirmwareEntry | None:
    page = 1
    while True:
        url = f"{_LIST_URL}?page={page}&child_menu_id={category_id}"
        fetched = await http.get_json(url)
        raw_data = fetched.data
        if not isinstance(raw_data, dict):
            return None
        response_data = raw_data.get("data")
        if not isinstance(response_data, dict):
            return None
        raw_list = response_data.get("list")
        if not isinstance(raw_list, list) or not raw_list:
            return None

        for item in raw_list:
            if str(item.get("firmware_id")) == firmware_id:
                entries = parse_firmware_list([item])
                return entries[0] if entries else None

        total = response_data.get("total", 0)
        if page * 10 >= total:
            return None
        page += 1


def _build_refresh_result(
    entry: FirmwareEntry,
    request: ArtifactRefreshRequest,
    product_id: str,
    firmware_id: str,
) -> ArtifactRefreshResult:
    expected_artifact_key = _artifact_source_key(firmware_id)
    current_artifact_key = _artifact_source_key(entry.firmware_id)

    if current_artifact_key != expected_artifact_key:
        return ArtifactRefreshFailed(
            reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
            detail=(
                f"firmware_id 为 {firmware_id}，"
                f"但找到的 artifact source_key 为 {current_artifact_key}"
            ),
        )

    expected_release_key = _release_source_key(firmware_id)
    if expected_release_key != request.release_source_key:
        return ArtifactRefreshFailed(
            reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
            detail=(
                f"firmware_id={firmware_id} 的 release_source_key "
                f"{expected_release_key} 与请求的 {request.release_source_key} 不一致"
            ),
        )

    if not _is_asset_url(entry.firmware_url):
        return ArtifactRefreshFailed(
            reason_code=RefreshFailureReason.SOURCE_ERROR,
            detail=f"固件 URL 不属于官方资源域名: {entry.firmware_url}",
        )

    has_product = any(
        _product_source_key(p.product_id) == request.product_source_key
        for p in entry.products
    )
    if not has_product:
        return ArtifactRefreshFailed(
            reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
            detail=(
                f"firmware_id={firmware_id} 当前关联的产品 "
                f"与请求的 {request.product_source_key} 不一致"
            ),
        )

    return ArtifactUrlRefreshed(
        download_url=entry.firmware_url,
        url_expires_at=None,
    )


# ---------------------------------------------------------------------------
# 内部树结构
# ---------------------------------------------------------------------------


@dataclass
class _ArtifactInfo:
    source_key: str
    download_url: str
    original_filename: str | None
    advertised_size: int | None
    media_type: str | None
    checksum_md5: str | None
    checksum_sha256: str | None


@dataclass
class _ReleaseInfo:
    source_key: str
    version_raw: str | None
    version_normalized: str | None
    release_date: date | None
    release_notes_url: str | None
    artifact: _ArtifactInfo


@dataclass
class _ProductTree:
    source_key: str
    product_id: str
    product_name: str
    source_category: str
    releases: dict[str, _ReleaseInfo] = field(default_factory=dict)

    def add_firmware(self, entry: FirmwareEntry) -> None:
        release_key = _release_source_key(entry.firmware_id)
        if release_key in self.releases:
            return
        self.releases[release_key] = _ReleaseInfo(
            source_key=release_key,
            version_raw=entry.version_raw,
            version_normalized=(
                entry.version_raw.lower() if entry.version_raw else None
            ),
            release_date=entry.post_date,
            release_notes_url=entry.release_notes_url,
            artifact=_ArtifactInfo(
                source_key=_artifact_source_key(entry.firmware_id),
                download_url=entry.firmware_url,
                original_filename=_filename_from_url(entry.firmware_url),
                advertised_size=entry.advertised_size_bytes,
                media_type=(
                    "application/zip"
                    if entry.firmware_url.lower().endswith(".zip")
                    else "application/octet-stream"
                ),
                checksum_md5=entry.md5,
                checksum_sha256=entry.sha256,
            ),
        )

    def to_candidate(self) -> ProductCandidate | None:
        if not self.releases:
            return None

        release_candidates = tuple(
            FirmwareReleaseCandidate(
                source_key=release.source_key,
                version_raw=release.version_raw or "",
                version_normalized=release.version_normalized,
                release_date=release.release_date,
                title=release.version_raw or None,
                release_notes=None,
                release_notes_url=release.release_notes_url,
                source_url="https://www.dahuasecurity.com/download-center/firmware",
                artifacts=(
                    FirmwareArtifactCandidate(
                        source_key=release.artifact.source_key,
                        artifact_type=ArtifactType.FIRMWARE,
                        original_filename=release.artifact.original_filename,
                        download_url=release.artifact.download_url,
                        url_expires_at=None,
                        advertised_size=release.artifact.advertised_size,
                        media_type=release.artifact.media_type,
                        official_checksum=None,
                    ),
                ),
            )
            for release in self.releases.values()
        )

        if not release_candidates:
            return None

        revision = HardwareRevisionCandidate(
            source_key=UNSPECIFIED_REVISION_SOURCE_KEY,
            raw_revision=None,
            normalized_revision=UNSPECIFIED_REVISION,
            revision_explicit=False,
            source_url=None,
            releases=release_candidates,
        )

        return ProductCandidate(
            source_key=self.source_key,
            display_name=self.product_name,
            model_raw=self.product_name,
            model_normalized=self.product_name.upper(),
            series=None,
            product_family=ProductFamily.CAMERA,
            product_type=ProductType.CAMERA,
            source_category=self.source_category,
            source_url="https://www.dahuasecurity.com/download-center/firmware",
            hardware_revisions=(revision,),
        )


# ---------------------------------------------------------------------------
# source_key 生成
# ---------------------------------------------------------------------------


def _product_source_key(product_id: str) -> str:
    return f"pid:{product_id}"


def _release_source_key(firmware_id: str) -> str:
    return f"fid:{firmware_id}"


def _artifact_source_key(firmware_id: str) -> str:
    return f"fid:{firmware_id}"


def _product_id_from_source_key(source_key: str) -> str | None:
    if source_key.startswith("pid:"):
        return source_key[4:]
    return None


def _firmware_id_from_source_key(source_key: str) -> str | None:
    if source_key.startswith("fid:"):
        return source_key[4:]
    return None


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _is_asset_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == _ASSET_HOST and bool(parsed.path)


def _filename_from_url(url: str) -> str | None:
    try:
        return unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or None
    except Exception:
        return None
