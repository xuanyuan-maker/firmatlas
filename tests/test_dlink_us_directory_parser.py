"""D-Link IIS 资源目录解析器测试，只读取固定 fixture。"""

from pathlib import Path

from firmatlas.adapters.dlink_us.directory_parser import parse_directory_listing

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dlink-us"


def _parse(filename: str, page_url: str):
    return parse_directory_listing(
        (FIXTURE_DIR / filename).read_text(encoding="utf-8"),
        page_url,
    )


def test_parse_product_directories_and_ignore_parent() -> None:
    entries = _parse(
        "products-index.html",
        "https://support.dlink.com/resource/PRODUCTS/",
    )

    assert [entry.name for entry in entries] == [
        "DBA-X2830P",
        "DCS-8302LH",
        "DGS-1210-SERIES",
        "DIR-X5460",
        "DSR-250V2",
    ]
    assert all(entry.is_directory for entry in entries)
    assert entries[1].url == "https://support.dlink.com/resource/PRODUCTS/DCS-8302LH/"


def test_distinguish_file_and_revision_directory() -> None:
    entries = _parse(
        "product-dcs-8302lh.html",
        "https://support.dlink.com/resource/products/DCS-8302LH/",
    )

    assert entries[0].name == "DCS-8302LH-US EOS NOTICE.pdf"
    assert entries[0].is_directory is False
    assert entries[1].name == "REVA"
    assert entries[1].is_directory is True


def test_preserve_encoded_download_url_and_visible_filename() -> None:
    entries = _parse(
        "firmware-dsr-250v2-revb.html",
        "https://support.dlink.com/resource/products/DSR-250V2/REVB/",
    )

    assert entries[0].name == "DSR-250v2_B1_FW2.01.B002 (1).img"
    assert "%20" in entries[0].url
    assert entries[0].is_directory is False


def test_ignore_links_outside_pre_directory_listing() -> None:
    html = """
    <nav><a href="https://example.com/help">Help</a></nav>
    <pre><a href="FIRMWARE/">FIRMWARE</a></pre>
    """

    entries = parse_directory_listing(
        html,
        "https://support.dlink.com/resource/products/R15/",
    )

    assert len(entries) == 1
    assert entries[0].name == "FIRMWARE"
    assert entries[0].url == "https://support.dlink.com/resource/products/R15/FIRMWARE/"
