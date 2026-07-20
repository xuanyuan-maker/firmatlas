"""hikvision-global 固件目录解析器测试，只读取固定 HTML fixture。"""

from pathlib import Path

import pytest

from firmatlas.adapters.hikvision_global.firmware_parser import (
    extract_firmware_version,
    parse_firmware_products,
)

FIXTURE = Path(__file__).parent / "fixtures" / "hikvision-global" / "firmware_camera_samples.html"


def _products():
    return parse_firmware_products(FIXTURE.read_text(encoding="utf-8"))


def test_parse_products_preserves_source_categories() -> None:
    products = _products()

    assert len(products) == 4
    assert products[0].main_category == "IP-Products"
    assert products[0].sub_category == "Network-Cameras"
    assert products[-1].sub_category == "Network-Video-Recorders"


def test_parse_standard_camera_group() -> None:
    product = _products()[0]
    group = product.groups[0]

    assert product.title == "DS-2CD1043G3-LIU(F)(/SX)"
    assert product.product_url == (
        "/en/products/IP-Products/Network-Cameras/EasyIP-2.0plus/ds-2cd1043g3-liu/"
    )
    assert group.applied_models == (
        "DS-2CD1043G3-LIU(2.8mm)",
        "DS-2CD1043G3-LIU(2.8mm)(BLACK)",
        "DS-2CD1043G3-LIU(4mm)",
    )
    assert group.firmware_assets[0].title == "Firmware_V5.9.15_260508"
    assert group.firmware_assets[0].download_url is not None
    assert group.firmware_assets[0].download_url.endswith("S3000721729.zip")
    assert group.release_notes[0].title == "Network Camera-V5.9.15_260508 Release Notes"


def test_parse_same_version_regional_artifacts_in_one_group() -> None:
    group = _products()[1].groups[0]

    assert [asset.title for asset in group.firmware_assets] == [
        "Firmware_Europe_V4.30.122_201107",
        "Firmware_V4.30.122_201107",
    ]
    assert len(group.release_notes) == 1
    assert group.applied_models == ("DS-2DF8425IX-AELW(T3)",)


def test_parse_different_versions_without_release_notes() -> None:
    group = _products()[2].groups[0]

    assert [extract_firmware_version(asset.title) for asset in group.firmware_assets] == [
        "V4.2.7_180418",
        "V5.5.8_210702",
    ]
    assert group.release_notes == ()


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Firmware_V5.9.15_260508", "V5.9.15_260508"),
        ("Firmware_Europe_V4.30.122_201107", "V4.30.122_201107"),
        ("Camera V4.30.122 build 201107 Release Notes", "V4.30.122 build 201107"),
        ("firmware without a version", None),
    ],
)
def test_extract_firmware_version(title: str, expected: str | None) -> None:
    assert extract_firmware_version(title) == expected


def test_missing_download_url_is_preserved_for_adapter_validation() -> None:
    html = """
    <div class="nav-item" data-main-tag="IP-Products" data-sub-tag="Network-Cameras">
      <div class="main-title"><a class="link" href="/camera/">Camera</a></div>
      <div class="main-item">
        <div class="firmware-section">
          <a class="assets" data-title="Firmware_V1.0.0_250101" href="#download-agreement">
            Firmware
          </a>
        </div>
        <ul class="sub-list"><li class="sub-item">CAMERA-1</li></ul>
      </div>
    </div>
    """

    asset = parse_firmware_products(html)[0].groups[0].firmware_assets[0]
    assert asset.download_url is None
