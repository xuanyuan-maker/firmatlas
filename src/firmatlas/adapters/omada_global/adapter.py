"""Omada Worldwide 公开固件 API 适配器。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from datetime import date
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

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
from firmatlas.adapters.omada_global.classification import Classification, classify
from firmatlas.adapters.omada_global.response_parser import (
    FirmwareEntry,
    FirmwareModelEntry,
    parse_firmware_response,
    parse_model_response,
)
from firmatlas.domain.candidates import (
    FirmwareArtifactCandidate,
    FirmwareReleaseCandidate,
    HardwareRevisionCandidate,
    ProductCandidate,
)
from firmatlas.domain.model import ArtifactType
from firmatlas.infra.http_client import HttpFetcher

_BASE_URL = "https://support.omadanetworks.com"
_FIRMWARE_INDEX_URL = f"{_BASE_URL}/en/download/firmware/"
_PRODUCT_MENU_API = f"{_BASE_URL}/api/v1/menu/tourist/findProductMenuByTree"
_MODEL_LIST_API = f"{_BASE_URL}/api/v1/resource/tourist/findFirmwareModelByTypeId"
_FIRMWARE_API = f"{_BASE_URL}/api/v1/resource/tourist/findFirmwareByModel"
_SITE_ID = 1
_SOURCE_HOST = "support.omadanetworks.com"
_DOWNLOAD_HOST = "static.tp-link.com"

_PRODUCT_KEY_PATTERN = re.compile(r"model-id:(\d+)\Z")
_SIZE_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(KB|MB|GB)", re.IGNORECASE)
_SIZE_UNITS = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}


class OmadaGlobalAdapter:
    """发现 Omada Worldwide 白名单设备固件，不下载固件内容。"""

    source_key = "omada-global"

    def __init__(self, http: HttpFetcher) -> None:
        self._http = http

    async def discover(self) -> AsyncIterator[DiscoveryEvent]:
        try:
            models = await self._fetch_models()
        except Exception as exc:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason=f"请求 Omada 型号目录失败: {exc}",
                issues=(),
            )
            return

        targets: list[tuple[FirmwareModelEntry, Classification]] = []
        excluded_count = 0
        for model in models:
            classification = classify(model.model_name)
            if classification is None:
                excluded_count += 1
                continue
            targets.append((model, classification))

        if not targets:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="Omada 型号目录没有命中白名单型号",
                issues=(),
            )
            return

        failures: list[str] = []
        without_firmware: list[str] = []
        for model, classification in targets:
            try:
                entries = await self._fetch_firmware_entries(model.model_name)
            except Exception as exc:
                failures.append(f"{model.model_name}: {exc}")
                continue

            product, skipped = _build_product(model, classification, entries)
            for event in skipped:
                yield event
            if product is None:
                without_firmware.append(model.model_name)
                continue
            yield DiscoveredProduct(product=product)

        if excluded_count:
            yield SkippedCandidate(
                stage="product",
                reason_code=SkipReason.UNMAPPED_TYPE,
                detail=f"白名单外 Omada 产品共 {excluded_count} 项",
                source_url=_FIRMWARE_INDEX_URL,
                raw_hint="non_target_products",
            )

        issues: list[AdapterIssueSummary] = []
        if without_firmware:
            issues.append(
                AdapterIssueSummary(
                    code="target_without_firmware",
                    detail=(
                        f"{len(without_firmware)} 个白名单产品没有可入库固件；"
                        f"示例: {'; '.join(without_firmware[:3])}"
                    ),
                    source_url=_FIRMWARE_INDEX_URL,
                )
            )

        yield DiscoveryCompleted(
            is_complete=not failures,
            incomplete_reason=(f"{len(failures)} 个白名单产品固件请求失败" if failures else None),
            issues=tuple(issues),
        )

    async def refresh_artifact_url(self, request: ArtifactRefreshRequest) -> ArtifactRefreshResult:
        matched = _PRODUCT_KEY_PATTERN.fullmatch(request.product_source_key)
        if matched is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=f"Omada 产品身份格式无效: {request.product_source_key}",
            )

        model_id = int(matched.group(1))
        try:
            models = await self._fetch_models()
        except Exception as exc:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.SOURCE_ERROR,
                detail=f"刷新时请求 Omada 型号目录失败: {exc}",
            )

        model = next((item for item in models if item.model_id == model_id), None)
        if model is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.NOT_FOUND,
                detail=f"Omada 型号目录已找不到 modelId={model_id}",
            )
        classification = classify(model.model_name)
        if classification is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=f"Omada 型号 {model.model_name} 已不在白名单内",
            )

        try:
            entries = await self._fetch_firmware_entries(model.model_name)
        except Exception as exc:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.SOURCE_ERROR,
                detail=f"刷新时请求型号 {model.model_name} 固件失败: {exc}",
            )

        product, _ = _build_product(model, classification, entries)
        if product is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.NOT_FOUND,
                detail=f"Omada 型号 {model.model_name} 当前没有可入库固件",
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
            detail=f"型号 {model.model_name} 未找到 Artifact {request.artifact_source_key}",
        )

    async def _fetch_models(self) -> list[FirmwareModelEntry]:
        menu = await self._http.post_json(_PRODUCT_MENU_API, body={"siteId": _SITE_ID})
        _ensure_source_url(menu.url)
        root_type_id = _root_type_id(menu.data)
        fetched = await self._http.post_json(
            _MODEL_LIST_API,
            body={"siteId": _SITE_ID, "typeIds": [root_type_id], "modelName": ""},
        )
        _ensure_source_url(fetched.url)
        models = parse_model_response(json.dumps(fetched.data))
        if not models:
            raise ValueError("Omada 型号目录为空")
        return models

    async def _fetch_firmware_entries(self, model_name: str) -> list[FirmwareEntry]:
        fetched = await self._http.post_json(
            _FIRMWARE_API,
            body={"siteId": _SITE_ID, "modelName": model_name},
        )
        _ensure_source_url(fetched.url)
        return parse_firmware_response(json.dumps(fetched.data))


def _build_product(
    model: FirmwareModelEntry,
    classification: Classification,
    entries: list[FirmwareEntry],
) -> tuple[ProductCandidate | None, tuple[SkippedCandidate, ...]]:
    product_key = _product_source_key(model.model_id)
    product_url = _product_url(model.model_name)
    revisions: dict[str, list[FirmwareReleaseCandidate]] = {}
    revision_raw: dict[str, str] = {}
    skipped: list[SkippedCandidate] = []

    for entry in entries:
        parsed = entry.parsed_title
        if parsed is None:
            skipped.append(
                _skipped_artifact(
                    entry,
                    SkipReason.PARSE_FAILED,
                    f"无法解析 Omada 固件标题: {entry.title}",
                )
            )
            continue
        if parsed.model_name.casefold() != model.model_name.casefold():
            skipped.append(
                _skipped_artifact(
                    entry,
                    SkipReason.MISSING_IDENTITY,
                    f"固件标题型号 {parsed.model_name} 与目录型号 {model.model_name} 不一致",
                )
            )
            continue
        if not _is_download_url(entry.download_url):
            skipped.append(
                _skipped_artifact(
                    entry,
                    SkipReason.MISSING_IDENTITY,
                    f"固件 {entry.title} 缺少可信官方 ZIP 地址",
                )
            )
            continue

        normalized_revision = f"{parsed.region}-{parsed.hardware_revision}"
        revision_key = f"{product_key}/rev:{normalized_revision.casefold()}"
        release_key = _derived_key(
            "release",
            product_key,
            revision_key,
            parsed.version_normalized.casefold(),
        )
        artifact_key = _derived_key("artifact", release_key, "firmware")
        artifact = FirmwareArtifactCandidate(
            source_key=artifact_key,
            artifact_type=ArtifactType.FIRMWARE,
            original_filename=_filename(entry.download_url),
            download_url=entry.download_url,
            url_expires_at=None,
            advertised_size=_size_to_bytes(entry.size_text),
            media_type="application/zip",
            official_checksum=None,
        )
        release = FirmwareReleaseCandidate(
            source_key=release_key,
            version_raw=parsed.version_raw,
            version_normalized=parsed.version_normalized,
            release_date=_parse_date(entry.publish_date_text),
            title=entry.title,
            release_notes=_release_notes(entry),
            release_notes_url=(
                entry.release_notes_url if _is_download_url(entry.release_notes_url) else None
            ),
            source_url=product_url,
            artifacts=(artifact,),
        )
        revisions.setdefault(revision_key, []).append(release)
        revision_raw[revision_key] = normalized_revision

    if not revisions:
        return None, tuple(skipped)

    hardware_candidates = tuple(
        HardwareRevisionCandidate(
            source_key=revision_key,
            raw_revision=revision_raw[revision_key],
            normalized_revision=revision_raw[revision_key],
            revision_explicit=True,
            source_url=product_url,
            releases=tuple(
                sorted(releases, key=lambda item: (item.release_date or date.min, item.source_key))
            ),
        )
        for revision_key, releases in sorted(revisions.items())
    )
    return (
        ProductCandidate(
            source_key=product_key,
            display_name=model.model_name,
            model_raw=model.model_name,
            model_normalized=model.model_name.upper(),
            series=classification.source_category,
            product_family=classification.family,
            product_type=classification.product_type,
            source_category=classification.source_category,
            source_url=product_url,
            hardware_revisions=hardware_candidates,
        ),
        tuple(skipped),
    )


def _root_type_id(data: object) -> int:
    if not isinstance(data, dict) or data.get("errorCode") != 0:
        raise ValueError("Omada 产品树接口返回失败")
    result = data.get("result")
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        raise ValueError("Omada 产品树缺少根节点")
    root_type_id = result[0].get("typeId")
    if not isinstance(root_type_id, int):
        raise ValueError("Omada 产品树根节点缺少 typeId")
    return root_type_id


def _ensure_source_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != _SOURCE_HOST:
        raise ValueError(f"Omada API 重定向到来源外: {url}")


def _is_download_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlsplit(url)
    return parsed.scheme == "https" and (parsed.hostname or "").casefold() == _DOWNLOAD_HOST


def _product_source_key(model_id: int) -> str:
    return f"model-id:{model_id}"


def _product_url(model_name: str) -> str:
    slug = quote("-".join(model_name.lower().split()), safe="-")
    return f"{_FIRMWARE_INDEX_URL}{slug}/"


def _derived_key(kind: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join((kind, *parts)).encode()).hexdigest()
    return f"derived:v1:{digest}"


def _filename(url: str | None) -> str | None:
    if not url:
        return None
    return unquote(PurePosixPath(urlsplit(url).path).name) or None


def _size_to_bytes(size_text: str | None) -> int | None:
    if not size_text:
        return None
    matched = _SIZE_PATTERN.search(size_text)
    if matched is None:
        return None
    return int(float(matched.group(1)) * _SIZE_UNITS[matched.group(2).upper()])


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    matched = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", value.strip())
    if matched is None:
        return None
    try:
        return date(int(matched.group(3)), int(matched.group(1)), int(matched.group(2)))
    except ValueError:
        return None


def _release_notes(entry: FirmwareEntry) -> str | None:
    parts = [
        text
        for html in (entry.notes_html, entry.modifications_html)
        if (text := _html_to_text(html))
    ]
    return "\n\n".join(parts) or None


def _html_to_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextParser()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split())


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _skipped_artifact(
    entry: FirmwareEntry,
    reason: SkipReason,
    detail: str,
) -> SkippedCandidate:
    return SkippedCandidate(
        stage="artifact",
        reason_code=reason,
        detail=detail,
        source_url=entry.download_url,
        raw_hint=entry.title,
    )
