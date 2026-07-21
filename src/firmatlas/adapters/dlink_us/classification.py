"""D-Link 美国支持站目标型号白名单。

资源目录混合了路由器、摄像头、交换机、网卡和智能家居等大量产品。
本模块只根据目录中公开的型号名做白名单判断，不访问网络，也不解析固件。
未命中的型号由适配器统一记录为非目标产品。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from firmatlas.domain.model import ProductFamily, ProductType


@dataclass(frozen=True)
class Classification:
    """白名单型号对应的标准分类和原始系列。"""

    source_category: str
    family: ProductFamily
    product_type: ProductType


_PREFIX_CLASSIFICATIONS: tuple[
    tuple[str, ProductFamily, ProductType],
    ...,
] = (
    ("DCS-", ProductFamily.CAMERA, ProductType.CAMERA),
    ("DBA-", ProductFamily.ROUTER, ProductType.WIRELESS_AP),
    ("DWR-", ProductFamily.ROUTER, ProductType.CELLULAR_CPE),
    ("COVR-", ProductFamily.ROUTER, ProductType.MESH_ROUTER),
    ("COVR_", ProductFamily.ROUTER, ProductType.MESH_ROUTER),
    ("DIR-", ProductFamily.ROUTER, ProductType.ROUTER),
    ("DGL-", ProductFamily.ROUTER, ProductType.ROUTER),
    ("GO-RT-", ProductFamily.ROUTER, ProductType.ROUTER),
    ("EBR-", ProductFamily.ROUTER, ProductType.ROUTER),
    ("DI-", ProductFamily.ROUTER, ProductType.ROUTER),
    ("DSL-", ProductFamily.ROUTER, ProductType.ROUTER),
    ("DSR-", ProductFamily.ROUTER, ProductType.ROUTER),
    ("DFL-", ProductFamily.ROUTER, ProductType.ROUTER),
    ("DBG-", ProductFamily.ROUTER, ProductType.ROUTER),
    ("DBR-", ProductFamily.ROUTER, ProductType.ROUTER),
)

_R_MODEL = re.compile(r"R\d+(?:-[A-Z0-9]+)?\Z")
_M_MODEL = re.compile(r"M\d+(?:-[A-Z0-9]+)?\Z")
_CELLULAR_M_MODEL = re.compile(r"M9\d{2}(?:-[A-Z0-9]+)?\Z")


def classify(model_name: str) -> Classification | None:
    """返回白名单型号的分类；非目标型号返回 ``None``。"""
    normalized = model_name.strip().upper()
    if not normalized:
        return None

    for prefix, family, product_type in _PREFIX_CLASSIFICATIONS:
        if normalized.startswith(prefix):
            return Classification(
                source_category=prefix.rstrip("-_"),
                family=family,
                product_type=product_type,
            )

    if _R_MODEL.fullmatch(normalized):
        return Classification(
            source_category="R",
            family=ProductFamily.ROUTER,
            product_type=ProductType.ROUTER,
        )

    if _M_MODEL.fullmatch(normalized):
        product_type = (
            ProductType.CELLULAR_CPE
            if _CELLULAR_M_MODEL.fullmatch(normalized)
            else ProductType.MESH_ROUTER
        )
        return Classification(
            source_category="M",
            family=ProductFamily.ROUTER,
            product_type=product_type,
        )

    return None
