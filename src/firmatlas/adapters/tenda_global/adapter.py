"""Tenda 全球站固件下载适配器。

通过 REST API 采集 Tenda 全球站 (tendacn.com) 的固件元数据。

数据流：
  product/tree → 提取目标分类 ID
  → product/list（按分类分页）→ 获取产品清单
  → data/center/list?format=zip（每个产品）→ 提取固件
  → 构建 ProductCandidate 树 → yield DiscoveredProduct

只采集 format=zip 的固件文件，跳过文档、视频、FAQ 等资源。
"""

from __future__ import annotations

from typing import Any

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

_BASE_URL = "https://www.tendacn.com"
_SITE_ID = 206917

_PRODUCT_TREE_URL = f"{_BASE_URL}/prod/api/pro/product/tree?siteId={_SITE_ID}"
_PRODUCT_LIST_URL = f"{_BASE_URL}/prod/api/pro/product/list"
_DOWNLOAD_LIST_URL = f"{_BASE_URL}/prod/api/data/center/list"

# 目标分类 ID（排除天线、网卡、监控套装、NVR、网络扩展、交换机、OLT、ONT、xDSL Modem）
_TARGET_CATEGORY_IDS: dict[int, ProductType] = {
    # Wi-Fi 路由器
    68: ProductType.ROUTER,           # Wi-Fi 7 Routers
    18: ProductType.ROUTER,           # Wi-Fi 6 Routers
    19: ProductType.ROUTER,           # Wi-Fi 5 Routers
    20: ProductType.ROUTER,           # Wi-Fi 4 Routers
    # Mesh Wi-Fi
    69: ProductType.MESH_ROUTER,      # Nova Mesh Wi-Fi 7
    16: ProductType.MESH_ROUTER,      # Nova Mesh Wi-Fi 6
    17: ProductType.MESH_ROUTER,      # Nova Mesh Wi-Fi 5
    # 5G/4G 路由器
    786932206813253: ProductType.CELLULAR_CPE,  # 5G Router
    786932514361413: ProductType.CELLULAR_CPE,  # 4G Router
    # 移动 Wi-Fi
    786936863776837: ProductType.CELLULAR_CPE,  # LCD Mobile Wi-Fi
    786937300783173: ProductType.CELLULAR_CPE,  # LED Mobile Wi-Fi
    # 企业网关
    33: ProductType.ROUTER,           # Wireless Enterprise Router
    32: ProductType.ROUTER,           # Wired Enterprise Router (包含 VPN 路由器)
    # 企业无线 AP
    37: ProductType.WIRELESS_AP,      # Outdoor AP
    34: ProductType.WIRELESS_AP,      # Ceiling AP
    35: ProductType.WIRELESS_AP,      # In-wall AP
    # CPE / 基站
    39: ProductType.WIRELESS_AP,      # Outdoor CPE
    40: ProductType.WIRELESS_AP,      # Basestation
    # IP 摄像头
    31: ProductType.CAMERA,           # Home Security
    9: ProductType.CAMERA,            # SMB Security
}


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


