"""锐捷中国站固件适配器。

从锐捷官网固件下载中心采集路由器/无线等产品的固件元数据。

数据流：
  分类页 HTML → 提取产品 URL 列表 → 逐个产品页提取 VID 列表
  → 调版本详情 API (/getDetail/{vid}) → 调下载地址 API (/getDownUrl/{fileId})
  → 构建 ProductCandidate 树 → yield DiscoveredProduct

认证要求：需要 GW_ACCESS_TOKEN（浏览器登录后获取，约 8 小时有效）。

只采集路由器族（router/wireless_ap/mesh_router/home_router/cellular_cpe），
锐捷无摄像头产品，其他类型跳过。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
)
from firmatlas.adapters.ruijie_cn.auth import TokenNotConfiguredError, load_token
from firmatlas.domain.candidates import (
    UNSPECIFIED_REVISION,
    UNSPECIFIED_REVISION_SOURCE_KEY,
    FirmwareArtifactCandidate,
    FirmwareReleaseCandidate,
    HardwareRevisionCandidate,
    ProductCandidate,
)
from firmatlas.domain.model import ArtifactType, OfficialChecksum, ProductFamily, ProductType
from firmatlas.infra.http_client import HttpFetcher

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_BASE = "https://www.ruijie.com.cn"

# 目标分类页及其默认产品类型
_CATEGORY_PAGES: list[tuple[str, ProductType]] = [
    ("/fw/rj-first-2321/", ProductType.ROUTER),         # 路由器
    ("/fw/rj-first-2320/", ProductType.WIRELESS_AP),     # 无线
    ("/fw/rj-first-2742/", ProductType.ROUTER),          # 家用路由器
    ("/fw/rj-first-2348/", ProductType.ROUTER),          # 网关
]

# 从分类页提取产品 URL 的正则
_PRODUCT_LINK_RE = re.compile(r'href="(/fw/rj-cp-[^"]+)"')

# 从产品页提取 VID 的正则
_ROW_HREF_RE = re.compile(r"""rowHref\s*\(\s*['"](\d+)['"]""")

# 移动路由器关键词（覆盖为 cellular_cpe）
_MOBILE_ROUTER_KW = ("移动", "5g", "4g", "3g", "nr", "lte")

# 非固件产品页面的 URL 特征（解决方案/软件/实验室等）
_NON_FIRMWARE_SLUGS = frozenset({
    "sdwan", "srv6-solution", "5g-solution", "lyiowan",  # 解决方案
    "bros", "rg-rcms", "racc", "rcms",                    # 应用软件
    "lbsys1",                                              # 实验室
    "3g",                                                  # 过时的 3G 分类页
})


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


class RuijieCnAdapter:
    """锐捷中国站固件适配器。

    支持 discover（采集元数据）和 refresh_artifact_url（下载地址刷新）。
    """

    source_key = "ruijie-cn"

    def __init__(self, http: HttpFetcher, data_dir: Path | None = None) -> None:
        self._http = http
        try:
            self._token_info = load_token(data_dir)
        except TokenNotConfiguredError:
            self._token_info = None

    # ------------------------------------------------------------------
    # discover()
    # ------------------------------------------------------------------

    async def discover(self) -> AsyncIterator[DiscoveryEvent]:
        """采集固件元数据。"""
        if self._token_info is None:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason=(
                    "未配置锐捷登录 token。"
                    "请执行 firmatlas auth ruijie-cn 查看获取指引。"
                ),
                issues=(),
            )
            return

        issues: list[AdapterIssueSummary] = []
        product_failures = 0
        discovered = 0

        # 1. 从各分类页收集产品 URL（去重）
        product_entries = await self._collect_product_entries()
        if product_entries is None:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="无法访问锐捷分类页面（token 可能已过期或网络错误）",
                issues=(),
            )
            return

        # 2. 逐个产品采集
        for entry in product_entries:
            try:
                candidate = await self._discover_product(entry)
            except Exception as exc:
                product_failures += 1
                issues.append(
                    AdapterIssueSummary(
                        code="product_error",
                        detail=f"产品 {entry.url} 采集失败: {exc}",
                        source_url=entry.url,
                    )
                )
                continue

            if candidate is not None:
                discovered += 1
                yield DiscoveredProduct(product=candidate)

        incomplete_reason = None
        if product_failures > 0:
            incomplete_reason = f"{product_failures} 个产品采集失败"

        yield DiscoveryCompleted(
            is_complete=(product_failures == 0),
            incomplete_reason=incomplete_reason,
            issues=tuple(issues),
        )

    # ------------------------------------------------------------------
    # refresh_artifact_url()
    # ------------------------------------------------------------------

    async def refresh_artifact_url(
        self, request: ArtifactRefreshRequest
    ) -> ArtifactRefreshResult:
        """下载地址失效时重新获取 OSS 临时链接。

        artifact_source_key 格式: ruijie:{vid}:{file_id}
        从 source_key 中提取 file_id 重新调用 getDownUrl API。
        """
        if self._token_info is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.SOURCE_ERROR,
                detail="未配置锐捷登录 token",
            )

        # 从 artifact_source_key 提取 file_id
        parts = request.artifact_source_key.split(":")
        if len(parts) < 3 or not parts[-1].isdigit():
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.NOT_FOUND,
                detail=f"无法从 source_key 解析 file_id: {request.artifact_source_key}",
            )
        file_id = parts[-1]

        try:
            url = await self._get_download_url(int(file_id))
        except Exception as exc:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.SOURCE_ERROR,
                detail=f"获取下载地址失败: {exc}",
            )

        if url is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.NOT_FOUND,
                detail=f"file_id {file_id} 无法获取下载地址",
            )

        return ArtifactUrlRefreshed(
            download_url=url,
            url_expires_at=datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ),  # OSS URL 有效期约 1 小时，标记为当天过期
        )

    @property
    def has_token(self) -> bool:
        """是否已配置 token。"""
        return self._token_info is not None

    # ------------------------------------------------------------------
    # 内部：分类页 → 产品 URL 收集
    # ------------------------------------------------------------------

    async def _collect_product_entries(self) -> list[_ProductEntry] | None:
        """从各分类页收集产品页面 URL（去重，过滤非固件产品）。"""
        seen: set[str] = set()
        entries: list[_ProductEntry] = []

        for cat_url, default_type in _CATEGORY_PAGES:
            try:
                fetched = await self._auth_get_text(f"{_BASE}{cat_url}")
            except Exception:
                continue  # 单个分类页失败不中断全部

            for match in _PRODUCT_LINK_RE.finditer(fetched.text):
                path = match.group(1)
                if path in seen:
                    continue

                slug = path.rsplit("/", 2)[-2] if path.endswith("/") else path.rsplit("/", 1)[-1]
                slug = slug.lower().lstrip("/")
                if slug in _NON_FIRMWARE_SLUGS:
                    continue

                seen.add(path)
                entries.append(
                    _ProductEntry(url=f"{_BASE}{path}", default_type=default_type)
                )

        if not entries:
            return None  # 表示完全无法访问
        return entries

    # ------------------------------------------------------------------
    # 内部：单个产品采集
    # ------------------------------------------------------------------

    async def _discover_product(self, entry: _ProductEntry) -> ProductCandidate | None:
        """从产品页面采集所有固件版本，组装为 ProductCandidate。"""
        # 1. 获取产品页 HTML
        try:
            fetched = await self._auth_get_text(entry.url)
        except Exception:
            return None

        html = fetched.text

        # 2. 提取产品名称
        title_match = re.search(r"<title>(.*?)</title>", html)
        page_title = title_match.group(1) if title_match else ""
        # 标题格式："RG-RSR20-X1系列接入路由器软件下载-锐捷网络"
        product_name = page_title.replace("软件下载-锐捷网络", "").strip()
        # 提取型号（系列名，如 "RG-RSR20-X1系列接入路由器" → "RG-RSR20-X1"）
        model = _extract_model(product_name)

        # 3. 提取所有 VID
        vids: list[tuple[str, str]] = []  # [(vid, version_name)]
        for match in _ROW_HREF_RE.finditer(html):
            vid = match.group(1)
            # 版本名称从 rowHref 第二个参数或后续文本提取
            name = _extract_version_name(html, match.end(), vid)
            vids.append((vid, name))

        if not vids:
            return None  # 无固件版本

        # 4. 逐个 VID 获取详情
        releases: list[FirmwareReleaseCandidate] = []
        for vid, vname in vids:
            try:
                release = await self._build_release(vid, vname, entry.url)
            except Exception:
                continue
            if release is not None:
                releases.append(release)

        if not releases:
            return None

        # 5. 分类
        family = ProductFamily.ROUTER
        ptype = _classify_product_type(product_name, entry.default_type)

        # 6. 组装候选树
        slug = _url_slug(entry.url)
        revision = HardwareRevisionCandidate(
            source_key=UNSPECIFIED_REVISION_SOURCE_KEY,
            raw_revision=None,
            normalized_revision=UNSPECIFIED_REVISION,
            revision_explicit=False,
            source_url=None,
            releases=tuple(releases),
        )

        return ProductCandidate(
            source_key=f"ruijie:{slug}",
            display_name=product_name,
            model_raw=product_name,
            model_normalized=model.upper() if model else slug.upper(),
            series=None,
            product_family=family,
            product_type=ptype,
            source_category=None,
            source_url=entry.url,
            hardware_revisions=(revision,),
        )

    # ------------------------------------------------------------------
    # 内部：版本详情 → FirmwareReleaseCandidate
    # ------------------------------------------------------------------

    async def _build_release(
        self, vid: str, version_name: str, source_url: str
    ) -> FirmwareReleaseCandidate | None:
        """调 API 获取版本详情，构建 FirmwareReleaseCandidate。"""
        detail = await self._auth_get_json(
            f"{_BASE}/application/soft/version/getDetail/{vid}?loadFile=true"
        )

        if not isinstance(detail, dict) or detail.get("code") != 200:
            return None

        vd = detail.get("data")
        if not isinstance(vd, dict):
            return None

        file_list = vd.get("softFileList", [])
        if not isinstance(file_list, list) or not file_list:
            return None

        # 发布信息
        title = vd.get("pageTitle", version_name) or version_name
        publish_date = _parse_date(vd.get("publishDate"))
        version_stage = vd.get("versionStageStr", "")

        artifacts: list[FirmwareArtifactCandidate] = []
        for fi in file_list:
            if not isinstance(fi, dict):
                continue

            file_id = fi.get("id")
            if file_id is None:
                continue

            filename = fi.get("filename") or None
            size = _parse_int(fi.get("size"))
            md5_raw = fi.get("md5")

            # 解析 MD5（Base64 编码 → hex）
            checksum = _parse_md5_base64(md5_raw)

            # 获取下载地址
            try:
                download_url = await self._get_download_url(int(file_id))
            except Exception:
                download_url = None

            if download_url is None:
                continue

            artifact = FirmwareArtifactCandidate(
                source_key=f"ruijie:{vid}:{file_id}",
                artifact_type=ArtifactType.FIRMWARE,
                original_filename=filename,
                download_url=download_url,
                url_expires_at=None,
                advertised_size=size,
                media_type="application/octet-stream",
                official_checksum=checksum,
            )
            artifacts.append(artifact)

        if not artifacts:
            return None

        version_raw = version_name or title
        version_normalized = _normalize_version(version_raw)

        return FirmwareReleaseCandidate(
            source_key=f"ruijie:{vid}",
            version_raw=version_raw,
            version_normalized=version_normalized,
            release_date=publish_date,
            title=title,
            release_notes=f"版本阶段: {version_stage}" if version_stage else None,
            release_notes_url=None,
            source_url=source_url,
            artifacts=tuple(artifacts),
        )

    # ------------------------------------------------------------------
    # 内部：下载地址获取
    # ------------------------------------------------------------------

    async def _get_download_url(self, file_id: int) -> str | None:
        """调 API 获取 OSS 临时下载地址。"""
        data = await self._auth_get_json(
            f"{_BASE}/application/soft/version/getDownUrl/{file_id}"
        )
        if isinstance(data, dict) and data.get("code") == 200:
            url = data.get("data")
            if isinstance(url, str) and url:
                return url
        return None

    # ------------------------------------------------------------------
    # 内部：带认证的 HTTP 请求
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """构建带 token 的请求头。"""
        token = self._token_info.token if self._token_info else ""
        return {
            "Cookie": f"GW_ACCESS_TOKEN={token}; xp-Admin-Token={token}",
            "X-Requested-With": "XMLHttpRequest",
        }

    async def _auth_get_text(self, url: str) -> Any:
        """带认证的 GET 文本请求。"""
        return await self._http.get_text(url, headers=self._auth_headers())

    async def _auth_get_json(self, url: str) -> Any:
        """带认证的 GET JSON 请求，返回已解析的 dict。"""
        import json

        result = await self._http.get_text(url, headers=self._auth_headers())
        return json.loads(result.text)


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------


