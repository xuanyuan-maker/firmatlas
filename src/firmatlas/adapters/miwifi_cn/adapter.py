"""小米路由器 MiWiFi 下载页适配器。

从 index.json 提取产品清单，通过小米固件 API 获取最新固件信息。

数据流：
  index.json → 解析 downloadList → 提取 model 与 typeList 码
  → 逐个调用 /upgrade/log/latest API
  → 按 model 分组 → ProductCandidate 树
  → yield DiscoveredProduct

只采集最新固件，不采集历史版本。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import unquote, urlsplit

from firmatlas.adapters.events import (
    AdapterIssueSummary,
    DiscoveredProduct,
    DiscoveryCompleted,
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

_PAGE_URL = "https://www1.miwifi.com/miwifi_download.html"
_INDEX_URL = "https://www1.miwifi.com/statics/json/index.json"
_API_BASE = "https://api.miwifi.com/upgrade/log/latest"

# JSONP 包装匹配：jQuery123({...}); 或 callback({...});
_JSONP_RE = re.compile(r"^[\w$.]+\s*\((.+)\)\s*;?\s*$", re.DOTALL)

# Mesh 产品关键词
_MESH_KEYWORDS = ("mesh", "全屋")


class MiwifiCnAdapter:
    """小米路由器中国站固件适配器。

    从 index.json 提取产品清单，通过 API 获取最新固件信息。
    每个产品（model）可包含稳定版和开发版两个固件 Release。
    """

    source_key = "miwifi-cn"

    def __init__(self, http: HttpFetcher) -> None:
        self._http = http

    # ------------------------------------------------------------------
    # discover()
    # ------------------------------------------------------------------

    async def discover(self):
        """执行发现流程：解析 index.json → API 调用 → 构建候选树。"""

        # ── 步骤 1：获取 index.json，提取 model 列表 ──
        try:
            index_result = await self._http.get_text(_INDEX_URL)
            entries = _parse_download_list(index_result.text)
        except Exception as exc:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason=f"无法获取 index.json: {exc}",
                issues=(),
            )
            return

        if not entries:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="index.json 中未找到产品列表",
                issues=(),
            )
            return

        # ── 步骤 2：逐个调用 API，按 model 分组 ──
        products: dict[str, _ProductBuilder] = {}
        issues: list[AdapterIssueSummary] = []
        api_failures = 0

        for entry in entries:
            model = entry["model"]
            title = entry.get("title", "")

            type_codes = _type_codes_for_entry(entry)

            for type_code in type_codes:
                try:
                    data = await _fetch_firmware(self._http, type_code)
                except Exception as exc:
                    api_failures += 1
                    issues.append(
                        AdapterIssueSummary(
                            code="api_error",
                            detail=f"产品 {type_code} API 请求失败: {exc}",
                            source_url=f"{_API_BASE}?typeList={type_code}",
                        )
                    )
                    continue

                if data is None:
                    continue

                variant = _variant_from_type_code(type_code)

                if model not in products:
                    products[model] = _ProductBuilder(model=model, title=title)
                products[model].add_release(type_code, variant, data)

        # ── 步骤 3：产出事件 ──
        discovered = 0
        for builder in products.values():
            candidate = builder.to_candidate()
            if candidate is not None:
                discovered += 1
                yield DiscoveredProduct(product=candidate)

        yield DiscoveryCompleted(
            is_complete=(api_failures == 0),
            incomplete_reason=(
                f"{api_failures} 个产品 API 请求失败" if api_failures else None
            ),
            issues=tuple(issues),
        )


# ---------------------------------------------------------------------------
# index.json 解析
# ---------------------------------------------------------------------------


def _parse_download_list(text: str) -> list[dict[str, Any]]:
    """从 index.json 中提取 downloadList 数组。

    index.json 是 JavaScript 格式（含尾逗号），不是合法 JSON。
    需要手动清理后再解析。
    """
    idx = text.find("downloadList = [")
    if idx == -1:
        return []

    # 定位数组起止位置（正确处理嵌套括号和字符串）
    start = idx + len("downloadList = ")
    depth = 0
    end = start
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    raw = text[start:end]
    if not raw:
        return []

    # 清理尾逗号后解析
    cleaned = _strip_trailing_commas(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return []


def _strip_trailing_commas(json_text: str) -> str:
    """移除 JSON 中对象/数组的尾逗号（处理 JS 风格 JSON）。"""
    return re.sub(r",\s*([}\]])", r"\1", json_text)


def _type_codes_for_entry(entry: dict[str, Any]) -> list[str]:
    """根据 downloadList 条目生成 API typeList 码。

    每个条目至少生成 {model}STA。
    如果是开发版条目，同时生成 {model}DEV。
    """
    model = entry.get("model", "")
    if not model:
        return []

    name = entry.get("name", "")
    codes: list[str] = [f"{model}STA"]

    if "开发版" in name:
        codes.append(f"{model}DEV")

    return codes


# ---------------------------------------------------------------------------
# API 调用
# ---------------------------------------------------------------------------


async def _fetch_firmware(http: HttpFetcher, type_code: str) -> dict | None:
    """调用小米固件 API，返回最新固件条目。

    API 以 JSONP 格式返回（jQuery callback 包装），
    需手动剥除包装后解析 JSON。
    返回 None 表示该 type_code 当前无固件数据。
    """
    url = f"{_API_BASE}?typeList={type_code}"
    fetched = await http.get_text(url)
    data = _parse_jsonp(fetched.text)

    if data is None:
        return None

    if data.get("code") != 0:
        return None

    items = data.get("data", {}).get("list", [])
    if not isinstance(items, list) or not items:
        return None

    return items[0]


def _parse_jsonp(text: str) -> dict | None:
    """解析 JSONP 响应体，兼容纯 JSON。

    尝试顺序：
      1. 直接作为 JSON 解析（服务器可能返回纯 JSON）
      2. 匹配 callback({...}); 模式，提取内部 JSON
    """
    text = text.strip()
    # 尝试纯 JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试 JSONP
    m = _JSONP_RE.match(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


# ---------------------------------------------------------------------------
# source_key 规则
# ---------------------------------------------------------------------------


def _variant_from_type_code(type_code: str) -> str:
    """判断 typeList 码对应的固件通道。"""
    if "DEV" in type_code.upper():
        return "dev"
    return "stable"


def _product_source_key(model: str) -> str:
    return f"miwifi:{model}"


def _release_source_key(type_code: str) -> str:
    return f"miwifi:{type_code}"


def _artifact_source_key(type_code: str) -> str:
    return f"miwifi:{type_code}"


# ---------------------------------------------------------------------------
# 产品分类
# ---------------------------------------------------------------------------


def _classify(name: str) -> tuple[ProductFamily, ProductType]:
    """根据产品名称判断分类。

    含 "Mesh" 或 "全屋" 关键词判定为 mesh_router，其余为 router。
    """
    name_lower = name.lower()
    for kw in _MESH_KEYWORDS:
        if kw in name_lower:
            return ProductFamily.ROUTER, ProductType.MESH_ROUTER
    return ProductFamily.ROUTER, ProductType.ROUTER


# ---------------------------------------------------------------------------
# 内部构建器：从 API 条目构建 ProductCandidate 树
# ---------------------------------------------------------------------------


@dataclass
class _ReleaseEntry:
    """一份固件版本的中间表示。"""

    type_code: str
    variant: str  # "stable" | "dev"
    version: str
    release_date: date | None
    download_url: str
    release_notes: str | None


@dataclass
class _ProductBuilder:
    """同一 model 的固件收集器。"""

    model: str
    title: str
    releases: dict[str, _ReleaseEntry] = field(default_factory=dict)

    def add_release(self, type_code: str, variant: str, entry: dict) -> None:
        """添加一个固件版本（同一 type_code 去重）。"""
        if type_code in self.releases:
            return

        ts = entry.get("time")
        release_date = None
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                release_date = datetime.fromtimestamp(
                    ts / 1000, tz=timezone.utc
                ).date()
            except (OSError, ValueError):
                pass

        self.releases[type_code] = _ReleaseEntry(
            type_code=type_code,
            variant=variant,
            version=entry.get("version", ""),
            release_date=release_date,
            download_url=entry.get("url", ""),
            release_notes=entry.get("contents"),
        )

    def to_candidate(self) -> ProductCandidate | None:
        """将收集的所有 Release 组装为一棵 ProductCandidate 树。"""
        if not self.releases:
            return None

        family, ptype = _classify(self.title)

        release_candidates: list[FirmwareReleaseCandidate] = []
        for rel in self.releases.values():
            variant_label = "开发版" if rel.variant == "dev" else "稳定版"

            filename = _filename_from_url(rel.download_url)

            artifact = FirmwareArtifactCandidate(
                source_key=_artifact_source_key(rel.type_code),
                artifact_type=ArtifactType.FIRMWARE,
                original_filename=filename,
                download_url=rel.download_url,
                url_expires_at=None,
                advertised_size=None,
                media_type="application/octet-stream",
                official_checksum=None,
            )

            release_candidates.append(
                FirmwareReleaseCandidate(
                    source_key=_release_source_key(rel.type_code),
                    version_raw=rel.version,
                    version_normalized=rel.version.lower() if rel.version else None,
                    release_date=rel.release_date,
                    title=f"{self.title} {variant_label}",
                    release_notes=rel.release_notes,
                    release_notes_url=None,
                    source_url=_PAGE_URL,
                    artifacts=(artifact,),
                )
            )

        revision = HardwareRevisionCandidate(
            source_key=UNSPECIFIED_REVISION_SOURCE_KEY,
            raw_revision=None,
            normalized_revision=UNSPECIFIED_REVISION,
            revision_explicit=False,
            source_url=None,
            releases=tuple(release_candidates),
        )

        return ProductCandidate(
            source_key=_product_source_key(self.model),
            display_name=self.title,
            model_raw=self.title,
            model_normalized=self.model.upper(),
            series=None,
            product_family=family,
            product_type=ptype,
            source_category=None,
            source_url=_PAGE_URL,
            hardware_revisions=(revision,),
        )


def _filename_from_url(url: str) -> str | None:
    """从下载 URL 提取文件名（不含查询参数）。"""
    if not url:
        return None
    try:
        path = urlsplit(url).path
        return unquote(path.rsplit("/", 1)[-1]) or None
    except Exception:
        return None
