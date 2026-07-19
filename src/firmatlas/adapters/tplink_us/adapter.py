"""tp-link-us 适配器（接口设计 §4，阶段 5）。

从 TP-Link 美国站下载中心发现固件，产出 DiscoveryEvent 流。

## 数据流（与 CN 的本质差异）

CN 站用 POST 搜索 API 返回平铺 JSON；US 站是服务端渲染 HTML，分三层：

1. 索引页 `/us/support/download/` 内嵌 `productTree` JSON：
   menu_id → [型号对象]，每个对象含 model_name / menu_name / product_title / url。
2. 型号下载页 `/us/support/download/{slug}/`：
   多硬件版本型号在此列出 version-list（→ 各硬件子页）；
   单硬件版本型号的固件表直接在此页。
3. 硬件版本子页 `/us/support/download/{slug}/{hwver}/`：固件条目所在页。

适配器工作：
1. 抓索引页，提取 productTree
2. 逐型号用 classify(menu_name) 判定是否目标类（非目标 → SkippedCandidate）
3. 对目标型号抓下载页 → 解析硬件版本 → 抓各硬件子页（或主页）→ 解析固件
4. 每个固件条目转 Artifact 候选（无下载链接 → SkippedCandidate）
5. 逐型号产出 DiscoveredProduct，最后产出 DiscoveryCompleted

## 边界

- Omada/VIGI 商用设备的下载页会重定向到独立站（support.omadanetworks.com），
  抓取时抛异常 → 记 issue 并跳过该型号（不跨来源合并）。
- 部分摄像头固件走 App OTA，页面有固件条目但无下载链接 → SkippedCandidate。
- US 下载不需要 Referer（与 CN 相反）。
"""

from __future__ import annotations

import json
import re

from firmatlas.adapters.events import (
    AdapterIssueSummary,
    DiscoveredProduct,
    DiscoveryCompleted,
    SkippedCandidate,
    SkipReason,
)
from firmatlas.adapters.tplink_us.classification import Classification, classify
from firmatlas.adapters.tplink_us.firmware_parser import (
    parse_firmware_entries,
    parse_hardware_versions,
)
from firmatlas.domain.candidates import (
    FirmwareArtifactCandidate,
    FirmwareReleaseCandidate,
    HardwareRevisionCandidate,
    ProductCandidate,
)
from firmatlas.domain.model import ArtifactType
from firmatlas.infra.http_client import HttpFetcher

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.tp-link.com/us/"
_INDEX_URL = "https://www.tp-link.com/us/support/download/"
_DOWNLOAD_URL_TEMPLATE = "https://www.tp-link.com/us/support/download/{slug}/"
# 只信任主站域名的页面；下载页重定向到其它主机（如 Omada 独立站）视为越界
_ALLOWED_HOST = "www.tp-link.com"

# 从索引页 HTML 提取 `var productTree = {...};` 的起始锚点
_PRODUCT_TREE_ANCHOR = re.compile(r"var\s+productTree\s*=\s*")


