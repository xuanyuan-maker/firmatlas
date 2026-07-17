"""tp-link-cn 搜索结果的标题解析。

搜索 API 返回的每条 SOFTWARE 记录是一条 1:1:1:1 平铺记录——产品型号、
硬件版本、固件版本都编码在 title 字段里，例如：
  "TL-R5009PE-AC V1.0升级软件20260108_1.0.30"
  "TL-IPC48AW 双模版 V1.0升级软件20260618_1.0.7"
  "TL-IPC632X-A4GY V1.0/V1.1升级软件20251231_1.0.10"

本模块负责把 title 解析为 ParsedTitle 结构体，供适配器构造
ProductCandidate / HardwareRevisionCandidate / FirmwareReleaseCandidate。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# " TL-R5009PE-AC V1.0升级软件20260108_1.0.30"
#  └─ 型号 ──────────┘  └─HW┘        └─日期─┘ └─固件版本─┘
_TITLE_PATTERN = re.compile(
    r"^(.+?) V(\S+?)升级软件(\d{8})_(.+)$"
)


@dataclass(frozen=True)
class ParsedTitle:
    """从搜索记录 title 解析出的结构化字段。

    - model_raw: title 中提取的完整型号（含中文变体描述，如"双模版"）
    - hardware_version_raw: V 后的原始字符串（如 "1.0", "1.0/1.1"）
    - firmware_version: 固件版本号（如 "1.0.30"）
    - release_date: 日期部分解析结果，解析失败为 None
    """

    model_raw: str
    hardware_version_raw: str
    firmware_version: str
    release_date: date | None


def parse_title(title: str) -> ParsedTitle | None:
    """解析搜索结果的 title，返回结构化字段；解析失败返回 None。

    >>> parse_title("TL-R5009PE-AC V1.0升级软件20260108_1.0.30")
    ParsedTitle(model_raw='TL-R5009PE-AC', hardware_version_raw='1.0',
                firmware_version='1.0.30', release_date=date(2026, 1, 8))

    >>> parse_title("TL-IPC632X-A4GY V1.0/V1.1升级软件20251231_1.0.10")
    ParsedTitle(model_raw='TL-IPC632X-A4GY', hardware_version_raw='1.0/1.1',
                firmware_version='1.0.10', release_date=date(2025, 12, 31))

    >>> parse_title("TL-IPC689V双目广角版 V1.0升级软件20260602_1.0.10")
    ParsedTitle(model_raw='TL-IPC689V双目广角版', hardware_version_raw='1.0',
                firmware_version='1.0.10', release_date=date(2026, 6, 2))
    """
    m = _TITLE_PATTERN.match(title.strip())
    if not m:
        return None

    model_raw = m.group(1).strip()
    hw_raw = m.group(2).strip()
    date_str = m.group(3)
    fw_ver = m.group(4).strip()

    parsed_date = None
    try:
        parsed_date = date(
            int(date_str[0:4]),
            int(date_str[4:6]),
            int(date_str[6:8]),
        )
    except (ValueError, IndexError):
        parsed_date = None

    return ParsedTitle(
        model_raw=model_raw,
        hardware_version_raw=hw_raw,
        firmware_version=fw_ver,
        release_date=parsed_date,
    )
