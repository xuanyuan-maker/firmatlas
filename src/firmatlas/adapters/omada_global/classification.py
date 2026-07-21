"""Omada Worldwide 目标型号白名单。

官方固件目录同时包含无线 AP、网关、交换机、控制器和扩展器。
本模块只根据公开型号名完成目标设备分类，不访问网络，也不解析固件响应。
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


_EAP_MODEL = re.compile(r"EAP\d[0-9A-Z]*(?:[ -][0-9A-Z]+)*\Z")
_OMADA_PRO_AP_MODEL = re.compile(r"AP\d+(?:-[A-Z])?\Z")
_BRIDGE_MODEL = re.compile(r"(?:SECTOR|FLEX|BEAM) BRIDGE \d+(?: [A-Z]+)?(?: KIT)?\Z")
_GATEWAY_PREFIXES = ("TL-ER", "TL-R", "ER", "DR")
_OMADA_PRO_GATEWAY = re.compile(r"G\d+(?:W-\dG)?\Z")
_CELLULAR_MARKER = re.compile(r"(?:^|[- ])(?:4G|5G)(?:[- ]|$)")


def classify(model_name: str) -> Classification | None:
    """返回白名单型号的分类；非目标型号返回 ``None``。"""
    normalized = model_name.strip().upper()
    if not normalized:
        return None

    if "EXTENDER" in normalized:
        return None

    if _EAP_MODEL.fullmatch(normalized):
        return _wireless_ap("EAP")

    if _OMADA_PRO_AP_MODEL.fullmatch(normalized):
        return _wireless_ap("AP")

    if _BRIDGE_MODEL.fullmatch(normalized):
        return _wireless_ap("BRIDGE")

    for prefix in _GATEWAY_PREFIXES:
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            return _gateway(prefix, normalized)

    if _OMADA_PRO_GATEWAY.fullmatch(normalized):
        return _gateway("G", normalized)

    return None


def _wireless_ap(source_category: str) -> Classification:
    return Classification(
        source_category=source_category,
        family=ProductFamily.ROUTER,
        product_type=ProductType.WIRELESS_AP,
    )


def _gateway(source_category: str, normalized: str) -> Classification:
    product_type = (
        ProductType.CELLULAR_CPE if _CELLULAR_MARKER.search(normalized) else ProductType.ROUTER
    )
    return Classification(
        source_category=source_category,
        family=ProductFamily.ROUTER,
        product_type=product_type,
    )
