"""tp-link-us HTML 固件解析器测试（基于 fixture，AC-31）。

所有测试使用 tests/fixtures/tp-link-us/ 下的脱敏 HTML，不访问真实网站。
"""

from __future__ import annotations

from pathlib import Path

from firmatlas.adapters.tplink_us.firmware_parser import (
    parse_firmware_entries,
    parse_hardware_versions,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tp-link-us"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 硬件版本列表
# ---------------------------------------------------------------------------


def test_parse_hardware_versions_multi():
    """多硬件版本型号主页：解析 version-list 得到各子页链接。"""
    links = parse_hardware_versions(_load("download_deco-x55.html"))
    labels = [link.version_label for link in links]
    urls = [link.url for link in links]
    assert labels == ["V3", "V2", "V1"]
    assert urls[0] == "https://www.tp-link.com/us/support/download/deco-x55/v3/"
    assert all(u.startswith("https://www.tp-link.com/us/support/download/deco-x55/") for u in urls)


def test_parse_hardware_versions_single_none():
    """单硬件版本型号主页无 version-list：返回空列表。"""
    links = parse_hardware_versions(_load("download_archer-be670.html"))
    assert links == []


# ---------------------------------------------------------------------------
# 固件条目（有下载链接）
# ---------------------------------------------------------------------------


def test_parse_firmware_router_with_download():
    """路由器主页固件条目：标题/日期/大小/下载真链齐全。"""
    entries = parse_firmware_entries(_load("download_archer-be670.html"))
    assert len(entries) == 1
    e = entries[0]
    assert e.title == "Archer BE670(US)_V1.6_1.0.2 Build 20251203"
    assert e.download_url == (
        "https://static.tp-link.com/upload/firmware/2026/202601/20260126/"
        "Archer BE670(US)_V1.6_20251203.zip"
    )
    assert e.published_date == "2026-01-26"
    assert e.file_size_text == "18.66 MB"
    assert e.language == "Multi-language"


def test_parse_firmware_subpage_with_download():
    """硬件子页固件条目：下载真链正确（排除 Go to Local Website）。"""
    entries = parse_firmware_entries(_load("download_deco-x55_v3.html"))
    assert len(entries) == 1
    e = entries[0]
    assert e.title == "Deco X55(US)_V3_1.2.9 Build 20250815"
    assert e.download_url is not None
    assert e.download_url.startswith("https://static.tp-link.com/upload/firmware/")
    # 不能误取本地站链接
    assert "/en/support/download/" not in e.download_url
    assert e.file_size_text == "24.30 MB"


# ---------------------------------------------------------------------------
# 固件条目（无下载链接：摄像头 App OTA 边界）
# ---------------------------------------------------------------------------


def test_parse_firmware_camera_no_download():
    """摄像头固件条目有标题/日期但无下载链接（走 App OTA）：download_url 为 None。"""
    entries = parse_firmware_entries(_load("download_tapo-c100.html"))
    assert len(entries) == 1
    e = entries[0]
    assert e.title == "Tapo C100(US)_V1_1.3.6 Build 230512"
    assert e.download_url is None
    assert e.published_date == "2023-07-20"
    # 该条目无 File Size / Language
    assert e.file_size_text is None


def test_parse_no_table_returns_empty():
    """无固件表格的 HTML：返回空列表。"""
    assert parse_firmware_entries("<html><body><p>no firmware</p></body></html>") == []
