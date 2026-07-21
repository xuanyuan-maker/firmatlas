"""Zyxel Global 目标型号白名单契约测试。"""

import pytest

from firmatlas.adapters.zyxel_global.classification import classify
from firmatlas.domain.model import ProductFamily, ProductType


@pytest.mark.parametrize(
    ("model", "source_category", "product_type"),
    [
        ("NWA50AX", "NWA", ProductType.WIRELESS_AP),
        ("WAX610D", "WAX", ProductType.WIRELESS_AP),
        ("WBE660S", "WBE", ProductType.WIRELESS_AP),
        ("USG FLEX 100H", "USG FLEX", ProductType.ROUTER),
        ("USG20-VPN", "USG", ProductType.ROUTER),
        ("ATP500", "ATP", ProductType.ROUTER),
        ("VPN100", "VPN", ProductType.ROUTER),
        ("NR5103E", "NR", ProductType.CELLULAR_CPE),
        ("FWA710", "FWA", ProductType.CELLULAR_CPE),
        ("LTE5366-M608", "LTE", ProductType.CELLULAR_CPE),
    ],
)
def test_direct_include_whitelist(
    model: str,
    source_category: str,
    product_type: ProductType,
) -> None:
    result = classify(model)

    assert result is not None
    assert result.source_category == source_category
    assert result.family is ProductFamily.ROUTER
    assert result.product_type is product_type


@pytest.mark.parametrize(
    "model",
    [
        "GS1920-24HPv2",
        "XS1930-12HP",
        "NAS540",
        "NXC2500",
        "USG FLEX H SERIES",
        "NWA",
        "WAX",
        "NR",
        "",
    ],
)
def test_non_target_models_are_excluded(model: str) -> None:
    assert classify(model) is None


def test_matching_is_case_insensitive_and_trims_outer_whitespace() -> None:
    result = classify("  usg flex 100h  ")

    assert result is not None
    assert result.product_type is ProductType.ROUTER
