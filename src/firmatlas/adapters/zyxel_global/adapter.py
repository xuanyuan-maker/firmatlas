"""Zyxel Global 当前产品固件适配器。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from pathlib import PurePosixPath
from urllib.parse import unquote, urlencode, urlsplit

from firmatlas.adapters.events import (
    AdapterIssueSummary,
    ArtifactRefreshFailed,
    ArtifactRefreshRequest,
    ArtifactRefreshResult,
    ArtifactUrlRefreshed,
    DiscoveredProduct,
    DiscoveryCompleted,
    DiscoveryEvent,
    RefreshFailureReason,
    SkippedCandidate,
    SkipReason,
)
from firmatlas.adapters.zyxel_global.autocomplete import (
    EnumerationResult,
    ProductModelEntry,
    enumerate_product_models,
    parse_autocomplete_response,
)
from firmatlas.adapters.zyxel_global.classification import Classification, classify
from firmatlas.adapters.zyxel_global.download_parser import (
    DownloadMaterial,
    FirmwareDownload,
    firmware_downloads,
    parse_download_materials,
)
from firmatlas.domain.candidates import (
    UNSPECIFIED_REVISION,
    UNSPECIFIED_REVISION_SOURCE_KEY,
    FirmwareArtifactCandidate,
    FirmwareReleaseCandidate,
    HardwareRevisionCandidate,
    ProductCandidate,
)
from firmatlas.domain.model import ArtifactType
from firmatlas.infra.http_client import HttpFetcher

_BASE_URL = "https://www.zyxel.com"
_DOWNLOAD_PAGE = f"{_BASE_URL}/global/en/support/download"
_AUTOCOMPLETE_API = f"{_BASE_URL}/global/en/search_api_autocomplete/product_list_by_model"
_SOURCE_HOST = "www.zyxel.com"
_MACHINE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


class ZyxelGlobalAdapter:
    """从 Zyxel Worldwide 当前产品下载中心发现公开固件。"""

    source_key = "zyxel-global"

    def __init__(self, http: HttpFetcher) -> None:
        self._http = http

    async def discover(self) -> AsyncIterator[DiscoveryEvent]:
        try:
            enumeration = await enumerate_product_models(self._search_autocomplete)
        except Exception as exc:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason=f"Zyxel 产品枚举失败: {exc}",
                issues=(),
            )
            return

        targets: list[tuple[ProductModelEntry, Classification]] = []
        excluded_count = 0
        for model in enumeration.products:
            classification = _classify_model(model)
            if classification is None:
                excluded_count += 1
                continue
            targets.append((model, classification))

        if not targets:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="Zyxel 产品枚举没有命中白名单型号",
                issues=_enumeration_issues(enumeration),
            )
            return

        failures: list[str] = []
        without_firmware: list[str] = []
        for model, classification in targets:
            try:
                materials = await self._fetch_materials(model.machine_name)
            except Exception as exc:
                failures.append(f"{model.machine_name}: {exc}")
                continue

            product, skipped = _build_product(model, classification, materials)
            for event in skipped:
                yield event
            if product is None:
                without_firmware.append(model.display_name)
                continue
            yield DiscoveredProduct(product=product)

        if excluded_count:
            yield SkippedCandidate(
                stage="product",
                reason_code=SkipReason.UNMAPPED_TYPE,
                detail=f"白名单外 Zyxel 产品共 {excluded_count} 项",
                source_url=_DOWNLOAD_PAGE,
                raw_hint="non_target_products",
            )

        issues = list(_enumeration_issues(enumeration))
        if without_firmware:
            issues.append(
                AdapterIssueSummary(
                    code="target_without_public_firmware",
                    detail=(
                        f"{len(without_firmware)} 个白名单产品没有发现公开固件；"
                        f"示例: {'; '.join(without_firmware[:3])}"
                    ),
                    source_url=_DOWNLOAD_PAGE,
                )
            )

        incomplete_reasons: list[str] = []
        if enumeration.saturated_prefixes:
            incomplete_reasons.append(
                f"{len(enumeration.saturated_prefixes)} 个 Autocomplete 前缀达到递归深度上限"
            )
        if failures:
            incomplete_reasons.append(f"{len(failures)} 个白名单产品详情请求失败")

        yield DiscoveryCompleted(
            is_complete=not incomplete_reasons,
            incomplete_reason="；".join(incomplete_reasons) or None,
            issues=tuple(issues),
        )

    async def refresh_artifact_url(self, request: ArtifactRefreshRequest) -> ArtifactRefreshResult:
        machine_name = request.product_source_key.casefold()
        if _MACHINE_NAME.fullmatch(machine_name) is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=f"Zyxel 产品身份格式无效: {request.product_source_key}",
            )

        model = ProductModelEntry(
            machine_name=machine_name,
            display_name=_display_name_from_machine_name(machine_name),
        )
        classification = _classify_model(model)
        if classification is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=f"Zyxel 产品 {machine_name} 已不在白名单内",
            )

        try:
            materials = await self._fetch_materials(machine_name)
        except Exception as exc:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.SOURCE_ERROR,
                detail=f"刷新时请求 Zyxel 产品 {machine_name} 失败: {exc}",
            )

        product, _ = _build_product(model, classification, materials)
        if product is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.NOT_FOUND,
                detail=f"Zyxel 产品 {machine_name} 当前没有公开固件",
            )

        artifact_parent: tuple[str, str] | None = None
        for revision in product.hardware_revisions:
            for release in revision.releases:
                for artifact in release.artifacts:
                    if artifact.source_key != request.artifact_source_key:
                        continue
                    artifact_parent = (revision.source_key, release.source_key)
                    if artifact_parent != (
                        request.hardware_revision_source_key,
                        request.release_source_key,
                    ):
                        continue
                    return ArtifactUrlRefreshed(
                        download_url=artifact.download_url,
                        url_expires_at=None,
                    )

        if artifact_parent is not None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=(f"Artifact {request.artifact_source_key} 当前所属硬件版本或发布已变化"),
            )
        return ArtifactRefreshFailed(
            reason_code=RefreshFailureReason.NOT_FOUND,
            detail=f"Zyxel 产品 {machine_name} 未找到 Artifact {request.artifact_source_key}",
        )

    async def _search_autocomplete(self, prefix: str) -> list[ProductModelEntry]:
        query = urlencode(
            {
                "display": "block_1",
                "field": "model_machine_name",
                "filter": "model",
                "q": prefix,
            }
        )
        fetched = await self._http.get_text(
            f"{_AUTOCOMPLETE_API}?{query}",
            headers={"Accept": "application/json"},
        )
        _ensure_source_url(fetched.url, expected_path=urlsplit(_AUTOCOMPLETE_API).path)
        return parse_autocomplete_response(fetched.text)

    async def _fetch_materials(self, machine_name: str) -> list[DownloadMaterial]:
        product_url = _product_url(machine_name)
        fetched = await self._http.get_text(product_url)
        _ensure_source_url(fetched.url, expected_path=urlsplit(_DOWNLOAD_PAGE).path)
        return parse_download_materials(fetched.text)


def _build_product(
    model: ProductModelEntry,
    classification: Classification,
    materials: list[DownloadMaterial],
) -> tuple[ProductCandidate | None, tuple[SkippedCandidate, ...]]:
    product_url = _product_url(model.machine_name)
    downloads = firmware_downloads(materials)
    skipped = tuple(
        SkippedCandidate(
            stage="artifact",
            reason_code=SkipReason.PARSE_FAILED,
            detail=f"无法从 Zyxel 固件文件名解析版本: {item.filename}",
            source_url=item.download_url,
            raw_hint=item.filename,
        )
        for item in materials
        if item.material_type == "firmware" and item.version_normalized is None
    )
    if not downloads:
        return None, skipped

    releases_by_version: dict[str, list[FirmwareDownload]] = {}
    for download in downloads:
        releases_by_version.setdefault(download.version_normalized, []).append(download)

    releases: list[FirmwareReleaseCandidate] = []
    for version, version_downloads in sorted(releases_by_version.items()):
        release_key = _derived_key("release", model.machine_name, version.casefold())
        artifacts = tuple(
            FirmwareArtifactCandidate(
                source_key=_url_path_source_key(download.download_url),
                artifact_type=ArtifactType.FIRMWARE,
                original_filename=download.filename,
                download_url=download.download_url,
                url_expires_at=None,
                advertised_size=None,
                media_type=_media_type(download.filename),
                official_checksum=None,
            )
            for download in sorted(version_downloads, key=lambda item: item.download_url)
        )
        release_notes_url = next(
            (
                download.release_notes_url
                for download in version_downloads
                if download.release_notes_url is not None
            ),
            None,
        )
        releases.append(
            FirmwareReleaseCandidate(
                source_key=release_key,
                version_raw=version_downloads[0].version_raw,
                version_normalized=version,
                release_date=None,
                title=f"{model.display_name} firmware {version}",
                release_notes=None,
                release_notes_url=release_notes_url,
                source_url=product_url,
                artifacts=artifacts,
            )
        )

    revision = HardwareRevisionCandidate(
        source_key=UNSPECIFIED_REVISION_SOURCE_KEY,
        raw_revision=None,
        normalized_revision=UNSPECIFIED_REVISION,
        revision_explicit=False,
        source_url=product_url,
        releases=tuple(releases),
    )
    return (
        ProductCandidate(
            source_key=model.machine_name,
            display_name=model.display_name,
            model_raw=model.display_name,
            model_normalized=model.display_name.upper(),
            series=classification.source_category,
            product_family=classification.family,
            product_type=classification.product_type,
            source_category=classification.source_category,
            source_url=product_url,
            hardware_revisions=(revision,),
        ),
        skipped,
    )


def _classify_model(model: ProductModelEntry) -> Classification | None:
    return classify(model.display_name) or classify(
        _display_name_from_machine_name(model.machine_name)
    )


def _display_name_from_machine_name(machine_name: str) -> str:
    return " ".join(part.upper() for part in machine_name.split("-"))


def _product_url(machine_name: str) -> str:
    return f"{_DOWNLOAD_PAGE}?{urlencode({'model': machine_name})}"


def _ensure_source_url(url: str, *, expected_path: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != _SOURCE_HOST
        or parsed.path.rstrip("/") != expected_path.rstrip("/")
    ):
        raise ValueError(f"Zyxel 请求重定向到来源外: {url}")


def _url_path_source_key(url: str) -> str:
    path = unquote(urlsplit(url).path).strip("/").casefold()
    return f"url-path:{path}"


def _derived_key(kind: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join((kind, *parts)).encode()).hexdigest()
    return f"derived:v1:{digest}"


def _media_type(filename: str) -> str | None:
    return "application/zip" if PurePosixPath(filename).suffix.casefold() == ".zip" else None


def _enumeration_issues(enumeration: EnumerationResult) -> tuple[AdapterIssueSummary, ...]:
    if not enumeration.saturated_prefixes:
        return ()
    return (
        AdapterIssueSummary(
            code="autocomplete_saturated",
            detail=(
                f"{len(enumeration.saturated_prefixes)} 个前缀达到递归深度上限；"
                f"示例: {', '.join(enumeration.saturated_prefixes[:5])}"
            ),
            source_url=_DOWNLOAD_PAGE,
        ),
    )