@dataclass
class _ProductEntry:
    """分类页中一条产品链接。"""

    url: str
    default_type: ProductType


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------


def _url_slug(url: str) -> str:
    """从产品 URL 提取唯一标识符（路径最后一段）。

    /fw/rj-cp-rg-rsr-x1/ → rg-rsr-x1
    """
    path = urlsplit(url).path.strip("/")
    parts = path.split("/")
    # 最后一段就是 slug
    slug = parts[-1]
    # 去掉可能的前缀 rj-cp-
    if slug.startswith("rj-cp-"):
        slug = slug[6:]
    return slug


def _extract_model(product_name: str) -> str:
    """从产品名称中提取型号（如 RG-RSR20-X1）。

    只匹配 ASCII 字母/数字/连字符，避免 \\w 匹配到中文字符。
    """
    m = re.match(r"(RG-[a-zA-Z0-9_-]+)", product_name)
    return m.group(1) if m else product_name.split("系列")[0].strip()


def _extract_version_name(html: str, pos: int, vid: str) -> str:
    """从 rowHref 调用位置附近提取版本名称。

    rowHref('VID', '版本名称') — pos 指向 VID 引号之后，
    后续文本为 `, '版本名称')`，匹配逗号后的第二引号参数。
    """
    after = html[pos : pos + 200]
    m = re.search(r"""\s*,\s*['"]([^'"]+)['"]""", after)
    if m:
        return m.group(1)
    return vid


