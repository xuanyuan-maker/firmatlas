"""解析 D-Link 美国支持站公开的 IIS 资源目录页。"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


@dataclass(frozen=True)
class DirectoryEntry:
    """目录页中的一个文件或子目录链接。"""

    name: str
    url: str
    is_directory: bool


def parse_directory_listing(html: str, page_url: str) -> tuple[DirectoryEntry, ...]:
    """提取 IIS 目录页的直接子项，并忽略返回上级目录的链接。"""
    parser = _DirectoryListingParser(page_url)
    parser.feed(html)
    parser.close()
    return tuple(parser.entries)


@dataclass
class _PendingLink:
    href: str
    text_parts: list[str]


class _DirectoryListingParser(HTMLParser):
    """只读取 ``pre`` 中的链接，避免把普通错误页导航误当资源。"""

    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._page_url = page_url
        self._pre_depth = 0
        self._pending: _PendingLink | None = None
        self.entries: list[DirectoryEntry] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "pre":
            self._pre_depth += 1
            return
        if tag != "a" or self._pre_depth == 0:
            return

        href = dict(attrs).get("href")
        if href:
            self._pending = _PendingLink(href=href, text_parts=[])

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._pending is not None:
            self._finish_link()
        elif tag == "pre" and self._pre_depth:
            self._pre_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._pending is not None:
            self._pending.text_parts.append(data)

    def _finish_link(self) -> None:
        assert self._pending is not None
        pending = self._pending
        self._pending = None

        name = " ".join("".join(pending.text_parts).split())
        if not name or name.casefold() == "[to parent directory]":
            return

        url = urljoin(self._page_url, pending.href)
        path = urlsplit(url).path
        self.entries.append(
            DirectoryEntry(
                name=name,
                url=url,
                is_directory=path.endswith("/"),
            )
        )
