"""Zyxel Global 产品详情页下载材料解析测试。"""

from pathlib import Path

from firmatlas.adapters.zyxel_global.download_parser import (
    firmware_downloads,
    parse_download_materials,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "zyxel-global"


def _fixture() -> str:
    return (FIXTURE_DIR / "download-usg-flex-100h.html").read_text(encoding="utf-8")


def test_parse_all_material_urls_including_unselected_firmware_options() -> None:
    materials = parse_download_materials(_fixture())

    assert len(materials) == 5
    assert [item.material_type for item in materials] == [
        "firmware",
        "firmware",
        "firmware",
        "release_note",
        "datasheet",
    ]
    assert [item.filename for item in materials[:3]] == [
        "USG FLEX 100H_1.38(ABXF.0)C0.zip",
        "USG FLEX 100H_V1.37(ABXF.1)C0.zip",
        "USG FLEX 100H_1.36(ABXF.2)C0.zip",
    ]


def test_firmware_downloads_exclude_other_materials_and_associate_release_note() -> None:
    downloads = firmware_downloads(parse_download_materials(_fixture()))

    assert [item.version_normalized for item in downloads] == [
        "1.38(ABXF.0)C0",
        "1.37(ABXF.1)C0",
        "1.36(ABXF.2)C0",
    ]
    assert downloads[0].release_notes_url == (
        "https://download.zyxel.com/USG_FLEX_100H/release_note/"
        "USG%20FLEX%20100H_1.38(ABXF.0)C0_Release_Note.pdf"
    )
    assert downloads[1].release_notes_url is None


def test_untrusted_and_malformed_urls_are_ignored() -> None:
    html = """
    <a href="https://example.com/MODEL/firmware/fake.zip">mirror</a>
    <a href="http://download.zyxel.com/MODEL/firmware/insecure.zip">http</a>
    <a href="https://download.zyxel.com/too-short.zip">short</a>
    """

    assert parse_download_materials(html) == []


def test_protocol_relative_official_url_is_normalized() -> None:
    html = (
        '<option value="//download.zyxel.com/NR5103E/firmware/'
        'NR5103E_1.00(ABVC.0)C0.zip">1.00</option>'
    )

    materials = parse_download_materials(html)

    assert len(materials) == 1
    assert materials[0].download_url.startswith("https://download.zyxel.com/")
