"""draytek_global directory_parser 单元测试。"""

from pathlib import Path

from firmatlas.adapters.draytek_global.directory_parser import (
    DirectoryEntry,
    parse_directory_listing,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "draytek-global"


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class TestParseDirectoryListing:
    """Apache 目录列表解析测试。"""

    def test_parses_root_directories(self) -> None:
        """根目录列表应正确提取所有子目录，跳过 Parent Directory。"""
        html = _fixture("ftp-root.html")
        page_url = "https://fw.draytek.com.tw/"
        entries = parse_directory_listing(html, page_url)

        names = [e.name for e in entries]
        assert "Vigor2767" in names
        assert "Vigor2962" in names
        assert "VigorSwitch G1080" in names
        assert "ACS 3" in names
        assert "Utility" in names
        # 确认都是目录
        assert all(e.is_directory for e in entries)

    def test_parses_firmware_directory(self) -> None:
        """Firmware 目录应同时包含子目录和文件，跳过 Parent Directory。"""
        html = _fixture("firmware-vigor2767.html")
        page_url = "https://fw.draytek.com.tw/Vigor2767/Firmware/"
        entries = parse_directory_listing(html, page_url)

        dirs = [e for e in entries if e.is_directory]
        files = [e for e in entries if not e.is_directory]

        assert [d.name for d in dirs] == ["v5.4.0", "v4.4.5.3"]
        assert [f.name for f in files] == ["latest.txt"]
        # latest.txt 大小
        assert files[0].size == "7"

    def test_parses_version_directory_with_files(self) -> None:
        """版本目录应正确提取固件 zip、checksum、release note。"""
        html = _fixture("version-vigor2767-v5.4.0.html")
        page_url = "https://fw.draytek.com.tw/Vigor2767/Firmware/v5.4.0/"
        entries = parse_directory_listing(html, page_url)

        files = [e for e in entries if not e.is_directory]
        names = [f.name for f in files]

        assert "Vigor2767_v5.4.0.zip" in names
        assert "FIRMWARE.DIGESTS" in names
        assert "DrayTek_Vigor2767_V5.4.0_release-note.pdf" in names
        # 确认大小解析正确
        zip_file = next(f for f in files if f.name.endswith(".zip"))
        assert zip_file.size == "51M"

    def test_urls_are_absolute(self) -> None:
        """解析出的 URL 应该基于 page_url 被解析为绝对 URL。"""
        html = _fixture("ftp-root.html")
        base = "https://fw.draytek.com.tw/"
        entries = parse_directory_listing(html, base)

        for entry in entries:
            assert entry.url.startswith("https://fw.draytek.com.tw/")
            assert entry.url.endswith("/")

    def test_parses_last_modified_date(self) -> None:
        """应正确解析目录的修改日期。"""
        html = _fixture("firmware-vigor2767.html")
        base = "https://fw.draytek.com.tw/Vigor2767/Firmware/"
        entries = parse_directory_listing(html, base)

        v540 = next(e for e in entries if e.name == "v5.4.0")
        assert v540.last_modified is not None
        assert v540.last_modified.year == 2026
        assert v540.last_modified.month == 7
        assert v540.last_modified.day == 14

    def test_skips_parent_directory(self) -> None:
        """Parent Directory 行应被跳过。"""
        html = _fixture("version-vigor2767-v5.4.0.html")
        base = "https://fw.draytek.com.tw/Vigor2767/Firmware/v5.4.0/"
        entries = parse_directory_listing(html, base)

        names = [e.name for e in entries]
        assert "Parent Directory" not in names

    def test_multi_channel_firmware_dir(self) -> None:
        """多 channel（latest + latest_stable）的 Firmware 目录应正确解析。"""
        html = _fixture("firmware-vigor2962.html")
        base = "https://fw.draytek.com.tw/Vigor2962/Firmware/"
        entries = parse_directory_listing(html, base)

        dirs = [e.name for e in entries if e.is_directory]
        files = [e.name for e in entries if not e.is_directory]

        assert "v4.4.6.1" in dirs
        assert "v4.4.5.3" in dirs
        assert "latest.txt" in files
        assert "latest_stable.txt" in files
