"""D-Link 美国站目标型号白名单契约测试。"""

import pytest

from firmatlas.adapters.dlink_us.classification import classify
from firmatlas.domain.model import ProductFamily, ProductType


@pytest.mark.parametrize(
    ("model", "source_category", "product_type"),
    [
        ("DIR-X5460", "DIR", ProductType.ROUTER),
        ("DGL-4500", "DGL", ProductType.ROUTER),
        ("COVR-X1863", "COVR", ProductType.MESH_ROUTER),
        ("COVR_R2203", "COVR", ProductType.MESH_ROUTER),
        ("R15", "R", ProductType.ROUTER),
        ("M30", "M", ProductType.MESH_ROUTER),
        ("M30-SP", "M", ProductType.MESH_ROUTER),
        ("M960", "M", ProductType.CELLULAR_CPE),
        ("GO-RT-N300", "GO-RT", ProductType.ROUTER),
        ("EBR-2310", "EBR", ProductType.ROUTER),
        ("DI-604PLUS", "DI", ProductType.ROUTER),
        ("DSL-2750B", "DSL", ProductType.ROUTER),
        ("DWR-961", "DWR", ProductType.CELLULAR_CPE),
        ("DCS-8302LH", "DCS", ProductType.CAMERA),
        ("DBA-X2830P", "DBA", ProductType.WIRELESS_AP),
        ("DSR-250V2", "DSR", ProductType.ROUTER),
        ("DFL-860E", "DFL", ProductType.ROUTER),
        ("DBG-2000", "DBG", ProductType.ROUTER),
        ("DBR-X3000-AP", "DBR", ProductType.ROUTER),
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
    assert result.product_type is product_type
    expected_family = (
        ProductFamily.CAMERA if product_type is ProductType.CAMERA else ProductFamily.ROUTER
    )
    assert result.family is expected_family


@pytest.mark.parametrize(
    "model",
    [
        "DWC-1000",
        "DWS-3160-24PC",
        "DNH-200",
        "DAP-X2850",
        "DWL-8610AP",
        "DGS-1210-28",
        "DNR-202L",
        "DNS-320L",
        "DWA-X1850",
        "DCH-S1621KT",
        "AC13U",
        "README",
        "R",
        "M",
        "",
    ],
)
def test_non_target_models_are_excluded(model: str) -> None:
    assert classify(model) is None


def test_matching_is_case_insensitive_and_trims_outer_whitespace() -> None:
    result = classify("  dcs-8302lh  ")

    assert result is not None
    assert result.product_type is ProductType.CAMERA
