"""锐捷中国站固件适配器。

从锐捷官网固件下载中心采集路由器/无线等产品的固件元数据。

数据流：
  分类页 HTML → 提取产品 URL 列表 → 逐个产品页提取 goodsId
  → 调版本列表 API (POST /goods/new) → 并发调版本详情 API (/getDetail/{id})
  → 构建 ProductCandidate 树 → yield DiscoveredProduct

认证要求：需要 GW_ACCESS_TOKEN（浏览器登录后获取，约 8 小时有效）。

只采集路由器族（router/wireless_ap/mesh_router/home_router/cellular_cpe），
锐捷无摄像头产品，其他类型跳过。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
    ("/fw/rj-first-2321/", ProductType.ROUTER),  # 路由器
    ("/fw/rj-first-2320/", ProductType.WIRELESS_AP),  # 无线
    ("/fw/rj-first-2742/", ProductType.ROUTER),  # 家用路由器
    ("/fw/rj-first-2348/", ProductType.ROUTER),  # 网关
]

# 从分类页提取产品 URL 的正则
_PRODUCT_LINK_RE = re.compile(r'href="(/fw/rj-cp-[^"]+)"')

# 从产品页提取产品线 goodsId（<span goodsId="...">）
_GOODS_SPAN_RE = re.compile(r'<span[^>]*goodsId="(\d+)"[^>]*>([^<]+)</span>')

# 从产品页提取子型号 goodsId（<div class="...item..." goodsId="...">）
_GOODS_ITEM_RE = re.compile(
    r'<div[^>]*class="[^"]*item[^"]*"[^>]*goodsId="(\d+)"[^>]*>\s*([^<]+?)\s*</div>'
)

# 版本 API 每页条数
_VERSION_PAGE_SIZE = 50

# 同一版本列表页内同时获取详情的最大任务数
_DETAIL_MAX_CONCURRENCY = 4

# 移动路由器关键词（覆盖为 cellular_cpe）
_MOBILE_ROUTER_KW = ("移动", "5g", "4g", "3g", "nr", "lte")

# 非固件产品页面的 URL 特征（解决方案/软件/实验室等）
_NON_FIRMWARE_SLUGS = frozenset(
    {
        "sdwan",
        "srv6-solution",
        "5g-solution",
        "lyiowan",  # 解决方案
        "bros",
        "rg-rcms",
        "racc",
        "rcms",  # 应用软件
        "lbsys1",  # 实验室
        "3g",  # 过时的 3G 分类页
    }
)


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
                    "未配置锐捷登录 token。请执行 firmatlas auth ruijie-cn 查看获取指引。"
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

    async def refresh_artifact_url(self, request: ArtifactRefreshRequest) -> ArtifactRefreshResult:
        """获取/刷新 OSS 临时下载链接。

        两种调用路径：
        1. 首次下载：stale_url 为 pending:{file_id} 占位符
        2. 地址失效：stale_url 为已过期的 OSS URL，从 source_key 提取 file_id
        """
        if self._token_info is None:
            return ArtifactRefreshFailed(
                reason_code=RefreshFailureReason.SOURCE_ERROR,
                detail="未配置锐捷登录 token",
            )

        stale = request.stale_url

        # 路径 1：pending:{file_id} 占位符 → 直接从占位符提取 file_id
        if stale.startswith("pending:"):
            file_id_str = stale.split(":", 1)[1]
            if not file_id_str.isdigit():
                return ArtifactRefreshFailed(
                    reason_code=RefreshFailureReason.NOT_FOUND,
                    detail=f"无效的 pending URL 格式: {stale}",
                )
            file_id = file_id_str
        else:
            # 路径 2：从 artifact_source_key 提取 file_id
            # 格式: ruijie:{version_id}:{file_id}
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
            url_expires_at=datetime.now(UTC).replace(
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
                entries.append(_ProductEntry(url=f"{_BASE}{path}", default_type=default_type))

        if not entries:
            return None  # 表示完全无法访问
        return entries

    # ------------------------------------------------------------------
    # 内部：单个产品采集
    # ------------------------------------------------------------------

    async def _discover_product(self, entry: _ProductEntry) -> ProductCandidate | None:
        """从产品页面采集所有固件版本，组装为 ProductCandidate。

        新版页面结构：
        - <span goodsId="..."> 包含产品线名称和 goodsId
        - <div class="item" goodsId="..."> 包含子型号名称和 goodsId
        - 有子型号时为每个子型号创建独立 HardwareRevision
        - 无子型号时使用产品线 goodsId，创建单一 UNSPECIFIED HardwareRevision
        """
        # 1. 获取产品页 HTML
        try:
            fetched = await self._auth_get_text(entry.url)
        except Exception:
            return None

        html = fetched.text

        # 2. 提取产品线 goodsId 及名称
        span_match = _GOODS_SPAN_RE.search(html)
        if not span_match:
            return None
        product_line_gid = span_match.group(1)
        product_name = span_match.group(2).strip()

        # 3. 提取子型号 goodsId 列表
        raw_items = _GOODS_ITEM_RE.findall(html)
        model_items: list[tuple[str, str]] = []
        seen_gids: set[str] = set()
        for gid, name in raw_items:
            name = name.strip()
            if gid and name and gid not in seen_gids:
                seen_gids.add(gid)
                model_items.append((gid, name))

        # 4. 提取型号
        model = _extract_model(product_name)

        # 5. 获取版本并构建 HardwareRevision
        if model_items:
            # 有子型号：为每个子型号创建独立 HardwareRevision
            revisions: list[HardwareRevisionCandidate] = []
            for model_gid, model_name in model_items:
                releases = await self._get_releases_for_goods(model_gid, entry.url)
                if releases:
                    revisions.append(
                        HardwareRevisionCandidate(
                            source_key=f"ruijie:{_url_slug(entry.url)}:{model_gid}",
                            raw_revision=model_name,
                            normalized_revision=model_name,
                            revision_explicit=True,
                            source_url=entry.url,
                            releases=tuple(releases),
                        )
                    )
            if not revisions:
                return None
        else:
            # 无子型号：使用产品线 goodsId
            releases = await self._get_releases_for_goods(product_line_gid, entry.url)
            if not releases:
                return None
            revisions = [
                HardwareRevisionCandidate(
                    source_key=UNSPECIFIED_REVISION_SOURCE_KEY,
                    raw_revision=None,
                    normalized_revision=UNSPECIFIED_REVISION,
                    revision_explicit=False,
                    source_url=None,
                    releases=tuple(releases),
                )
            ]

        # 6. 分类
        family = ProductFamily.ROUTER
        ptype = _classify_product_type(product_name, entry.default_type)

        # 7. 组装候选树
        slug = _url_slug(entry.url)
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
            hardware_revisions=tuple(revisions),
        )

    # ------------------------------------------------------------------
    # 内部：版本列表 API → 版本详情 → FirmwareReleaseCandidate
    # ------------------------------------------------------------------

    async def _get_releases_for_goods(
        self, goods_id: str, source_url: str
    ) -> list[FirmwareReleaseCandidate]:
        """获取指定 goodsId 的所有固件版本（自动翻页）。"""
        releases: list[FirmwareReleaseCandidate] = []
        page = 1

        while True:
            data = await self._auth_post_json(
                f"{_BASE}/application/soft/version/goods/new",
                body={
                    "productId": goods_id,
                    "pageIndex": page,
                    "pageSize": _VERSION_PAGE_SIZE,
                    "versionAttr": "",
                    "versionStage": "",
                    "status": "",
                    "versionName": "",
                },
            )

            if not isinstance(data, dict) or data.get("code") != 200:
                break

            payload = data.get("data")
            if not isinstance(payload, dict):
                break

            records = payload.get("records", [])
            total = _parse_int(payload.get("total")) or 0

            releases.extend(await self._build_releases(records, source_url))

            if page * _VERSION_PAGE_SIZE >= total:
                break
            page += 1

        return releases

    async def _build_releases(
        self, records: Any, source_url: str
    ) -> list[FirmwareReleaseCandidate]:
        """有界并发构建一页版本详情，并保持版本列表的原始顺序。"""
        if not isinstance(records, list):
            return []

        semaphore = asyncio.Semaphore(_DETAIL_MAX_CONCURRENCY)

        async def build(record: dict[str, Any]) -> FirmwareReleaseCandidate | None:
            async with semaphore:
                return await self._build_release(record, source_url)

        tasks = [build(record) for record in records if isinstance(record, dict)]
        if not tasks:
            return []

        results = await asyncio.gather(*tasks)
        return [release for release in results if release is not None]

    async def _build_release(
        self, version_record: dict[str, Any], source_url: str
    ) -> FirmwareReleaseCandidate | None:
        """从 goods/new API 的版本记录构建 FirmwareReleaseCandidate。

        version_record 来自 POST /application/soft/version/goods/new 的 records 元素。
        再通过 GET /getDetail/{id} 获取文件列表；下载地址在实际下载前按需解析。
        """
        vid = version_record.get("id")
        if not vid:
            return None

        # 跳过不可下载的版本
        control_state = version_record.get("controlState", "")
        if control_state and control_state != "可下载":
            return None

        # 获取文件列表（旧 getDetail API 仍有效）
        try:
            detail = await self._auth_get_json(
                f"{_BASE}/application/soft/version/getDetail/{vid}?loadFile=true"
            )
        except Exception:
            return None

        if not isinstance(detail, dict) or detail.get("code") != 200:
            return None

        vd = detail.get("data")
        if not isinstance(vd, dict):
            return None

        file_list = vd.get("softFileList", [])
        if not isinstance(file_list, list) or not file_list:
            return None

        # 发布信息（优先用版本记录的字段，回退到详情接口字段）
        title = version_record.get("pageTitle") or vd.get("pageTitle") or ""
        publish_date = _parse_date(version_record.get("publishDate"))
        version_stage = version_record.get("versionStageStr") or ""
        status_str = version_record.get("statusStr") or ""

        # 构建发布说明
        notes_parts: list[str] = []
        if version_stage:
            notes_parts.append(f"版本阶段: {version_stage}")
        if status_str:
            notes_parts.append(f"状态: {status_str}")
        new_feature = version_record.get("newFeature", "")
        if new_feature and new_feature.strip():
            notes_parts.append(f"新功能: {new_feature.strip()}")

        # 构建 Artifact
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
            checksum = _parse_md5_base64(md5_raw)

            # 下载地址留占位符，下载时由 refresh_artifact_url 实时解析
            # （OSS 签名 URL 有效期约 1 小时，采集阶段获取毫无意义）
            download_url = f"pending:{file_id}"

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

        version_raw = title
        version_normalized = _normalize_version(version_raw)

        return FirmwareReleaseCandidate(
            source_key=f"ruijie:{vid}",
            version_raw=version_raw,
            version_normalized=version_normalized,
            release_date=publish_date,
            title=title,
            release_notes="; ".join(notes_parts) if notes_parts else None,
            release_notes_url=None,
            source_url=source_url,
            artifacts=tuple(artifacts),
        )

    # ------------------------------------------------------------------
    # 内部：下载地址获取
    # ------------------------------------------------------------------

    async def _get_download_url(self, file_id: int) -> str | None:
        """调 API 获取 OSS 临时下载地址。"""
        data = await self._auth_get_json(f"{_BASE}/application/soft/version/getDownUrl/{file_id}")
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
        result = await self._http.get_text(url, headers=self._auth_headers())
        return json.loads(result.text)

    async def _auth_post_json(self, url: str, *, body: dict[str, Any]) -> Any:
        """带认证的 JSON POST 请求，返回已解析的响应体。"""
        result = await self._http.post_json(url, body=body, headers=self._auth_headers())
        return result.data


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
    slug = parts[-1]
    if slug.startswith("rj-cp-"):
        slug = slug[6:]
    return slug


def _extract_model(product_name: str) -> str:
    """从产品名称中提取型号（如 RG-RSR20-X1）。

    只匹配 ASCII 字母/数字/连字符，避免 \\w 匹配到中文字符。
    """
    m = re.match(r"(RG-[a-zA-Z0-9_-]+)", product_name)
    if m:
        return m.group(1)
    # 回退：取"系列"之前的部分
    return product_name.split("系列")[0].strip()


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
    m = re.search(r"(\d+\.\d+[^\s]*)", raw)
    if m:
        v = m.group(1)
        v = v.replace("(", ".").replace(")", ".")
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
