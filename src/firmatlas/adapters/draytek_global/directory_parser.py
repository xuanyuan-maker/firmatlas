"""解析 Apache 目录列表 HTML（fw.draytek.com.tw）。

DrayTek 固件 FTP 服务器使用标准的 Apache mod_autoindex 生成的目录列表，
格式为 <table> 结构，每行一个 <a> 链接。

与 dlink_us/directory_parser.py 的区别：
- D-Link 是 IIS 格式（<pre> 标签包裹的纯文本链接）
- DrayTek 是 Apache 格式（<table> 结构）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

# Apache 目录列表中日期的格式: "2026-07-14 11:49" 或 "2025-12-26  "
_APACHE_DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})?")


@dataclass(frozen=True)
class DirectoryEntry:
    """Apache 目录列表中的一个条目（文件或子目录）。"""

    name: str
    url: str
    is_directory: bool
    last_modified: datetime | None
    size: str | None  # 原始大小字符串，如 "51M", "367", "192K"


def parse_directory_listing(html: str, page_url: str) -> tuple[DirectoryEntry, ...]:
    """解析 Apache 目录列表 HTML，返回所有条目的元组（不含 Parent Directory）。

    忽略 Parent Directory 行和无法解析的行。
    """
    parser = _ApacheListingParser(page_url)
    parser.feed(html)
    parser.close()
    return tuple(parser.entries)


class _ApacheListingParser(HTMLParser):
    """解析 Apache mod_autoindex 生成的 <table> 结构目录列表。

    每行结构:
      <tr>
        <td><img ...></td>
        <td><a href="name/">name/</a></td>    ← 目录（href 以 / 结尾）
        <td>2025-12-26  </td>                 ← 最后修改时间
        <td>  - </td>                         ← 大小（目录为 -）
      </tr>

    文件行:
      <tr>
        <td><img ...></td>
        <td><a href="file.zip">file.zip</a></td>
        <td>2026-07-14 11:55  </td>
        <td> 51M</td>
      </tr>
    """

    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._page_url = page_url
        self._in_tr = False
        self._in_a = False
        self._td_index = 0  # 当前 <tr> 内第几个 <td>
        self._current_href: str | None = None
        self._current_text: str | None = None
        self._current_date: datetime | None = None
        self._current_size: str | None = None
        self.entries: list[DirectoryEntry] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._in_tr = True
            self._td_index = 0
            self._current_href = None
            self._current_text = None
            self._current_date = None
            self._current_size = None
        elif tag == "td" and self._in_tr:
            self._td_index += 1
        elif tag == "a" and self._in_tr:
            self._in_a = True
            href = dict(attrs).get("href")
            if href:
                self._current_href = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_a:
            self._in_a = False
        elif tag == "tr" and self._in_tr:
            self._in_tr = False
            self._finish_row()

    def handle_data(self, data: str) -> None:
        if not self._in_tr:
            return
        # 在 <a> 标签内: 收集链接文本（文件名或目录名）
        if self._in_a:
            if self._current_text is None:
                self._current_text = data.strip()
            else:
                self._current_text += data.strip()
            return

        # 在非 <a> 的 <td> 内: td_index 2 = 日期列, td_index 3 = 大小列
        stripped = data.strip()
        if not stripped:
            return
        if self._td_index == 3 and self._current_date is None:
            self._current_date = _parse_apache_date(stripped)
        elif self._td_index == 4 and self._current_size is None:
            self._current_size = stripped

    def _finish_row(self) -> None:
        if not self._current_href or not self._current_text:
            return
        # 跳过 Parent Directory
        if self._current_text.casefold() == "parent directory":
            return

        url = urljoin(self._page_url, self._current_href)
        self.entries.append(
            DirectoryEntry(
                name=self._current_text.rstrip("/"),
                url=url,
                is_directory=self._current_href.endswith("/"),
                last_modified=self._current_date,
                size=self._current_size,
            )
        )


def _parse_apache_date(raw: str) -> datetime | None:
    """解析 Apache 目录列表中的日期字符串。

    格式: "2026-07-14 11:49" 或 "2025-12-26  " (无时间部分)。
    """
    m = _APACHE_DATE_PATTERN.search(raw)
    if not m:
        return None
    date_str = m.group(1)
    time_str = m.group(2)
    dt_str = f"{date_str}T{time_str}:00" if time_str else f"{date_str}T00:00:00"
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        return None
