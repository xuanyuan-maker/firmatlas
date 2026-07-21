"""Zyxel Global 目标型号白名单。"""

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


_WIRELESS_AP_PATTERNS = (
    ("NWA", re.compile(r"NWA\d[0-9A-Z-]*\Z")),
    ("WAX", re.compile(r"WAX\d[0-9A-Z-]*\Z")),
    ("WBE", re.compile(r"WBE\d[0-9A-Z-]*\Z")),
)
_GATEWAY_PATTERNS = (
    ("USG FLEX", re.compile(r"USG FLEX \d[0-9A-Z-]*\Z")),
    ("USG", re.compile(r"USG\d[0-9A-Z-]*\Z")),
    ("ATP", re.compile(r"ATP\d[0-9A-Z-]*\Z")),
    ("VPN", re.compile(r"VPN\d[0-9A-Z-]*\Z")),
)
_CELLULAR_PATTERNS = (
    ("NR", re.compile(r"NR\d[0-9A-Z-]*\Z")),
    ("FWA", re.compile(r"FWA\d[0-9A-Z-]*\Z")),
    ("LTE", re.compile(r"LTE\d[0-9A-Z-]*\Z")),
)


def classify(model_name: str) -> Classification | None:
    """返回白名单型号的分类；非目标型号返回 ``None``。"""
    normalized = _normalize(model_name)
    if not normalized:
        return None

    for source_category, pattern in _WIRELESS_AP_PATTERNS:
        if pattern.fullmatch(normalized):
            return Classification(
                source_category=source_category,
                family=ProductFamily.ROUTER,
                product_type=ProductType.WIRELESS_AP,
            )

    for source_category, pattern in _GATEWAY_PATTERNS:
        if pattern.fullmatch(normalized):
            return Classification(
                source_category=source_category,
                family=ProductFamily.ROUTER,
                product_type=ProductType.ROUTER,
            )

    for source_category, pattern in _CELLULAR_PATTERNS:
        if pattern.fullmatch(normalized):
            return Classification(
                source_category=source_category,
                family=ProductFamily.ROUTER,
                product_type=ProductType.CELLULAR_CPE,
            )

    return None


def _normalize(model_name: str) -> str:
    return " ".join(model_name.strip().upper().replace("_", " ").split())