class TplinkUsAdapter:
    """TP-Link 美国站适配器。

    构造时注入 HttpFetcher，不得自建 HTTP 客户端。
    source_key 固定为 "tp-link-us"。
    """

    source_key = "tp-link-us"

    def __init__(self, http: HttpFetcher) -> None:
        self._http = http

    # -- discover -------------------------------------------------------

    async def discover(self):
        """异步生成器，逐事件产出发现结果。"""
        issues: list[AdapterIssueSummary] = []

        # --- 1. 抓索引页并提取 productTree ---
        try:
            fetched = await self._http.get_text(_INDEX_URL)
        except Exception as exc:
            # 索引页拿不到属于来源级致命错误：声明不完整，不触发消失对账
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason=f"索引页请求失败: {exc}",
                issues=(),
            )
            return

        product_tree = _extract_product_tree(fetched.text)
        if product_tree is None:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="索引页未找到 productTree 数据",
                issues=(),
            )
            return

        # --- 2. 收集目标型号（去重：同型号可能出现在多个分类下）---
        targets = _collect_targets(product_tree)

        # --- 3. 逐型号抓取固件，产出事件 ---
        for model in targets:
            async for event in self._process_model(model, issues):
                yield event

        # --- 4. 完成 ---
        yield DiscoveryCompleted(
            is_complete=True,
            incomplete_reason=None,
            issues=tuple(issues),
        )

    # -- 单型号处理 -----------------------------------------------------

    async def _process_model(self, model: _TargetModel, issues: list[AdapterIssueSummary]):
        """抓取一个型号的下载页与硬件子页，产出该型号的事件。"""
        download_url = _DOWNLOAD_URL_TEMPLATE.format(slug=model.slug)
        try:
            main = await self._http.get_text(download_url)
        except Exception as exc:
            # 抓取失败（含 Omada 重定向到独立站的 SSL/连接错误）：跳过该型号
            issues.append(
                AdapterIssueSummary(
                    code="model_fetch_failed",
                    detail=f"型号 {model.model_name} 下载页请求失败: {exc}",
                    source_url=download_url,
                )
            )
            return

        # 越界检查：下载页重定向到非主站域名（如 Omada 独立站）→ 跳过
        if _ALLOWED_HOST not in main.url:
            issues.append(
                AdapterIssueSummary(
                    code="offsite_redirect",
                    detail=f"型号 {model.model_name} 重定向到站外 {main.url}，不属于本来源",
                    source_url=download_url,
                )
            )
            return

        # 解析硬件版本：有子页则逐个抓，无则固件在主页
        hw_links = parse_hardware_versions(main.text)
        hw_pages: list[tuple[str, str, str]] = []  # (hw_label, page_url, html)
        if hw_links:
            for link in hw_links:
                try:
                    sub = await self._http.get_text(link.url)
                except Exception as exc:
                    issues.append(
                        AdapterIssueSummary(
                            code="hw_fetch_failed",
                            detail=f"型号 {model.model_name} 硬件版本 {link.version_label} "
                            f"请求失败: {exc}",
                            source_url=link.url,
                        )
                    )
                    continue
                hw_pages.append((link.version_label, link.url, sub.text))
        else:
            # 单硬件版本：固件表在主页，硬件版本从标题推断（解析时提取）
            hw_pages.append(("", download_url, main.text))

        product = self._build_product(model, hw_pages, download_url)
        if product is None:
            # 该型号无任何可下载固件条目：产出跳过说明
            yield SkippedCandidate(
                stage="product",
                reason_code=SkipReason.MISSING_IDENTITY,
                detail=f"型号 {model.model_name} 无可下载固件",
                source_url=download_url,
                raw_hint=model.slug,
            )
            return

        yield product

    # -- 构建 ProductCandidate ------------------------------------------

    def _build_product(
        self,
        model: _TargetModel,
        hw_pages: list[tuple[str, str, str]],
        product_url: str,
    ) -> DiscoveredProduct | None:
        """把型号的各硬件子页固件组装为 ProductCandidate。

        无任何可下载固件（download_url 全为 None）时返回 None。
        """
        hw_candidates: list[HardwareRevisionCandidate] = []

        for hw_label, page_url, html in hw_pages:
            entries = [e for e in parse_firmware_entries(html) if e.download_url]
            if not entries:
                continue

            # 硬件版本标识：优先用子页 URL 的版本段（如 v3），否则从标题推断
            hw_norm = _hw_from_label(hw_label) or _hw_from_title(entries[0].title)
            hw_source_key = f"{model.slug}/v{hw_norm}"

            release_candidates: list[FirmwareReleaseCandidate] = []
            for entry in entries:
                fw_ver = _fw_version_from_title(entry.title)
                release_source_key = f"{model.slug}/v{hw_norm}/fw{fw_ver}"
                artifact = FirmwareArtifactCandidate(
                    source_key=f"{model.slug}/v{hw_norm}/{_filename(entry.download_url)}",
                    artifact_type=ArtifactType.FIRMWARE,
                    original_filename=_filename(entry.download_url),
                    download_url=entry.download_url,  # type: ignore[arg-type]
                    url_expires_at=None,
                    advertised_size=_size_to_bytes(entry.file_size_text),
                    media_type="application/zip",
                    official_checksum=None,  # US 站不提供官方校验和
                )
                release_candidates.append(
                    FirmwareReleaseCandidate(
                        source_key=release_source_key,
                        version_raw=entry.title,
                        version_normalized=fw_ver,
                        release_date=_parse_date(entry.published_date),
                        title=entry.title,
                        release_notes=None,
                        release_notes_url=None,
                        source_url=page_url,
                        artifacts=(artifact,),
                    )
                )

            hw_candidates.append(
                HardwareRevisionCandidate(
                    source_key=hw_source_key,
                    raw_revision=f"V{hw_norm}",
                    normalized_revision=hw_norm,
                    revision_explicit=bool(hw_label),
                    source_url=page_url,
                    releases=tuple(release_candidates),
                )
            )

        if not hw_candidates:
            return None

        cls = model.classification
        return DiscoveredProduct(
            product=ProductCandidate(
                source_key=model.slug,
                display_name=model.model_name,
                model_raw=model.model_name,
                model_normalized=model.model_name.upper(),
                series=None,
                product_family=cls.family,
                product_type=cls.product_type,
                source_category=cls.source_category,
                source_url=product_url,
                hardware_revisions=tuple(hw_candidates),
            )
        )


