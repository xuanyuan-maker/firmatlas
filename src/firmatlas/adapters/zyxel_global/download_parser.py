"""Zyxel 产品详情页下载材料解析。

详情页通过 SSR 输出材料链接，固件下拉框中的未选版本可能位于 ``option.value``
或 ``data-*`` 属性。本解析器不依赖易变的 CSS 类名，而是识别 Zyxel 官方下载 URL
中的稳定路径结构：``/{MODEL}/{material_type}/{filename}``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

_DOWNLOAD_HOST = "download.zyxel.com"
_VERSION_PATTERN = re.compile(r"(?i)(?<![A-Z0-9])V?(\d+(?:\.\d+)+(?:\([A-Z0-9.-]+\))[A-Z0-9.-]*)")


@dataclass(frozen=True)
class DownloadMaterial:
    """详情页中的一个官方材料下载。"""

    material_type: str
    filename: str
    download_url: str
    version_raw: str | None
    version_normalized: str | None


@dataclass(frozen=True)
class FirmwareDownload:
    """一个固件文件及可明确关联的发布说明。"""

    filename: str
    download_url: str
    version_raw: str
    version_normalized: str
    release_notes_url: str | None


def parse_download_materials(html: str) -> list[DownloadMaterial]:
    """解析页面所有官方材料 URL，按首次出现顺序去重。"""
    parser = _MaterialUrlParser()
    parser.feed(html)
    parser.close()

    materials: list[DownloadMaterial] = []
    seen: set[str] = set()
    for raw_url in parser.urls:
        parsed = _parse_material_url(raw_url)
        if parsed is None or parsed.download_url in seen:
            continue
        seen.add(parsed.download_url)
        materials.append(parsed)
    return materials


def firmware_downloads(materials: list[DownloadMaterial]) -> list[FirmwareDownload]:
    """筛出可识别版本的固件，并按版本关联 Release Note。"""
    release_notes = [item for item in materials if item.material_type == "release_note"]
    firmware: list[FirmwareDownload] = []
    for item in materials:
        if item.material_type != "firmware":
            continue
        if item.version_raw is None or item.version_normalized is None:
            continue
        note = next(
            (
                candidate.download_url
                for candidate in release_notes
                if candidate.version_normalized == item.version_normalized
            ),
            None,
        )
        firmware.append(
            FirmwareDownload(
                filename=item.filename,
                download_url=item.download_url,
                version_raw=item.version_raw,
                version_normalized=item.version_normalized,
                release_notes_url=note,
            )
        )
    return firmware


def _parse_material_url(raw_url: str) -> DownloadMaterial | None:
    normalized_url = unescape(raw_url.strip()).replace("\\/", "/")
    if normalized_url.startswith("//"):
        normalized_url = f"https:{normalized_url}"
    parsed = urlsplit(normalized_url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != _DOWNLOAD_HOST:
        return None

    parts = PurePosixPath(unquote(parsed.path)).parts
    if len(parts) < 4:
        return None
    material_type = parts[-2].strip().casefold().replace("-", "_")
    filename = parts[-1].strip()
    if not material_type or not filename:
        return None

    version_raw, version_normalized = _extract_version(filename)
    return DownloadMaterial(
        material_type=material_type,
        filename=filename,
        download_url=normalized_url,
        version_raw=version_raw,
        version_normalized=version_normalized,
    )


def _extract_version(filename: str) -> tuple[str | None, str | None]:
    stem = PurePosixPath(filename).stem
    matched = _VERSION_PATTERN.search(stem)
    if matched is None:
        return None, None
    raw = matched.group(0)
    normalized = matched.group(1)
    return raw, normalized


class _MaterialUrlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._collect(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._collect(attrs)

    def _collect(self, attrs: list[tuple[str, str | None]]) -> None:
        for _name, value in attrs:
            if value and _DOWNLOAD_HOST in value.casefold():
                self.urls.append(value)
