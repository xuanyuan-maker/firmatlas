"""D-Link 美国支持站官方资源目录适配器。"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from firmatlas.adapters.dlink_us.classification import Classification, classify
from firmatlas.adapters.dlink_us.directory_parser import DirectoryEntry, parse_directory_listing
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

_INDEX_URL = "https://support.dlink.com/resource/PRODUCTS/"
_EXPECTED_HOST = "support.dlink.com"
_MAX_PRODUCT_DEPTH = 3

_FIRMWARE_EXTENSIONS = {
    ".7z",
    ".bin",
    ".bz2",
    ".dlf",
    ".fw",
    ".gz",
    ".hex",
    ".img",
    ".rar",
    ".tar",
    ".xz",
    ".zip",
}
_NON_FIRMWARE_MARKERS = (
    "DATASHEET",
    "DRIVER",
    "GPL",
    "MANUAL",
    "QIG",
    "QUICK_INSTALL",
    "RELEASE_NOTE",
    "RELEASENOTE",
    "SOURCE_CODE",
)
_VERSION_PATTERN = re.compile(r"(?i)(?<![A-Z0-9])V?(\d+(?:\.(?:\d+|[A-Z]\d+))+(?:[A-Z]\d+)?)")
_REVISION_DIRECTORY_PATTERN = re.compile(r"(?i)^REV(?:ISION)?[_-]?([A-Z]\d*|\d+)$")


@dataclass(frozen=True)
class _FirmwareFile:
    name: str
    url: str
    listing_url: str
    raw_revision: str | None
    normalized_revision: str


@dataclass(frozen=True)
class _ProductDefinition:
    model_name: str
    product_url: str
    classification: Classification


@dataclass(frozen=True)
class _CollectedProduct:
    candidate: ProductCandidate | None
    failures: tuple[str, ...]
    unparsed_versions: tuple[str, ...]


class DlinkUsAdapter:
    """从公开资源目录发现白名单设备固件，不下载固件内容。"""

    source_key = "dlink-us"

    def __init__(self, http: HttpFetcher) -> None:
        self._http = http

    async def discover(self) -> AsyncIterator[DiscoveryEvent]:
        try:
            fetched = await self._http.get_text(_INDEX_URL)
        except Exception as exc:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason=f"请求 D-Link 产品目录失败: {exc}",
                issues=(),
            )
            return

        if not _is_index_url(fetched.url):
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason=f"D-Link 产品目录重定向到来源外: {fetched.url}",
                issues=(),
            )
            return

        entries = parse_directory_listing(fetched.text, fetched.url)
        if not entries:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="D-Link 产品目录没有解析到任何条目",
                issues=(),
            )
            return

        targets: list[_ProductDefinition] = []
        excluded_count = 0
        for entry in entries:
            classification = classify(entry.name)
            if classification is None or not entry.is_directory:
                excluded_count += 1
                continue
            if not _is_product_url(entry.url):
                excluded_count += 1
                continue
            targets.append(
                _ProductDefinition(
                    model_name=entry.name,
                    product_url=entry.url,
                    classification=classification,
                )
            )

        failures: list[str] = []
        unparsed_versions: list[str] = []
        without_firmware: list[str] = []
        discovered_count = 0

        for target in targets:
            collected = await self._collect_product(target)
            failures.extend(collected.failures)
            unparsed_versions.extend(collected.unparsed_versions)
            if collected.candidate is None:
                if not collected.failures:
                    without_firmware.append(target.model_name)
                continue
            discovered_count += 1
            yield DiscoveredProduct(product=collected.candidate)

        if excluded_count:
            yield SkippedCandidate(
                stage="product",
                reason_code=SkipReason.UNMAPPED_TYPE,
                detail=f"白名单外产品或非目录条目共 {excluded_count} 项",
                source_url=_INDEX_URL,
                raw_hint="non_target_products",
            )

        issues: list[AdapterIssueSummary] = []
        if unparsed_versions:
            issues.append(
                AdapterIssueSummary(
                    code="version_unparsed",
                    detail=(
                        f"{len(unparsed_versions)} 个固件文件未解析出规范版本，"
                        f"已保留原文件名；示例: {'; '.join(unparsed_versions[:3])}"
                    ),
                    source_url=_INDEX_URL,
                )
            )
        if without_firmware:
            issues.append(
                AdapterIssueSummary(
                    code="target_without_firmware",
                    detail=(
                        f"{len(without_firmware)} 个白名单产品当前没有发现固件；"
                        f"示例: {'; '.join(without_firmware[:3])}"
                    ),
                    source_url=_INDEX_URL,
                )
            )

        if not targets:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="D-Link 产品目录没有命中白名单型号",
                issues=tuple(issues),
            )
            return

        yield DiscoveryCompleted(
            is_complete=not failures,
            incomplete_reason=(
                f"{len(failures)} 个白名单产品目录请求或解析失败" if failures else None
            ),
            issues=tuple(issues),
        )

    async def refresh_artifact_url(self, request: ArtifactRefreshRequest) -> ArtifactRefreshResult:
        product_url = _product_url_from_source_key(request.product_source_key)
        if product_url is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=f"D-Link 产品身份不是受支持的资源路径: {request.product_source_key}",
            )

        model_name = unquote(PurePosixPath(urlsplit(product_url).path).name)
        classification = classify(model_name)
        if classification is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=f"D-Link 产品 {model_name} 已不在白名单内",
            )

        collected = await self._collect_product(
            _ProductDefinition(
                model_name=model_name,
                product_url=product_url,
                classification=classification,
            )
        )
        if collected.failures:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.SOURCE_ERROR,
                detail=f"刷新时产品目录读取失败: {collected.failures[0]}",
            )
        if collected.candidate is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.NOT_FOUND,
                detail=f"D-Link 产品 {model_name} 当前没有发现固件",
            )

        artifact_parent: tuple[str, str] | None = None
        for revision in collected.candidate.hardware_revisions:
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
            detail=f"D-Link 产品 {model_name} 当前未找到 Artifact {request.artifact_source_key}",
        )

    async def _collect_product(self, product: _ProductDefinition) -> _CollectedProduct:
        files: list[_FirmwareFile] = []
        failures: list[str] = []
        visited: set[str] = set()

        async def walk(directory_url: str, depth: int, in_firmware_directory: bool) -> None:
            canonical_url = _canonical_directory_url(directory_url)
            if canonical_url in visited:
                return
            visited.add(canonical_url)

            try:
                fetched = await self._http.get_text(directory_url)
            except Exception as exc:
                failures.append(f"{directory_url}: {exc}")
                return
            if not _is_within_product(fetched.url, product.product_url):
                failures.append(f"{directory_url} 重定向到产品目录外: {fetched.url}")
                return

            entries = parse_directory_listing(fetched.text, fetched.url)
            for entry in entries:
                if not _is_within_product(entry.url, product.product_url):
                    continue
                if entry.is_directory:
                    if depth < _MAX_PRODUCT_DEPTH and _should_descend(
                        entry.name,
                        in_firmware_directory=in_firmware_directory,
                    ):
                        await walk(
                            entry.url,
                            depth + 1,
                            in_firmware_directory=(
                                in_firmware_directory or _is_firmware_directory(entry.name)
                            ),
                        )
                    continue
                if not _is_firmware_file(entry, in_firmware_directory=in_firmware_directory):
                    continue
                raw_revision, normalized_revision = _revision_for_file(
                    product.model_name,
                    product.product_url,
                    fetched.url,
                    entry.name,
                )
                files.append(
                    _FirmwareFile(
                        name=entry.name,
                        url=entry.url,
                        listing_url=fetched.url,
                        raw_revision=raw_revision,
                        normalized_revision=normalized_revision,
                    )
                )

        await walk(product.product_url, 0, False)
        if not files:
            return _CollectedProduct(
                candidate=None,
                failures=tuple(failures),
                unparsed_versions=(),
            )

        revisions: dict[str, list[_FirmwareFile]] = {}
        for firmware_file in files:
            revisions.setdefault(firmware_file.normalized_revision, []).append(firmware_file)

        hardware_candidates: list[HardwareRevisionCandidate] = []
        unparsed_versions: list[str] = []
        for normalized_revision, revision_files in sorted(revisions.items()):
            raw_revision = next(
                (item.raw_revision for item in revision_files if item.raw_revision is not None),
                None,
            )
            revision_source_key = (
                UNSPECIFIED_REVISION_SOURCE_KEY
                if normalized_revision == UNSPECIFIED_REVISION
                else f"rev:{normalized_revision.casefold()}"
            )
            releases: list[FirmwareReleaseCandidate] = []
            for firmware_file in sorted(revision_files, key=lambda item: item.url.casefold()):
                version_raw, version_normalized = _firmware_version(firmware_file.name)
                if version_normalized is None:
                    unparsed_versions.append(firmware_file.name)
                artifact_source_key = _url_path_source_key(firmware_file.url)
                release_source_key = _release_source_key(
                    product_source_key=_url_path_source_key(product.product_url),
                    revision_source_key=revision_source_key,
                    version_raw=version_raw,
                    artifact_source_key=artifact_source_key,
                )
                artifact = FirmwareArtifactCandidate(
                    source_key=artifact_source_key,
                    artifact_type=ArtifactType.FIRMWARE,
                    original_filename=firmware_file.name,
                    download_url=firmware_file.url,
                    url_expires_at=None,
                    advertised_size=None,
                    media_type=None,
                    official_checksum=None,
                )
                releases.append(
                    FirmwareReleaseCandidate(
                        source_key=release_source_key,
                        version_raw=version_raw,
                        version_normalized=version_normalized,
                        release_date=None,
                        title=firmware_file.name,
                        release_notes=None,
                        release_notes_url=None,
                        source_url=firmware_file.listing_url,
                        artifacts=(artifact,),
                    )
                )

            hardware_candidates.append(
                HardwareRevisionCandidate(
                    source_key=revision_source_key,
                    raw_revision=raw_revision,
                    normalized_revision=normalized_revision,
                    revision_explicit=raw_revision is not None,
                    source_url=revision_files[0].listing_url,
                    releases=tuple(releases),
                )
            )

        return _CollectedProduct(
            candidate=ProductCandidate(
                source_key=_url_path_source_key(product.product_url),
                display_name=product.model_name,
                model_raw=product.model_name,
                model_normalized=product.model_name.upper(),
                series=product.classification.source_category,
                product_family=product.classification.family,
                product_type=product.classification.product_type,
                source_category=product.classification.source_category,
                source_url=product.product_url,
                hardware_revisions=tuple(hardware_candidates),
            ),
            failures=tuple(failures),
            unparsed_versions=tuple(unparsed_versions),
        )


def _is_index_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == _EXPECTED_HOST
        and parsed.path.rstrip("/").casefold() == "/resource/products"
    )


def _is_product_url(url: str) -> bool:
    parsed = urlsplit(url)
    parts = PurePosixPath(unquote(parsed.path)).parts
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == _EXPECTED_HOST
        and len(parts) == 4
        and parts[1].casefold() == "resource"
        and parts[2].casefold() == "products"
        and parsed.path.endswith("/")
    )


def _is_within_product(url: str, product_url: str) -> bool:
    parsed = urlsplit(url)
    product = urlsplit(product_url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != _EXPECTED_HOST:
        return False
    normalized_path = posixpath.normpath(unquote(parsed.path)).casefold()
    product_path = posixpath.normpath(unquote(product.path)).casefold()
    return normalized_path == product_path or normalized_path.startswith(f"{product_path}/")


def _canonical_directory_url(url: str) -> str:
    parsed = urlsplit(url)
    return f"https://{_EXPECTED_HOST}{posixpath.normpath(unquote(parsed.path)).casefold()}/"


def _is_firmware_directory(name: str) -> bool:
    return "FIRMWARE" in name.upper() or name.upper() == "FW"


def _should_descend(name: str, *, in_firmware_directory: bool) -> bool:
    normalized = name.strip().upper()
    if in_firmware_directory:
        return True
    if _is_firmware_directory(normalized):
        return True
    if _REVISION_DIRECTORY_PATTERN.fullmatch(normalized):
        return True
    return bool(re.fullmatch(r"(?:HW)?[A-Z]\d+|V\d+", normalized))


def _is_firmware_file(entry: DirectoryEntry, *, in_firmware_directory: bool) -> bool:
    name_upper = entry.name.upper()
    suffix = PurePosixPath(urlsplit(entry.url).path).suffix.lower()
    if suffix not in _FIRMWARE_EXTENSIONS:
        return False
    if any(marker in name_upper for marker in _NON_FIRMWARE_MARKERS):
        return False
    if in_firmware_directory:
        return True
    return (
        "FIRMWARE" in name_upper
        or "HOTFIX" in name_upper
        or bool(re.search(r"(?:^|[_-])FW(?:[_-]?V?\d|[_-])", name_upper))
    )


def _revision_for_file(
    model_name: str,
    product_url: str,
    listing_url: str,
    filename: str,
) -> tuple[str | None, str]:
    product_path = PurePosixPath(unquote(urlsplit(product_url).path))
    listing_path = PurePosixPath(unquote(urlsplit(listing_url).path))
    relative_parts = listing_path.parts[len(product_path.parts) :]
    for part in relative_parts:
        matched = _REVISION_DIRECTORY_PATTERN.fullmatch(part)
        if matched:
            raw = matched.group(1).upper()
            return raw, _normalize_revision(raw)

    model_pattern = re.escape(model_name).replace(r"\-", "[-_]")
    matched = re.search(
        rf"(?i){model_pattern}[-_](?:REV)?([A-Z]\d*)[-_]",
        filename,
    )
    if matched:
        raw = matched.group(1).upper()
        return raw, _normalize_revision(raw)
    return None, UNSPECIFIED_REVISION


def _normalize_revision(raw_revision: str) -> str:
    normalized = raw_revision.strip().upper()
    if re.fullmatch(r"[A-Z]\d+", normalized):
        return normalized[0]
    return normalized


def _firmware_version(filename: str) -> tuple[str, str | None]:
    matched = _VERSION_PATTERN.search(filename)
    if matched is None:
        return filename, None
    raw = matched.group(0)
    normalized = matched.group(1).upper()
    return raw, normalized


def _url_path_source_key(url: str) -> str:
    path = posixpath.normpath(unquote(urlsplit(url).path)).casefold().lstrip("/")
    return f"url-path:{path}"


def _release_source_key(
    *,
    product_source_key: str,
    revision_source_key: str,
    version_raw: str,
    artifact_source_key: str,
) -> str:
    payload = "\n".join(
        (product_source_key, revision_source_key, version_raw, artifact_source_key)
    ).encode()
    return f"derived:v1:{hashlib.sha256(payload).hexdigest()}"


def _product_url_from_source_key(source_key: str) -> str | None:
    prefix = "url-path:"
    if not source_key.startswith(prefix):
        return None
    path = source_key.removeprefix(prefix).strip("/")
    url = f"https://{_EXPECTED_HOST}/{path}/"
    return url if _is_product_url(url) else None
