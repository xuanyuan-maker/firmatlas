"""Omada Worldwide 目标型号白名单契约测试。"""

import pytest

from firmatlas.adapters.omada_global.classification import classify
from firmatlas.domain.model import ProductFamily, ProductType


@pytest.mark.parametrize(
    ("model", "source_category", "product_type"),
    [
        ("EAP787", "EAP", ProductType.WIRELESS_AP),
        ("EAP690E HD", "EAP", ProductType.WIRELESS_AP),
        ("EAP650-Outdoor", "EAP", ProductType.WIRELESS_AP),
        ("EAP215-Bridge KIT", "EAP", ProductType.WIRELESS_AP),
        ("AP8635-E", "AP", ProductType.WIRELESS_AP),
        ("Sector Bridge 5", "BRIDGE", ProductType.WIRELESS_AP),
        ("Beam Bridge 5 UR KIT", "BRIDGE", ProductType.WIRELESS_AP),
        ("ER8411", "ER", ProductType.ROUTER),
        ("ER7212PC", "ER", ProductType.ROUTER),
        ("TL-ER7206", "TL-ER", ProductType.ROUTER),
        ("TL-R605", "TL-R", ProductType.ROUTER),
        ("DR3650v", "DR", ProductType.ROUTER),
        ("G611", "G", ProductType.ROUTER),
        ("ER701-5G-Outdoor", "ER", ProductType.CELLULAR_CPE),
        ("ER706WP-4G", "ER", ProductType.CELLULAR_CPE),
        ("DR3650v-4G", "DR", ProductType.CELLULAR_CPE),
        ("G36W-4G", "G", ProductType.CELLULAR_CPE),
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
        "SG3452",
        "SX3206HPP",
        "ES210GMP",
        "IES206G",
        "OC200",
        "OC400",
        "RP108GE",
        "Fusion G+",
        "EAP673-Extender",
        "EAP",
        "AP",
        "ER",
        "DR",
        "GATEWAY",
        "",
    ],
)
def test_non_target_models_are_excluded(model: str) -> None:
    assert classify(model) is None


def test_matching_is_case_insensitive_and_trims_outer_whitespace() -> None:
    result = classify("  er706w-4g  ")

    assert result is not None
    assert result.product_type is ProductType.CELLULAR_CPE
