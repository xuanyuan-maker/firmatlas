"""宇视科技全球站（global.uniview.com）固件下载适配器。

通过 HTML 页面解析采集宇视科技美国站的固件元数据。该站没有 REST API，
产品列表由 ASP.NET 服务端渲染直接嵌入 HTML 中。

数据流：
  HTTP GET 三种摄像机分类页面 → 正则提取 downFile() 参数
  → 构建 ProductCandidate 树 → yield DiscoveredProduct

只采集 Network Cameras、PTZ Cameras、Thermal Cameras 三类摄像机的固件。
"""

from __future__ import annotations

import re
from datetime import date, datetime

from firmatlas.adapters.events import (
    AdapterIssueSummary,
    DiscoveredProduct,
    DiscoveryCompleted,
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

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_BASE_URL = "https://global.uniview.com"

# 目标分类：分类名 → 分类页面 URL
_TARGET_CATEGORIES: dict[str, str] = {
    "Network Cameras": f"{_BASE_URL}/us/Support/Download_Center/Firmware/Network_Cameras/",
    "PTZ Cameras": f"{_BASE_URL}/us/Support/Download_Center/Firmware/PTZ_Cameras/",
    "Thermal Cameras": f"{_BASE_URL}/us/Support/Download_Center/Firmware/Thermal_Cameras/",
}

# 从固件文件名中提取版本号
# 格式: GIPC-B6218.7.5.251212 20260120.zip → B6218.7.5.251212
_VERSION_FROM_FILENAME_RE = re.compile(r"GIPC-([A-Z]?\d[\d.]*\d+)")


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


class UniviewGlobalAdapter:
    """宇视科技全球站固件适配器。

    从 HTML 页面解析产品清单和固件下载信息。
    每次采集实时拉取，不缓存。
    """

    source_key = "uniview-global"

    def __init__(self, http: HttpFetcher) -> None:
        self._http = http

    # ------------------------------------------------------------------
    # discover()
    # ------------------------------------------------------------------

    async def discover(self):
        """执行发现流程：依次抓取三个分类页面 → 解析产品列表。"""

        cat_issues: list[AdapterIssueSummary] = []
        all_rows: list[_ProductRow] = []
        cat_failure_count = 0

        for cat_name, cat_url in _TARGET_CATEGORIES.items():
            try:
                result = await self._http.get_text(cat_url)
            except Exception as exc:
                cat_failure_count += 1
                cat_issues.append(
                    AdapterIssueSummary(
                        code="category_page_error",
                        detail=f"分类 {cat_name} 页面获取失败: {exc}",
                        source_url=cat_url,
                    )
                )
                continue

            if result.status_code != 200:
                cat_failure_count += 1
                cat_issues.append(
                    AdapterIssueSummary(
                        code="category_page_error",
                        detail=f"分类 {cat_name} 返回 HTTP {result.status_code}",
                        source_url=cat_url,
                    )
                )
                continue

            rows = _parse_product_rows(result.text, cat_name, cat_url)
            all_rows.extend(rows)

        # 所有分类均失败 → 灾难性错误
        if cat_failure_count == len(_TARGET_CATEGORIES):
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="所有分类页面均获取失败",
                issues=tuple(cat_issues),
            )
            return

        # 没有解析出任何产品
        if not all_rows:
            yield DiscoveryCompleted(
                is_complete=(cat_failure_count == 0),
                incomplete_reason=(
                    "所有分类均无产品数据" if cat_failure_count == 0 else None
                ),
                issues=tuple(cat_issues),
            )
            return

        # 构建并产出产品
        product_failures = 0
        for row in all_rows:
            try:
                candidate = _build_candidate(row)
            except Exception as exc:
                product_failures += 1
                yield SkippedCandidate(
                    stage="product",
                    reason_code=SkipReason.PARSE_FAILED,
                    detail=f"产品 {row.model} 构建失败: {exc}",
                    source_url=row.category_url,
                    raw_hint=row.model,
                )
                continue

            yield DiscoveredProduct(product=candidate)

        # 产出完成事件
        yield DiscoveryCompleted(
            is_complete=(cat_failure_count == 0 and product_failures == 0),
            incomplete_reason=_compose_incomplete_reason(
                cat_failure_count, product_failures
            ),
            issues=tuple(cat_issues),
        )


# ---------------------------------------------------------------------------
# HTML 解析
# ---------------------------------------------------------------------------


class _ProductRow:
    """从 HTML 中解析出的一条产品固件行。"""

    __slots__ = (
        "model",
        "download_url",
        "filename",
        "date_str",
        "category_name",
        "category_url",
    )

    def __init__(
        self,
        model: str,
        download_url: str,
        filename: str,
        date_str: str,
        category_name: str,
        category_url: str,
    ) -> None:
        self.model = model
        self.download_url = download_url
        self.filename = filename
        self.date_str = date_str
        self.category_name = category_name
        self.category_url = category_url


