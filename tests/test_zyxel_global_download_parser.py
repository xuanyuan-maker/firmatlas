"""Zyxel Global 产品详情页下载材料解析测试。"""

from pathlib import Path

from firmatlas.adapters.zyxel_global.download_parser import (
    firmware_downloads,
    parse_download_materials,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "zyxel-global"


def _fixture() -> str:
    return (FIXTURE_DIR / "download-usg-flex-100h.html").read_text(encoding="utf-8")


def test_parse_material_urls_from_realistic_fixture() -> None:
    materials = parse_download_materials(_fixture())

    assert len(materials) == 4
    assert [item.material_type for item in materials] == [
        "release_note",
        "release_note",
        "release_note",
        "datasheet",
    ]
    assert [item.filename for item in materials] == [
        "USG FLEX 100H_1.38(ABXF.0)C0_2.pdf",
        "USG FLEX 100H_1.37(ABXF.1)C0_2.pdf",
        "USG FLEX 100H_1.38(ABXF.0)C0_Release_Note.pdf",
        "USG FLEX 100H_14.pdf",
    ]


def test_firmware_downloads_empty_when_all_firmware_is_login_protected() -> None:
    materials = parse_download_materials(_fixture())
    downloads = firmware_downloads(materials)

    assert downloads == []


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


def test_pdf_under_firmware_path_is_release_note_not_firmware() -> None:
    html = (
        '<a href="https://download.zyxel.com/USG_FLEX_100H/firmware/'
        'USG%20FLEX%20100H_1.38(ABXF.0)C0_2.pdf">Release Note</a>'
    )

    materials = parse_download_materials(html)

    assert len(materials) == 1
    assert materials[0].material_type == "release_note"
    assert materials[0].version_raw == "1.38(ABXF.0)C0"
    assert materials[0].version_normalized == "1.38(ABXF.0)C0"
    assert firmware_downloads(materials) == []
