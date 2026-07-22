"""DrayTek 全球站适配器。

从 fw.draytek.com.tw Apache 目录列表发现 Router 和 AP 固件，
不依赖被 Cloudflare 保护的 www.draytek.com。

## 数据流

FTP 根目录 → 过滤 Vigor 产品目录 → 读 Firmware/ 目录
→ 读 latest.txt / latest_stable.txt → 进入版本目录
→ 确认 .zip 存在 + 解析 FIRMWARE.DIGESTS → 构造下载 URL
→ 产出 DiscoveredProduct

## Channel 处理

- latest.txt → 主 channel（无标签）
- latest_stable.txt → "stable" channel
- 同一产品最多产出两个 FirmwareRelease（每个 channel 一个）

## 关键约束

- 适配器不访问数据库、不自建 HTTP 客户端、不下载固件
- 硬件版本固定为 "unspecified"（FTP 不暴露硬件版本信息）
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from firmatlas.adapters.draytek_global.classification import Classification, classify
from firmatlas.adapters.draytek_global.directory_parser import (
    DirectoryEntry,
    parse_directory_listing,
)
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
from firmatlas.domain.model import ArtifactType, OfficialChecksum
from firmatlas.infra.http_client import FetchError, HttpFetcher

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_ROOT_URL = "https://fw.draytek.com.tw/"

# FIRMWARE.DIGESTS 文件解析正则
# 格式: md5 sha1 .\filename
_DIGESTS_LINE = re.compile(
    r"^([0-9a-fA-F]{32})\s+([0-9a-fA-F]{40})\s+\.\\(.+)$"
)

# 固件版本号的规范化：去掉前导 v/V
_VERSION_CLEAN = re.compile(r"^[vV]")


# ---------------------------------------------------------------------------
# source_key 生成
# ---------------------------------------------------------------------------


def _make_product_source_key(dir_name: str) -> str:
    """Product source_key 基于 FTP 目录名的稳定身份（大小写不敏感）。"""
    return f"draytek-ftp:{dir_name.strip().casefold()}"


def _make_release_source_key(
    product_source_key: str, version: str, channel_label: str
) -> str:
    """Release source_key 组合产品 + 版本 + channel。"""
    base = f"{product_source_key}/fw/{version}"
    if channel_label:
        base = f"{base}/{channel_label}"
    return base


def _make_artifact_source_key(dir_name: str, version: str, filename: str) -> str:
    """Artifact source_key 基于 FTP 路径和实际文件名的稳定身份（保留原始大小写）。"""
    path = f"{dir_name}/Firmware/v{version}/{filename}"
    return f"draytek-ftp:{path}"


# ---------------------------------------------------------------------------
# URL 构造
# ---------------------------------------------------------------------------


def _product_page_url(dir_name: str) -> str:
    """从 FTP 目录名推导产品页面 URL。

    "Vigor2767" → "https://www.draytek.com/products/vigor2767/"
    "VigorAP 905" → "https://www.draytek.com/products/vigorap-905/"
    "Vigor C410" → "https://www.draytek.com/products/vigor-c410/"
    """
    slug = dir_name.strip().lower().replace(" ", "-")
    return f"https://www.draytek.com/products/{slug}/"


def _clean_version(raw: str) -> str:
    """规范化版本号：去空白、去前导 v/V。"""
    return _VERSION_CLEAN.sub("", raw.strip())


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


class DraytekGlobalAdapter:
    """DrayTek 全球站适配器。

    从 fw.draytek.com.tw Apache 目录列表发现固件元数据。
    """

    source_key = "draytek-global"

    def __init__(self, http: HttpFetcher) -> None:
        self._http = http

    # -- discover -------------------------------------------------------

    async def discover(self) -> AsyncIterator[DiscoveryEvent]:
        """异步生成器，从 FTP 目录树发现固件，逐产品产出 DiscoveredProduct。"""
        issues: list[AdapterIssueSummary] = []
        failures: list[str] = []

        # 1. 获取 FTP 根目录
        try:
            root_fetched = await self._http.get_text(_ROOT_URL)
        except FetchError as exc:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason=f"无法访问 DrayTek FTP 根目录: {exc}",
                issues=(),
            )
            return

        root_entries = parse_directory_listing(root_fetched.text, root_fetched.url)
        if not root_entries:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="DrayTek FTP 根目录解析为空",
                issues=(),
            )
            return

        # 2. 分类并筛选产品目录
        targets: list[tuple[DirectoryEntry, Classification]] = []
        non_target_count = 0

        for entry in root_entries:
            if not entry.is_directory:
                continue
            classification = classify(entry.name)
            if classification is None:
                non_target_count += 1
                continue
            targets.append((entry, classification))

        if non_target_count:
            yield SkippedCandidate(
                stage="product",
                reason_code=SkipReason.UNMAPPED_TYPE,
                detail=f"非目标产品共 {non_target_count} 项（非 Vigor 前缀或交换机等）",
                source_url=_ROOT_URL,
                raw_hint="non_target_directories",
            )

        if not targets:
            yield DiscoveryCompleted(
                is_complete=True,
                incomplete_reason=None,
                issues=tuple(issues),
            )
            return

        # 3. 逐产品处理
        discovered_count = 0
        for entry, classification in targets:
            try:
                candidate = await self._discover_product(entry, classification)
            except FetchError as exc:
                failures.append(f"{entry.name}: {exc}")
                continue

            if candidate is None:
                continue

            discovered_count += 1
            yield DiscoveredProduct(product=candidate)

        # 4. 产出 issues
        if failures:
            issues.append(
                AdapterIssueSummary(
                    code="product_fetch_error",
                    detail=(
                        f"{len(failures)} 个产品目录请求或解析失败；"
                        f"示例: {'; '.join(failures[:3])}"
                    ),
                    source_url=_ROOT_URL,
                )
            )

        yield DiscoveryCompleted(
            is_complete=not failures,
            incomplete_reason=(
                f"{len(failures)} 个产品处理失败" if failures else None
            ),
            issues=tuple(issues),
        )

    async def _discover_product(
        self,
        entry: DirectoryEntry,
        classification: Classification,
    ) -> ProductCandidate | None:
        """处理单个产品目录，返回 ProductCandidate 或 None。"""
        dir_name = entry.name
        product_key = _make_product_source_key(dir_name)

        # 获取 Firmware/ 目录
        fw_dir_url = f"{entry.url}Firmware/"
        fw_html = await self._http.get_text(fw_dir_url)
        fw_entries = parse_directory_listing(fw_html.text, fw_html.url)

        # 收集 (channel_label, version) 列表
        channels: list[tuple[str, str]] = []

        # mainline channel: 读 latest.txt
        latest_version = await self._read_version_file("latest.txt", fw_entries)
        if latest_version:
            channels.append(("", latest_version))

        # stable channel: 读 latest_stable.txt（如果存在）
        stable_version = await self._read_version_file(
            "latest_stable.txt", fw_entries
        )
        if stable_version and stable_version != latest_version:
            channels.append(("stable", stable_version))

        if not channels:
            return None

        # 处理每个 channel → 构建 FirmwareRelease
        releases: list[FirmwareReleaseCandidate] = []
        for channel_label, version in channels:
            release = await self._build_release(
                dir_name, product_key, version, channel_label, entry.url
            )
            if release is not None:
                releases.append(release)

        if not releases:
            return None

        # 硬件版本固定 unspecified
        hw_candidate = HardwareRevisionCandidate(
            source_key=UNSPECIFIED_REVISION_SOURCE_KEY,
            raw_revision=None,
            normalized_revision=UNSPECIFIED_REVISION,
            revision_explicit=False,
            source_url=entry.url,
            releases=tuple(releases),
        )

        return ProductCandidate(
            source_key=product_key,
            display_name=dir_name,
            model_raw=dir_name,
            model_normalized=dir_name.strip().upper(),
            series=None,
            product_family=classification.family,
            product_type=classification.product_type,
            source_category=classification.source_category,
            source_url=_product_page_url(dir_name),
            hardware_revisions=(hw_candidate,),
        )

    async def _read_version_file(
        self,
        filename: str,
        fw_entries: tuple[DirectoryEntry, ...],
    ) -> str | None:
        """读取 latest.txt 或 latest_stable.txt 的内容。

        返回去空白后的版本号字符串（如 "5.4.0"），文件不存在或为空时返回 None。
        """
        file_entry = next(
            (e for e in fw_entries if not e.is_directory and e.name == filename),
            None,
        )
        if file_entry is None:
            return None

        try:
            fetched = await self._http.get_text(file_entry.url)
            version = fetched.text.strip()
            return version if version else None
        except FetchError:
            return None

    async def _build_release(
        self,
        dir_name: str,
        product_key: str,
        version: str,
        channel_label: str,
        product_dir_url: str,
    ) -> FirmwareReleaseCandidate | None:
        """为单个 (产品, 版本, channel) 构建 FirmwareReleaseCandidate。

        版本目录中可能有一个或多个 .zip 固件变体（如 STD/MDM1-7），
        每个变体生成独立的 FirmwareArtifactCandidate。
        """
        cleaned_version = _clean_version(version)
        version_dir_url = f"{product_dir_url}Firmware/v{cleaned_version}/"

        # 获取版本目录内容
        try:
            version_html = await self._http.get_text(version_dir_url)
        except FetchError:
            return None

        version_entries = parse_directory_listing(version_html.text, version_html.url)

        # 收集所有 .zip 固件文件（可能有多变体）
        zip_entries = [
            e for e in version_entries
            if not e.is_directory and e.name.lower().endswith(".zip")
        ]
        if not zip_entries:
            return None

        # 解析 FIRMWARE.DIGESTS，得到 filename → checksum 映射
        digests_map = await self._parse_digests(version_dir_url)

        # 查找 release note PDF
        release_notes_url: str | None = None
        pdf_entry = next(
            (e for e in version_entries
             if not e.is_directory and e.name.lower().endswith(".pdf")),
            None,
        )
        if pdf_entry is not None:
            release_notes_url = pdf_entry.url

        # 版本标题：显示 channel 信息
        title = f"{dir_name} Firmware {version}"
        if channel_label:
            title += f" ({channel_label})"

        # 为每个 zip 变体创建 artifact
        artifacts: list[FirmwareArtifactCandidate] = []
        for zip_entry in zip_entries:
            actual_filename = zip_entry.name
            checksum = digests_map.get(actual_filename.casefold())
            artifacts.append(
                FirmwareArtifactCandidate(
                    source_key=_make_artifact_source_key(
                        dir_name, cleaned_version, actual_filename
                    ),
                    artifact_type=ArtifactType.FIRMWARE,
                    original_filename=actual_filename,
                    download_url=zip_entry.url,
                    url_expires_at=None,
                    advertised_size=None,
                    media_type="application/zip",
                    official_checksum=checksum,
                )
            )

        release_key = _make_release_source_key(
            product_key, cleaned_version, channel_label
        )

        return FirmwareReleaseCandidate(
            source_key=release_key,
            version_raw=version,
            version_normalized=cleaned_version,
            release_date=None,  # FTP 目录日期不可靠，不录入
            title=title,
            release_notes=None,
            release_notes_url=release_notes_url,
            source_url=version_dir_url,
            artifacts=tuple(artifacts),
        )

    async def _parse_digests(
        self,
        version_dir_url: str,
    ) -> dict[str, OfficialChecksum]:
        """解析 FIRMWARE.DIGESTS 文件，返回 {casefold 文件名: OfficialChecksum} 映射。

        FIRMWARE.DIGESTS 格式:
            //
            // File Checksum Integrity Verifier version 2.05.
            //
            MD5	SHA-1
            --------------------------------------------------------
            <md5> <sha1> .\\filename1
            <md5> <sha1> .\\filename2
        """
        digests_url = f"{version_dir_url}FIRMWARE.DIGESTS"
        try:
            fetched = await self._http.get_text(digests_url)
        except FetchError:
            return {}

        result: dict[str, OfficialChecksum] = {}
        for line in fetched.text.splitlines():
            m = _DIGESTS_LINE.match(line.strip())
            if m is None:
                continue
            sha1 = m.group(2).lower()
            filename = m.group(3).strip().casefold()
            result[filename] = OfficialChecksum(algorithm="sha1", value=sha1)

        return result

    # -- refresh_artifact_url --------------------------------------------

    async def refresh_artifact_url(
        self, request: ArtifactRefreshRequest
    ) -> ArtifactRefreshResult:
        """按产品名重新遍历 FTP，找回同一 Artifact 的最新下载地址。

        DrayTek 的 Artifact source_key 格式为:
            draytek-ftp:{dir_name}/Firmware/v{version}/{filename}

        从中解析 dir_name、version 和 filename，确认身份一致后返回当前 URL。
        """
        # 解析 source_key
        artifact_path = _parse_artifact_source_key(request.artifact_source_key)
        if artifact_path is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=f"无法解析 Artifact source_key: {request.artifact_source_key}",
            )

        dir_name = artifact_path[0]
        known_version = artifact_path[1]
        known_filename = artifact_path[2]

        # 构造产品 FTP URL
        encoded_dir = dir_name.replace(" ", "%20")
        product_url = f"https://fw.draytek.com.tw/{encoded_dir}/"

        # 验证 product_source_key 匹配
        expected_product_key = _make_product_source_key(dir_name)
        if request.product_source_key != expected_product_key:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.IDENTITY_CONFLICT,
                detail=(
                    f"Artifact 的产品归属 {dir_name!r} 与请求中的"
                    f" product_source_key {request.product_source_key!r} 不一致"
                ),
            )

        # 重新获取 Firmware/ 目录
        try:
            fw_dir_url = f"{product_url}Firmware/"
            fw_html = await self._http.get_text(fw_dir_url)
            fw_entries = parse_directory_listing(fw_html.text, fw_html.url)
        except FetchError as exc:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.SOURCE_ERROR,
                detail=f"访问 Firmware 目录失败: {exc}",
            )

        # 检查已知版本目录是否仍存在
        version_dir_name = f"v{known_version}"
        version_exists = any(
            e.is_directory and e.name == version_dir_name
            for e in fw_entries
        )

        if not version_exists:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.NOT_FOUND,
                detail=f"版本目录 v{known_version} 已不存在，该固件可能已下架",
            )

        # 构造当前下载 URL（使用实际文件名）
        encoded_filename = known_filename.replace(" ", "%20")
        download_url = (
            f"https://fw.draytek.com.tw/{encoded_dir}/"
            f"Firmware/v{known_version}/{encoded_filename}"
        )
        return ArtifactUrlRefreshed(
            download_url=download_url,
            url_expires_at=None,
        )


def _parse_artifact_source_key(source_key: str) -> tuple[str, str, str] | None:
    """从 artifact source_key 解析出 (dir_name, version, filename)。

    source_key 格式: draytek-ftp:{dir_name}/Firmware/v{version}/{filename}
    示例: draytek-ftp:Vigor2866/Firmware/v4.5.3/Vigor2866_v4.5.3_STD.zip
    """
    prefix = "draytek-ftp:"
    if not source_key.startswith(prefix):
        return None

    path = source_key[len(prefix):]
    # 格式: {dir_name}/Firmware/v{version}/{filename}
    parts = path.split("/")
    if len(parts) < 4:
        return None

    dir_name = parts[0]
    version_dir = parts[2]  # "v5.4.0"

    version = _VERSION_CLEAN.sub("", version_dir)
    if not version:
        return None

    filename = parts[3] if len(parts) >= 4 else ""
    return dir_name, version, filename