def _parse_date(raw: Any) -> date | None:
    """解析日期字符串（YYYY-MM-DD 或 YYYY/MM/DD）。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_int(raw: Any) -> int | None:
    """安全解析整数。"""
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _parse_md5_base64(raw: Any) -> OfficialChecksum | None:
    """解析 Base64 编码的 MD5（锐捷特有格式）。

    例: "abc123==" → 解码后得到二进制 → hex → "a1b2c3..."
    """
    import base64

    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        binary = base64.b64decode(raw.strip())
        hex_value = binary.hex().upper()
        return OfficialChecksum(algorithm="md5", value=hex_value)
    except Exception:
        return None


def _normalize_version(raw: str) -> str | None:
    """从版本原始文本提取可比较的版本号。

    例: "RGOS 11.0(5)B9P30" → "11.0.5.b9p30"
    """
    if not raw or not raw.strip():
        return None
    # 尝试直接匹配常见版本模式
    m = re.search(r"(\d+\.\d+[^\s]*)", raw)
    if m:
        v = m.group(1)
        # 替换括号为点
        v = v.replace("(", ".").replace(")", ".")
        # 去除末尾多余的点
        v = v.rstrip(".")
        return v.lower()
    return raw.lower()


def _classify_product_type(name: str, default_type: ProductType) -> ProductType:
    """根据产品名称细化产品类型。"""
    name_lower = name.lower()
    for kw in _MOBILE_ROUTER_KW:
        if kw in name_lower:
            return ProductType.CELLULAR_CPE
    return default_type
