"""tp-link-cn 标题解析器测试。"""

from datetime import date

import pytest

from firmatlas.adapters.tplink_cn.title_parser import (
    parse_title,
)

# --- 标准格式 ------------------------------------------------------------


@pytest.mark.parametrize(
    "title, expected_model, expected_hw, expected_fw, expected_date",
    [
        (
            "TL-R5009PE-AC V1.0升级软件20260108_1.0.30",
            "TL-R5009PE-AC",
            "1.0",
            "1.0.30",
            date(2026, 1, 8),
        ),
        (
            "TL-ER6229GPE-AC V2.0升级软件20240820_3.1.1",
            "TL-ER6229GPE-AC",
            "2.0",
            "3.1.1",
            date(2024, 8, 20),
        ),
        (
            "TL-IPC9440L-AC V1.0升级软件20260622_1.0.5",
            "TL-IPC9440L-AC",
            "1.0",
            "1.0.5",
            date(2026, 6, 22),
        ),
        (
            "TL-NIPC5454-GW4 V1.0升级软件20260417_1.0.2",
            "TL-NIPC5454-GW4",
            "1.0",
            "1.0.2",
            date(2026, 4, 17),
        ),
    ],
)
def test_standard_titles(
    title: str, expected_model: str, expected_hw: str,
    expected_fw: str, expected_date: date,
) -> None:
    result = parse_title(title)
    assert result is not None
    assert result.model_raw == expected_model
    assert result.hardware_version_raw == expected_hw
    assert result.firmware_version == expected_fw
    assert result.release_date == expected_date


# --- 中文变体 ------------------------------------------------------------


@pytest.mark.parametrize(
    "title, expected_model",
    [
        ("TL-IPC48AW 双模版 V1.0升级软件20260618_1.0.7", "TL-IPC48AW 双模版"),
        ("TL-IPC48AN 双目变焦版 V1.0升级软件20260527_1.0.4", "TL-IPC48AN 双目变焦版"),
        ("TL-IPC689V双目广角版 V1.0升级软件20260602_1.0.10", "TL-IPC689V双目广角版"),
        ("TL-IPC642X-F4GE 电源套装 V1.1升级软件20251231_1.0.10", "TL-IPC642X-F4GE 电源套装"),
    ],
)
def test_model_with_chinese_variant(title: str, expected_model: str) -> None:
    result = parse_title(title)
    assert result is not None
    assert result.model_raw == expected_model
    assert result.hardware_version_raw is not None
    assert result.firmware_version is not None


# --- 多硬件版本 ----------------------------------------------------------


def test_multi_hardware_version() -> None:
    result = parse_title("TL-IPC632X-A4GY V1.0/V1.1升级软件20251231_1.0.10")
    assert result is not None
    assert result.model_raw == "TL-IPC632X-A4GY"
    # 正则的 " V" 只消费第一个 V，第二个 V 留在 raw 里：V1.0/V1.1 → 1.0/V1.1
    assert result.hardware_version_raw == "1.0/V1.1"
    assert result.firmware_version == "1.0.10"
    assert result.release_date == date(2025, 12, 31)


# --- 长型号 --------------------------------------------------------------


def test_long_model_name() -> None:
    result = parse_title("TL-IPC682XLH-F4GE-S12C33 V1.0升级软件20260529_1.0.6")
    assert result is not None
    assert result.model_raw == "TL-IPC682XLH-F4GE-S12C33"
    assert result.hardware_version_raw == "1.0"
    assert result.firmware_version == "1.0.6"


# --- 解析失败 ------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_title",
    [
        "",
        "   ",
        "不是升级软件的标题",
        "TL-R5009PE-AC 升级软件20260108_1.0.30",  # 缺少 V 前缀
        "TL-R5009PE-AC V1.0升级软件_1.0.30",       # 缺少日期
    ],
)
def test_invalid_titles_return_none(bad_title: str) -> None:
    assert parse_title(bad_title) is None


# --- 尾部空白处理 --------------------------------------------------------


def test_title_is_stripped() -> None:
    result = parse_title("  TL-R5009PE-AC V1.0升级软件20260108_1.0.30  ")
    assert result is not None
    assert result.model_raw == "TL-R5009PE-AC"


# --- 日期解析边界 --------------------------------------------------------


def test_invalid_date_does_not_break_parsing() -> None:
    # 日期部分不合法时 release_date 为 None，但其他字段仍解析。
    result = parse_title("TL-TEST V1.0升级软件99999999_1.0.0")
    assert result is not None
    assert result.model_raw == "TL-TEST"
    assert result.firmware_version == "1.0.0"
    assert result.release_date is None