class TendaGlobalAdapter:
    """Tenda 全球站固件适配器。

    从 REST API 获取产品清单和固件下载信息。
    每次采集实时拉取，不缓存。
    """

    source_key = "tenda-global"

    def __init__(self, http: HttpFetcher) -> None:
        self._http = http

    # ------------------------------------------------------------------
    # discover()
    # ------------------------------------------------------------------

    async def discover(self):
        """执行发现流程：分类树 → 产品清单 → 固件下载。"""

        # ── 步骤 1：获取分类树，提取目标分类 ──
        try:
            tree_data = await self._http.get_json(_PRODUCT_TREE_URL)
        except Exception as exc:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason=f"无法获取产品分类树: {exc}",
                issues=(),
            )
            return

        if not tree_data.data or not isinstance(tree_data.data, dict):
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="产品分类树 API 返回空数据",
                issues=(),
            )
            return

        # ── 步骤 2：按分类获取产品 ──
        all_products: list[dict[str, Any]] = []
        cat_issues: list[AdapterIssueSummary] = []

        for cat_id, ptype in _TARGET_CATEGORY_IDS.items():
            try:
                products = await _fetch_all_pages(
                    self._http, _PRODUCT_LIST_URL, _SITE_ID, cat_id
                )
                # 给每个产品附加 target_product_type
                for p in products:
                    p["_target_product_type"] = ptype
                all_products.extend(products)
            except Exception as exc:
                cat_issues.append(
                    AdapterIssueSummary(
                        code="category_error",
                        detail=f"分类 {cat_id} 产品获取失败: {exc}",
                    )
                )

        if not all_products:
            yield DiscoveryCompleted(
                is_complete=False,
                incomplete_reason="所有目标分类均无产品数据",
                issues=tuple(cat_issues),
            )
            return

        # ── 步骤 3：逐产品获取固件 ──
        product_failures = 0
        firmware_issues: list[AdapterIssueSummary] = []
        discovered = 0

        for product in all_products:
            product_id = product["productId"]
            product_model = product.get("productModel", "")

            try:
                firmware_records = await _fetch_all_downloads(
                    self._http, _DOWNLOAD_LIST_URL, _SITE_ID, product_id
                )
            except Exception as exc:
                product_failures += 1
                firmware_issues.append(
                    AdapterIssueSummary(
                        code="firmware_api_error",
                        detail=f"产品 {product_model} (ID={product_id}) 固件获取失败: {exc}",
                        source_url=f"{_DOWNLOAD_LIST_URL}?siteId={_SITE_ID}&linkProductOrClass={product_id}&format=zip",
                    )
                )
                continue

            if not firmware_records:
                # 该产品无固件（可能是新发布产品），跳过但不计为错误
                yield SkippedCandidate(
                    stage="product",
                    reason_code=SkipReason.PARSE_FAILED,
                    detail=f"产品 {product_model} (ID={product_id}) 无固件下载",
                    source_url=f"{_BASE_URL}/product/help/{product.get('proPath', '')}#download",
                    raw_hint=str(product_id),
                )
                continue

            candidate = _build_candidate(product, firmware_records)
            discovered += 1
            yield DiscoveredProduct(product=candidate)

        # ── 步骤 4：产出完成事件 ──
        all_issues = cat_issues + firmware_issues
        yield DiscoveryCompleted(
            is_complete=(product_failures == 0),
            incomplete_reason=(
                f"{product_failures} 个产品固件 API 请求失败" if product_failures else None
            ),
            issues=tuple(all_issues),
        )


# ---------------------------------------------------------------------------
# API 分页工具
# ---------------------------------------------------------------------------


async def _fetch_all_pages(
    http: HttpFetcher,
    base_url: str,
    site_id: int,
    category_id: int,
) -> list[dict[str, Any]]:
    """分页获取某分类下全部产品。"""
    all_records: list[dict[str, Any]] = []
    page = 1

    while True:
        url = f"{base_url}?pageSize=100&pageNum={page}&siteId={site_id}&categoryId={category_id}"
        result = await http.get_json(url)
        data: dict[str, Any] = result.data

        inner = data.get("data") or {}
        records: list[dict[str, Any]] = inner.get("records", [])
        all_records.extend(records)

        total_pages: int = inner.get("pages", 0)
        if page >= total_pages:
            break
        page += 1

    return all_records


async def _fetch_all_downloads(
    http: HttpFetcher,
    base_url: str,
    site_id: int,
    product_id: int,
) -> list[dict[str, Any]]:
    """获取某产品的全部 zip 格式固件（单页 100 条足够）。"""
    url = (
        f"{base_url}?siteId={site_id}"
        f"&linkProductOrClass={product_id}"
        f"&pageSize=100&format=zip"
    )
    result = await http.get_json(url)
    data: dict[str, Any] = result.data
    inner = data.get("data") or {}
    return inner.get("records", []) or []


# ---------------------------------------------------------------------------
# source_key 规则
# ---------------------------------------------------------------------------


def _product_source_key(product_id: int) -> str:
    return f"tenda:{product_id}"