# ---------------------------------------------------------------------------
# 目标型号收集
# ---------------------------------------------------------------------------


class _TargetModel:
    """一个通过分类的目标型号。"""

    def __init__(self, model_name: str, slug: str, classification: Classification) -> None:
        self.model_name = model_name
        self.slug = slug
        self.classification = classification


def _collect_targets(product_tree: dict) -> list[_TargetModel]:
    """遍历 productTree，收集目标类型号（按 slug 去重）。"""
    seen: dict[str, _TargetModel] = {}
    for items in product_tree.values():
        if not isinstance(items, list):
            continue
        for obj in items:
            menu_name = obj.get("menu_name", "")
            model_name = obj.get("model_name", "")
            if not model_name:
                continue
            classification = classify(menu_name)
            if classification is None:
                continue
            slug = _model_to_slug(model_name)
            # 同型号可能出现在多个分类；保留首次命中
            if slug not in seen:
                seen[slug] = _TargetModel(model_name, slug, classification)
    return list(seen.values())


# ---------------------------------------------------------------------------
# 解析辅助
# ---------------------------------------------------------------------------


def _extract_product_tree(html: str) -> dict | None:
    """从索引页 HTML 提取 `var productTree = {...};` 的 JSON 对象。

    用括号配平找到对象结束位置（productTree 内含大量嵌套，正则不可靠）。
    """
    m = _PRODUCT_TREE_ANCHOR.search(html)
    if m is None:
        return None
    start = html.find("{", m.end())
    if start == -1:
        return None
    depth = 0
    i = start
    while i < len(html):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return None
        i += 1
    return None


def _model_to_slug(model_name: str) -> str:
    """型号名转下载页 slug：小写、空格转连字符。

    如 "Archer BE670" → "archer-be670"、"Deco X55" → "deco-x55"。
    """
    return model_name.strip().lower().replace(" ", "-")


def _hw_from_label(hw_label: str) -> str | None:
    """从 version-list 的标签提取硬件版本（去 V 前缀）。

    "V3" → "3"，"V5.60" → "5.60"，空串 → None。
    """
    label = hw_label.strip()
    if not label:
        return None
    if label.upper().startswith("V"):
        label = label[1:]
    return label or None


# 标题形如 "Archer BE670(US)_V1.6_1.0.2 Build 20251203"
_TITLE_HW = re.compile(r"_V([0-9.]+)_")
_TITLE_FW = re.compile(r"_V[0-9.]+_([0-9][0-9.]*)\s+Build", re.IGNORECASE)


def _hw_from_title(title: str) -> str:
    """从固件标题提取硬件版本（回退用）。无法提取时返回 'unspecified'。"""
    m = _TITLE_HW.search(title)
    return m.group(1) if m else "unspecified"


def _fw_version_from_title(title: str) -> str:
    """从固件标题提取固件版本号。无法提取时回退用整个标题。"""
    m = _TITLE_FW.search(title)
    if m:
        return m.group(1)
    return title.strip()


def _filename(url: str | None) -> str | None:
    """从下载 URL 取文件名。"""
    if not url:
        return None
    return url.rsplit("/", 1)[-1] or None


# "18.66 MB" / "24.30 MB" / "512 KB"
_SIZE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(KB|MB|GB)", re.IGNORECASE)
_SIZE_UNIT = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}


def _size_to_bytes(size_text: str | None) -> int | None:
    """把 "18.66 MB" 这类近似大小文本转成字节数（近似值）。

    US 站只提供两位小数的 MB/KB 文本，转出的字节数是近似值——
    下载用例的 size_tolerance 机制会容忍此偏差。无法解析时返回 None。
    """
    if not size_text:
        return None
    m = _SIZE_RE.search(size_text)
    if m is None:
        return None
    value = float(m.group(1))
    unit = _SIZE_UNIT[m.group(2).upper()]
    return int(value * unit)


# "2026-01-26"
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _parse_date(date_text: str | None):
    """把 "2026-01-26" 解析为 date；无法解析时返回 None。"""
    if not date_text:
        return None
    m = _DATE_RE.search(date_text)
    if m is None:
        return None
    from datetime import date

    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
