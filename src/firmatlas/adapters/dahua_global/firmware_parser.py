"""大华国际站固件下载 API JSON 解析。

API 返回 /api/en/downloadCenter/firmware/list 的 JSON 数据。
本模块只做数据提取和转换，不触网、不访问数据库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class ProductInfo:
    """固件适用产品信息。"""

    product_id: str
    product_name: str


@dataclass(frozen=True)
class FirmwareEntry:
    """API 返回的单条固件记录。"""

    firmware_id: str
    firmware_name: str
    firmware_url: str
    release_notes_url: str | None
    advertised_size_bytes: int | None
    post_date: date | None
    md5: str | None
    sha256: str | None
    version_raw: str | None
    products: tuple[ProductInfo, ...]


_VERSION_PATTERN = re.compile(r"V\d+(?:\.\d+)+(?:\.R\.\d+)?", re.IGNORECASE)

_SIZE_PATTERN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>KB|MB|GB|TB|B)", re.IGNORECASE
)

_SIZE_MULTIPLIERS: dict[str, int] = {
    "B": 1,
    "KB": 1024,
    "MB": 1024 * 1024,
    "GB": 1024 * 1024 * 1024,
    "TB": 1024 * 1024 * 1024 * 1024,
}


def extract_version(firmware_name: str) -> str | None:
    """从固件文件名中提取版本号。

    例如 ``DH_IPC-HX8XSC-Vodka_MultiLang_PN_Stream3_V3.146.0000000.32.R.260708``
    返回 ``V3.146.0000000.32.R.260708``。
    """
    matched = _VERSION_PATTERN.search(firmware_name)
    return matched.group(0) if matched else None


def parse_size(text: str | None) -> int | None:
    """将大华 API 返回的大小字符串转为字节数。

    例如 "356.51MB" → 373826550。
    """
    if not text or not text.strip():
        return None
    matched = _SIZE_PATTERN.search(text.strip())
    if not matched:
        return None
    value = float(matched.group("value"))
    unit = matched.group("unit").upper()
    multiplier = _SIZE_MULTIPLIERS.get(unit)
    if multiplier is None:
        return None
    return int(value * multiplier)


def parse_date(text: str | None) -> date | None:
    """将 API 返回的日期字符串 ``YYYY-MM-DD`` 转为 date。"""
    if not text or not text.strip():
        return None
    try:
        return date.fromisoformat(text.strip())
    except (ValueError, TypeError):
        return None


def parse_firmware_list(raw_list: list[dict[str, Any]]) -> list[FirmwareEntry]:
    """将 API 返回的 raw list 转换为 FirmwareEntry 列表。"""
    entries: list[FirmwareEntry] = []
    for item in raw_list:
        try:
            entry = _parse_single_firmware(item)
            if entry is not None:
                entries.append(entry)
        except (KeyError, TypeError, ValueError):
            continue
    return entries


def _parse_single_firmware(raw: dict[str, Any]) -> FirmwareEntry | None:
    firmware_id = raw.get("firmware_id")
    firmware_name = raw.get("firmware_name") or ""
    firmware_url = raw.get("firmware_url")

    if not firmware_id or not firmware_name or not firmware_url:
        return None

    release_notes_url = raw.get("firmware_note") or None
    if release_notes_url and not release_notes_url.startswith("http"):
        release_notes_url = None

    file_size = parse_size(raw.get("firmware_file_size"))
    post_date = parse_date(raw.get("post_date"))
    md5 = raw.get("md5") or None
    sha256 = raw.get("hash") or None
    version_raw = extract_version(firmware_name)

    products: list[ProductInfo] = []
    for prod in raw.get("product") or []:
        product_id = prod.get("product_id")
        product_name = (prod.get("product_name") or "").strip()
        if product_id and product_name:
            products.append(ProductInfo(product_id=str(product_id), product_name=product_name))

    return FirmwareEntry(
        firmware_id=str(firmware_id),
        firmware_name=firmware_name,
        firmware_url=firmware_url,
        release_notes_url=release_notes_url,
        advertised_size_bytes=file_size,
        post_date=post_date,
        md5=md5,
        sha256=sha256,
        version_raw=version_raw,
        products=tuple(products),
    )
