"""海康威视国际站摄像机固件适配器。

国际站把完整固件目录服务端渲染在一个 HTML 页面中。本适配器通过注入的
``HttpFetcher`` 获取页面，调用纯解析器保留网站结构，再只映射网站明确标记为摄像机的
分类。每个 ``Applied to`` 型号成为独立 Product；同版本的地域变体成为同一 Release
下的不同 Artifact。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit

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
from firmatlas.adapters.hikvision_global.firmware_parser import (
    FirmwareAssetEntry,
    FirmwareProductEntry,
    ReleaseNoteEntry,
    extract_firmware_version,
    parse_firmware_products,
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

_BASE_URL = "https://www.hikvision.com/en/"
_INDEX_URL = "https://www.hikvision.com/en/support/download/firmware/"
_EXPECTED_HOST = "www.hikvision.com"
_ASSET_HOST = "assets.hikvision.com"

# 国际站页面中明确属于摄像机的分类。其它类别不猜测、不入库。
_CAMERA_CATEGORIES = frozenset(
    {
        ("IP-Products", "Network-Cameras"),
        ("IP-Products", "PTZ-Cameras"),
        ("Thermal-Products", "Security-thermal-cameras"),
        ("Thermal-Products", "Thermography-thermal-cameras"),
        ("Turbo-HD-Products", "Turbo-HD-Cameras"),
        ("HiLook-IP-Products", "Network-Cameras"),
        ("HiLook-IP-Products", "PTZ-Cameras"),
    }
)


class HikvisionGlobalAdapter:
    """海康威视国际站摄像机适配器。"""

    source_key = "hikvision-global"

    def __init__(self, http: HttpFetcher) -> None:
        self._http = http

    async def discover(self):
        """获取一次完整目录，逐产品产出发现事件。"""
        try:
            fetched = await self._http.get_text(_INDEX_URL)
        except Exception as exc:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason=f"国际站固件目录请求失败: {exc}",
                issues=(),
            )
            return

        if not _is_expected_index_url(fetched.url):
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason=f"国际站固件目录重定向到来源外: {fetched.url}",
                issues=(),
            )
            return

        parsed = parse_firmware_products(fetched.text)
        if not parsed:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="国际站固件目录未解析到产品",
                issues=(),
            )
            return

        products: dict[str, _ProductTree] = {}
        skipped: list[SkippedCandidate] = []
        issues: list[AdapterIssueSummary] = []
        unmatched_release_notes: list[str] = []
        non_target_counts: Counter[tuple[str, str]] = Counter()
        parse_failures = 0

        for source_product in parsed:
            category = (source_product.main_category, source_product.sub_category)
            if category not in _CAMERA_CATEGORIES:
                non_target_counts[category] += 1
                continue
            parse_failures += _collect_camera_product(
                source_product,
                products=products,
                skipped=skipped,
                unmatched_release_notes=unmatched_release_notes,
            )

        if unmatched_release_notes:
            examples = "; ".join(dict.fromkeys(unmatched_release_notes[:3]))
            issues.append(
                AdapterIssueSummary(
                    code="release_note_unmatched",
                    detail=(
                        f"{len(unmatched_release_notes)} 条发布说明无法安全匹配固件版本"
                        f"；示例: {examples}"
                    ),
                    source_url=_INDEX_URL,
                )
            )

        for (main_category, sub_category), count in sorted(non_target_counts.items()):
            category_text = f"{main_category}/{sub_category}"
            skipped.append(
                SkippedCandidate(
                    stage="product",
                    reason_code=SkipReason.UNMAPPED_TYPE,
                    detail=f"非摄像机分类 {category_text}，跳过 {count} 个目录项",
                    source_url=_INDEX_URL,
                    raw_hint=category_text,
                )
            )

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
                incomplete_reason="国际站目录没有生成可下载的摄像机产品",
                issues=tuple(issues),
            )
            return

        yield DiscoveryCompleted(
            is_complete=parse_failures == 0,
            incomplete_reason=(
                f"{parse_failures} 个摄像机固件分组或文件解析失败" if parse_failures else None
            ),
            issues=tuple(issues),
        )

    async def refresh_artifact_url(self, request: ArtifactRefreshRequest) -> ArtifactRefreshResult:
        """重新读取完整目录，并按三层稳定身份寻找同一 Artifact 的当前地址。"""
        if request.hardware_revision_source_key != UNSPECIFIED_REVISION_SOURCE_KEY:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=(
                    "hikvision-global 未提供独立硬件版本，刷新请求的硬件版本身份不匹配: "
                    f"{request.hardware_revision_source_key}"
                ),
            )

        try:
            fetched = await self._http.get_text(_INDEX_URL)
        except Exception as exc:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.SOURCE_ERROR,
                detail=f"刷新时请求国际站固件目录失败: {exc}",
            )

        if not _is_expected_index_url(fetched.url):
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.SOURCE_ERROR,
                detail=f"刷新时国际站固件目录重定向到来源外: {fetched.url}",
            )

        parsed = parse_firmware_products(fetched.text)
        if not parsed:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.SOURCE_ERROR,
                detail="刷新时国际站固件目录未解析到产品",
            )

        product_found = False
        artifact_release: str | None = None

        for source_product in parsed:
            if (source_product.main_category, source_product.sub_category) not in (
                _CAMERA_CATEGORIES
            ):
                continue
            product_url = _absolute_product_url(source_product.product_url)
            if product_url is None:
                continue
            for group in source_product.groups:
                matching_model = any(
                    _product_source_key(product_url, _normalize_display_text(model))
                    == request.product_source_key
                    for model in group.applied_models
                    if _normalize_display_text(model)
                )
                if not matching_model:
                    continue
                product_found = True

                for asset in group.firmware_assets:
                    if not asset.download_url:
                        continue
                    if _artifact_source_key(asset.download_url) != request.artifact_source_key:
                        continue

                    raw_version = extract_firmware_version(asset.title)
                    normalized_version = _normalize_version(raw_version)
                    if normalized_version is None:
                        return ArtifactRefreshFailed(
                            reason_code=RefreshFailureReason.SOURCE_ERROR,
                            detail=(
                                f"找到 Artifact {request.artifact_source_key}，"
                                "但当前标题无法解析版本"
                            ),
                        )
                    artifact_release = f"fw/{normalized_version.lower()}"
                    if artifact_release != request.release_source_key:
                        continue
                    if not _is_asset_url(asset.download_url):
                        return ArtifactRefreshFailed(
                            reason_code=RefreshFailureReason.SOURCE_ERROR,
                            detail=(
                                f"找到 Artifact {request.artifact_source_key}，"
                                "但当前下载地址不属于官方资源域名"
                            ),
                        )
                    return ArtifactUrlRefreshed(
                        download_url=asset.download_url,
                        url_expires_at=None,
                    )

        if artifact_release is not None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=(
                    f"Artifact {request.artifact_source_key} 当前属于 {artifact_release}，"
                    f"与请求的 {request.release_source_key} 不一致"
                ),
            )

        target = (
            f"产品 {request.product_source_key} 下的 Artifact {request.artifact_source_key}"
            if product_found
            else f"产品 {request.product_source_key}"
        )
        return ArtifactRefreshFailed(
            reason_code=RefreshFailureReason.NOT_FOUND,
            detail=f"国际站当前目录未找到{target}，记录可能已下架",
        )


def _collect_camera_product(
    source_product: FirmwareProductEntry,
    *,
    products: dict[str, _ProductTree],
    skipped: list[SkippedCandidate],
    unmatched_release_notes: list[str],
) -> int:
    """把一个父级目录项展开到 Applied-to 型号树，返回解析失败数量。"""
    product_url = _absolute_product_url(source_product.product_url)
    if not source_product.title or product_url is None:
        skipped.append(
            SkippedCandidate(
                stage="product",
                reason_code=SkipReason.MISSING_IDENTITY,
                detail=f"摄像机目录项缺少标题或产品 URL: {source_product.title or '<empty>'}",
                source_url=_INDEX_URL,
                raw_hint=source_product.title or None,
            )
        )
        return 1

    failures = 0
    category_text = f"{source_product.main_category}/{source_product.sub_category}"

    for group_index, group in enumerate(source_product.groups):
        if not group.applied_models:
            skipped.append(
                SkippedCandidate(
                    stage="product",
                    reason_code=SkipReason.MISSING_IDENTITY,
                    detail=f"产品 {source_product.title} 的固件分组缺少 Applied to 型号",
                    source_url=product_url,
                    raw_hint=f"group-{group_index}",
                )
            )
            failures += 1
            continue

        version_assets: dict[str, list[FirmwareAssetEntry]] = {}
        version_raw: dict[str, str] = {}
        for asset in group.firmware_assets:
            raw_version = extract_firmware_version(asset.title)
            normalized_version = _normalize_version(raw_version)
            if not raw_version or not normalized_version or not _is_asset_url(asset.download_url):
                skipped.append(
                    SkippedCandidate(
                        stage="artifact",
                        reason_code=(
                            SkipReason.PARSE_FAILED if raw_version else SkipReason.MISSING_IDENTITY
                        ),
                        detail=f"产品 {source_product.title} 的固件缺少有效版本或下载地址: "
                        f"{asset.title or '<empty>'}",
                        source_url=product_url,
                        raw_hint=asset.title or None,
                    )
                )
                failures += 1
                continue
            version_raw.setdefault(normalized_version, raw_version)
            version_assets.setdefault(normalized_version, []).append(asset)

        if not version_assets:
            if not group.firmware_assets:
                skipped.append(
                    SkippedCandidate(
                        stage="artifact",
                        reason_code=SkipReason.MISSING_IDENTITY,
                        detail=f"产品 {source_product.title} 的固件分组没有下载文件",
                        source_url=product_url,
                        raw_hint=f"group-{group_index}",
                    )
                )
                failures += 1
            continue

        notes_by_version = _match_release_notes(
            group.release_notes,
            available_versions=tuple(version_assets),
            unmatched_release_notes=unmatched_release_notes,
        )
        for model_raw in group.applied_models:
            model = _normalize_display_text(model_raw)
            if not model:
                failures += 1
                continue
            product_key = _product_source_key(product_url, model)
            tree = products.setdefault(
                product_key,
                _ProductTree(
                    source_key=product_key,
                    model_raw=model,
                    series=source_product.title,
                    source_category=category_text,
                    source_url=product_url,
                ),
            )
            for normalized_version, assets in version_assets.items():
                tree.add_release(
                    version_raw=version_raw[normalized_version],
                    version_normalized=normalized_version,
                    assets=assets,
                    release_note=notes_by_version.get(normalized_version),
                )

    return failures


def _match_release_notes(
    notes: tuple[ReleaseNoteEntry, ...],
    *,
    available_versions: tuple[str, ...],
    unmatched_release_notes: list[str],
) -> dict[str, ReleaseNoteEntry]:
    matched: dict[str, ReleaseNoteEntry] = {}
    for note in notes:
        version = _normalize_version(extract_firmware_version(note.title))
        target_version = _matching_release_version(version, available_versions)
        if note.url and target_version:
            matched.setdefault(target_version, note)
            continue
        unmatched_release_notes.append(note.title or "<empty>")
    return matched


def _matching_release_version(
    note_version: str | None,
    available_versions: tuple[str, ...],
) -> str | None:
    """匹配发布说明版本；只有单版本分组允许标题不写版本。"""
    if note_version in available_versions:
        return note_version
    if note_version:
        compatible = [
            version for version in available_versions if version.startswith(f"{note_version}_")
        ]
        return compatible[0] if len(compatible) == 1 else None
    return available_versions[0] if len(available_versions) == 1 else None


@dataclass
class _ReleaseTree:
    version_raw: str
    version_normalized: str
    title: str
    release_notes_url: str | None
    artifacts: dict[str, FirmwareArtifactCandidate] = field(default_factory=dict)


@dataclass
class _ProductTree:
    source_key: str
    model_raw: str
    series: str
    source_category: str
    source_url: str
    releases: dict[str, _ReleaseTree] = field(default_factory=dict)

    def add_release(
        self,
        *,
        version_raw: str,
        version_normalized: str,
        assets: list[FirmwareAssetEntry],
        release_note: ReleaseNoteEntry | None,
    ) -> None:
        release_key = f"fw/{version_normalized.lower()}"
        release = self.releases.setdefault(
            release_key,
            _ReleaseTree(
                version_raw=version_raw,
                version_normalized=version_normalized,
                title=assets[0].title,
                release_notes_url=release_note.url if release_note else None,
            ),
        )
        if release.release_notes_url is None and release_note is not None:
            release.release_notes_url = release_note.url

        for asset in assets:
            assert asset.download_url is not None
            artifact_key = _artifact_source_key(asset.download_url)
            filename = _filename(asset.download_url)
            release.artifacts.setdefault(
                artifact_key,
                FirmwareArtifactCandidate(
                    source_key=artifact_key,
                    artifact_type=ArtifactType.FIRMWARE,
                    original_filename=filename,
                    download_url=asset.download_url,
                    url_expires_at=None,
                    advertised_size=None,
                    media_type="application/zip" if filename.lower().endswith(".zip") else None,
                    official_checksum=None,
                ),
            )

    def to_candidate(self) -> ProductCandidate | None:
        releases = tuple(
            FirmwareReleaseCandidate(
                source_key=release_key,
                version_raw=release.version_raw,
                version_normalized=release.version_normalized,
                release_date=None,
                title=release.title,
                release_notes=None,
                release_notes_url=release.release_notes_url,
                source_url=self.source_url,
                artifacts=tuple(release.artifacts.values()),
            )
            for release_key, release in self.releases.items()
            if release.artifacts
        )
        if not releases:
            return None

        revision = HardwareRevisionCandidate(
            source_key=UNSPECIFIED_REVISION_SOURCE_KEY,
            raw_revision=None,
            normalized_revision=UNSPECIFIED_REVISION,
            revision_explicit=False,
            source_url=self.source_url,
            releases=releases,
        )
        return ProductCandidate(
            source_key=self.source_key,
            display_name=self.model_raw,
            model_raw=self.model_raw,
            model_normalized=self.model_raw.upper(),
            series=self.series,
            product_family=ProductFamily.CAMERA,
            product_type=ProductType.CAMERA,
            source_category=self.source_category,
            source_url=self.source_url,
            hardware_revisions=(revision,),
        )


def _is_expected_index_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.hostname == _EXPECTED_HOST and parsed.path.rstrip("/") == (
        "/en/support/download/firmware"
    )


def _absolute_product_url(url: str | None) -> str | None:
    if not url:
        return None
    absolute = urljoin(_BASE_URL, url)
    parsed = urlsplit(absolute)
    if parsed.hostname != _EXPECTED_HOST or not parsed.path.startswith("/en/products/"):
        return None
    return absolute


def _is_asset_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == _ASSET_HOST and bool(parsed.path)


def _normalize_display_text(value: str) -> str:
    return " ".join(value.split())


def _normalize_version(version: str | None) -> str | None:
    if not version:
        return None
    normalized = re.sub(r"(?i)[ _]+BUILD[ _]*", "_", version.strip())
    normalized = re.sub(r"[ .](?=\d{6}$)", "_", normalized)
    return normalized.upper()


def _product_source_key(product_url: str, model_raw: str) -> str:
    path = urlsplit(product_url).path.rstrip("/").lower()
    identity = f"{path}\0{model_raw.upper()}"
    return hashlib.sha256(identity.encode()).hexdigest()


_SOURCE_ID = re.compile(r"(?:^|_)(S\d+)$", re.IGNORECASE)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _artifact_source_key(url: str) -> str:
    stem = _filename(url).rsplit(".", 1)[0]
    source_id = _SOURCE_ID.search(stem)
    if source_id:
        return source_id.group(1).upper()
    if _UUID.fullmatch(stem):
        return stem.lower()
    return hashlib.sha256(urlsplit(url).path.encode()).hexdigest()


def _filename(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])