def _parse_product_rows(
    html: str, category_name: str, category_url: str
) -> list[_ProductRow]:
    """从分类页 HTML 中解析所有产品固件行。

    目标结构:
      <li class="Product-Resources-box">
        <a onclick="downFile('URL', 'FILENAME')">MODEL</a>
        <span>DATE</span>
    """
    rows: list[_ProductRow] = []

    # 按 Product-Resources-box 分割
    boxes = html.split('class="Product-Resources-box"')[1:]  # 跳过标题行

    for box_html in boxes:
        # 提取 onclick 属性中的 downFile 参数
        onclick_match = re.search(
            r"onclick=\"downFile\('([^']*)',\s*'([^']*)'\);?\"", box_html
        )
        if not onclick_match:
            continue

        download_url = onclick_match.group(1)
        filename = onclick_match.group(2)

        # 提取模型名（<a> 标签文本内容）
        model_match = re.search(
            r"<a[^>]*onclick=\"downFile[^>]*>([^<]+)</a>", box_html
        )
        if not model_match:
            continue
        model = model_match.group(1).strip()

        # 提取日期（第二个 <span>）
        spans = re.findall(r"<span>([^<]*)</span>", box_html)
        date_str = spans[0].strip() if spans else ""

        if not model or not download_url:
            continue

        rows.append(
            _ProductRow(
                model=model,
                download_url=download_url,
                filename=filename,
                date_str=date_str,
                category_name=category_name,
                category_url=category_url,
            )
        )

    return rows


# ---------------------------------------------------------------------------
# source_key 生成
# ---------------------------------------------------------------------------


def _product_source_key(model_normalized: str) -> str:
    return f"uniview:{model_normalized}"


def _release_source_key(model_normalized: str, filename_normalized: str) -> str:
    return f"uniview:{model_normalized}:release:{filename_normalized}"


def _artifact_source_key(model_normalized: str, filename_normalized: str) -> str:
    return f"uniview:{model_normalized}:artifact:{filename_normalized}"


# ---------------------------------------------------------------------------
# Candidate 构建
# ---------------------------------------------------------------------------


def _build_candidate(row: _ProductRow) -> ProductCandidate:
    """从一条产品行构建一棵完整的 ProductCandidate 树。"""
    model_normalized = row.model.upper()
    filename_safe = _safe_key_part(row.filename)

    # 从固件文件名中提取版本号
    version_raw = _extract_version_from_filename(row.filename)

    # 解析日期
    release_date = _parse_date(row.date_str)

    release_title = f"{row.model} Firmware {version_raw}".strip()

    artifact = FirmwareArtifactCandidate(
        source_key=_artifact_source_key(model_normalized, filename_safe),
        artifact_type=ArtifactType.FIRMWARE,
        original_filename=row.filename,
        download_url=row.download_url,
        url_expires_at=None,
        advertised_size=None,
        media_type="application/zip",
        official_checksum=None,
    )

    release = FirmwareReleaseCandidate(
        source_key=_release_source_key(model_normalized, filename_safe),
        version_raw=version_raw,
        version_normalized=version_raw.lower() if version_raw else None,
        release_date=release_date,
        title=release_title,
        release_notes=None,
        release_notes_url=None,
        source_url=row.category_url,
        artifacts=(artifact,),
    )

    revision = HardwareRevisionCandidate(
        source_key=UNSPECIFIED_REVISION_SOURCE_KEY,
        raw_revision=None,
        normalized_revision=UNSPECIFIED_REVISION,
        revision_explicit=False,
        source_url=None,
        releases=(release,),
    )

    display_name = f"{row.model} ({row.category_name})"

    return ProductCandidate(
        source_key=_product_source_key(model_normalized),
        display_name=display_name,
        model_raw=row.model,
        model_normalized=model_normalized,
        series=None,
        product_family=ProductFamily.CAMERA,
        product_type=ProductType.CAMERA,
        source_category=row.category_name,
        source_url=row.category_url,
        hardware_revisions=(revision,),
    )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _extract_version_from_filename(filename: str) -> str:
    """从固件文件名中提取版本号。

    >>> _extract_version_from_filename("GIPC-B6218.7.5.251212 20260120.zip")
    'B6218.7.5.251212'
    >>> _extract_version_from_filename("unknown.zip")
    'unknown.zip'
    """
    m = _VERSION_FROM_FILENAME_RE.search(filename)
    if m:
        return m.group(1)
    # 回退：使用去掉扩展名的文件名
    return filename.rsplit(".", 1)[0]


def _parse_date(date_str: str) -> date | None:
    """解析日期字符串，格式: YYYY-MM-DD。"""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _safe_key_part(s: str) -> str:
    """将字符串转换为可在 source_key 中使用的安全格式。"""
    return s.replace(" ", "_").replace("(", "").replace(")", "")


def _compose_incomplete_reason(
    cat_failures: int, product_failures: int
) -> str | None:
    parts: list[str] = []
    if cat_failures:
        parts.append(f"{cat_failures} 个分类页面获取失败")
    if product_failures:
        parts.append(f"{product_failures} 个产品构建失败")
    return "；".join(parts) if parts else None