def _revision_source_key(product_id: int, version: str) -> str:
    """硬件版本 source_key。"""
    clean = version.strip() if version else ""
    if not clean:
        return UNSPECIFIED_REVISION_SOURCE_KEY
    return f"tenda:{product_id}:hw:{clean}"


def _release_source_key(download_id: int) -> str:
    return f"tenda:{download_id}"


def _artifact_source_key(download_id: int) -> str:
    return f"tenda:{download_id}"


def _product_family(ptype: ProductType) -> ProductFamily:
    if ptype == ProductType.CAMERA:
        return ProductFamily.CAMERA
    return ProductFamily.ROUTER


# ---------------------------------------------------------------------------
# Candidate 构建器
# ---------------------------------------------------------------------------


def _build_candidate(
    product: dict[str, Any],
    firmware_records: list[dict[str, Any]],
) -> ProductCandidate:
    """从产品数据和固件记录构建一棵 ProductCandidate 树。"""
    product_id = product["productId"]
    product_model = product.get("productModel", "")
    version = product.get("version", "") or ""

    ptype = product.get("_target_product_type", ProductType.ROUTER)

    display_name = product_model
    if version:
        display_name = f"{product_model} {version}"

    # 固件按版本去重，保留最新（按 updateTime 排序）
    unique_releases = _deduplicate_firmware(firmware_records)

    release_candidates: list[FirmwareReleaseCandidate] = []
    for fw in unique_releases:
        download_id = fw["id"]
        version_raw = (fw.get("version") or "").strip()
        file_url = fw.get("file") or ""

        if not file_url:
            continue

        filename = _filename_from_url(file_url)

        artifact = FirmwareArtifactCandidate(
            source_key=_artifact_source_key(download_id),
            artifact_type=ArtifactType.FIRMWARE,
            original_filename=filename,
            download_url=file_url,
            url_expires_at=None,
            advertised_size=fw.get("fileSize"),
            media_type="application/zip",
            official_checksum=None,
        )

        release_title = f"{display_name} Firmware {version_raw}".strip()
        release_candidates.append(
            FirmwareReleaseCandidate(
                source_key=_release_source_key(download_id),
                version_raw=version_raw,
                version_normalized=version_raw.lower() if version_raw else None,
                release_date=None,
                title=release_title,
                release_notes=None,
                release_notes_url=None,
                source_url=f"{_BASE_URL}/product/help/{product.get('proPath', '')}#download",
                artifacts=(artifact,),
            )
        )

    revision = HardwareRevisionCandidate(
        source_key=_revision_source_key(product_id, version),
        raw_revision=version or None,
        normalized_revision=version.strip() if version else UNSPECIFIED_REVISION,
        revision_explicit=bool(version),
        source_url=None,
        releases=tuple(release_candidates),
    )

    return ProductCandidate(
        source_key=_product_source_key(product_id),
        display_name=display_name,
        model_raw=product_model,
        model_normalized=product_model.upper(),
        series=None,
        product_family=_product_family(ptype),
        product_type=ptype,
        source_category=product.get("categoryName"),
        source_url=f"{_BASE_URL}/product/help/{product.get('proPath', '')}#download",
        hardware_revisions=(revision,),
    )


def _deduplicate_firmware(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """固件版本去重：同一 version 保留 updateTime 最新的一条。"""
    best: dict[str, dict[str, Any]] = {}
    for fw in records:
        version = (fw.get("version") or "").strip()
        if not version:
            # 无版本号的固件单独保留（用 id 区分）
            key = str(fw["id"])
        else:
            key = version.lower()

        if key not in best or (fw.get("updateTime") or "") > (best[key].get("updateTime") or ""):
            best[key] = fw

    return list(best.values())


def _filename_from_url(url: str) -> str | None:
    """从下载 URL 中提取文件名。"""
    if not url:
        return None
    try:
        # URL 格式: https://static.tenda.com.cn/document/2026/.../filename.zip
        return url.rsplit("/", 1)[-1].split("?")[0] or None
    except Exception:
        return None
