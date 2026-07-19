"""tp-link-us 下载页 HTML 解析（阶段 5）。

## 这个模块解决什么问题

US 下载页是服务端渲染的 HTML（与 CN 的 JSON API 不同）。本模块用 Python
标准库 html.parser（不引入第三方解析依赖）从下载页 HTML 提取两类信息：

1. 硬件版本列表（parse_hardware_versions）：多硬件版本型号的主页含
   `<ul id="version-list">`，每个 `<li>` 内的 `<a href>` 指向硬件版本子页。
   单硬件版本型号无此列表（固件表直接在主页），返回空列表。

2. 固件条目（parse_firmware_entries）：每个 `<table class="download-resource-table">`
   是一个固件条目：
   - 标题：`th.download-resource-name > p`（如 "Archer BE670(US)_V1.6_1.0.2 Build 20251203"）
   - 下载真链：弹窗内 `a.tp-dialog-btn[href^="https://static.tp-link.com"]`
     （另有 "Go to Local Website" 链接指向本地站，须排除）
   - 明细：`tr.detail-info` 内成对 `<span>`（Published Date / Language / File Size）
   - 部分条目（如 Tapo 摄像头）无下载链接，download_url 为 None。

本模块是纯逻辑（输入 HTML 字符串），不触网、不碰数据库，可独立单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser

# 固件下载真链的域名前缀（区别于 "Go to Local Website" 的本地站链接）
_STATIC_DOWNLOAD_PREFIX = "https://static.tp-link.com/"


@dataclass(frozen=True)
class HardwareVersionLink:
    """主页 version-list 里的一个硬件版本入口。"""

    version_label: str   # 如 "V3"、"V5.60"
    url: str             # 硬件版本子页绝对 URL


@dataclass(frozen=True)
class FirmwareEntry:
    """一个固件条目（一个 download-resource-table）。"""

    title: str                    # 完整标题（含型号/硬件版本/固件版本/日期）
    download_url: str | None      # 下载真链；无可下载文件时为 None
    published_date: str | None    # 如 "2026-01-26"
    file_size_text: str | None    # 原始大小文本，如 "18.66 MB"
    language: str | None          # 如 "Multi-language"


def parse_hardware_versions(html: str) -> list[HardwareVersionLink]:
    """解析主页 version-list，返回硬件版本子页链接（无则返回空列表）。"""
    parser = _VersionListParser()
    parser.feed(html)
    return parser.links


def parse_firmware_entries(html: str) -> list[FirmwareEntry]:
    """解析页面上所有 download-resource-table，返回固件条目列表。"""
    parser = _FirmwareTableParser()
    parser.feed(html)
    return parser.entries


# ---------------------------------------------------------------------------
# version-list 解析
# ---------------------------------------------------------------------------


class _VersionListParser(HTMLParser):
    """提取 <ul id="version-list"> 内每个 <li> 的 <a href> 与文本。"""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[HardwareVersionLink] = []
        self._in_list = False
        self._depth = 0            # 进入 version-list 后的标签深度（用于识别列表结束）
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "ul" and attr.get("id") == "version-list":
            self._in_list = True
            self._depth = 0
            return
        if not self._in_list:
            return
        self._depth += 1
        if tag == "a" and attr.get("href"):
            self._current_href = attr["href"]
            self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        if not self._in_list:
            return
        if tag == "a" and self._current_href is not None:
            label = "".join(self._current_text).strip()
            self.links.append(
                HardwareVersionLink(version_label=label, url=self._current_href)
            )
            self._current_href = None
            self._current_text = []
        if tag == "ul" and self._depth == 0:
            self._in_list = False
            return
        self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_list and self._current_href is not None:
            self._current_text.append(data)


# ---------------------------------------------------------------------------
# download-resource-table 解析
# ---------------------------------------------------------------------------


def _has_class(attr: dict[str, str | None], name: str) -> bool:
    """判断标签的 class 属性是否包含指定类名。"""
    classes = (attr.get("class") or "").split()
    return name in classes


class _FirmwareTableParser(HTMLParser):
    """状态机：逐个解析 download-resource-table 提取固件条目。

    识别锚点（都是稳定 class）：
    - table.download-resource-table       → 一个固件条目开始
    - th/td.download-resource-name         → 标题（其内 <p> 文本）
    - a.tp-dialog-btn 且 href 以 static 前缀开头 → 下载真链
    - tr.detail-info 内成对 <span>          → Published Date / Language / File Size
    """

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[FirmwareEntry] = []

        self._in_table = False
        self._in_name = False       # 在 download-resource-name 里
        self._name_parts: list[str] = []
        self._in_detail = False     # 在 tr.detail-info 里
        self._span_texts: list[str] = []  # detail-info 内所有 span 文本（顺序）
        self._collecting_span = False
        self._download_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)

        if tag == "table" and _has_class(attr, "download-resource-table"):
            self._begin_table()
            return
        if not self._in_table:
            return

        if tag in ("th", "td") and _has_class(attr, "download-resource-name"):
            self._in_name = True
            self._name_parts = []
        elif tag == "tr" and _has_class(attr, "detail-info"):
            self._in_detail = True
        elif tag == "span" and self._in_detail:
            self._collecting_span = True
            self._span_texts.append("")
        elif tag == "a" and _has_class(attr, "tp-dialog-btn"):
            href = attr.get("href") or ""
            # 只认 static 下载域名，排除 "Go to Local Website" 本地站链接
            if href.startswith(_STATIC_DOWNLOAD_PREFIX) and self._download_url is None:
                self._download_url = href

    def handle_endtag(self, tag: str) -> None:
        if not self._in_table:
            return
        if tag in ("th", "td") and self._in_name:
            self._in_name = False
        elif tag == "tr" and self._in_detail:
            self._in_detail = False
        elif tag == "span" and self._collecting_span:
            self._collecting_span = False
        elif tag == "table":
            self._end_table()

    def handle_data(self, data: str) -> None:
        if not self._in_table:
            return
        if self._in_name:
            self._name_parts.append(data)
        elif self._collecting_span:
            self._span_texts[-1] += data

    # -- 条目边界 --

    def _begin_table(self) -> None:
        self._in_table = True
        self._in_name = False
        self._name_parts = []
        self._in_detail = False
        self._span_texts = []
        self._collecting_span = False
        self._download_url = None

    def _end_table(self) -> None:
        title = "".join(self._name_parts).strip()
        if title:
            self.entries.append(
                FirmwareEntry(
                    title=title,
                    download_url=self._download_url,
                    published_date=_extract_labeled(self._span_texts, "Published Date"),
                    file_size_text=_extract_labeled(self._span_texts, "File Size"),
                    language=_extract_labeled(self._span_texts, "Language"),
                )
            )
        self._in_table = False


def _extract_labeled(spans: list[str], label: str) -> str | None:
    """detail-info 里 span 成对出现：["Published Date: ", "2026-01-26 ", ...]。

    找到文本以 label 开头的 span，取其下一个 span 作为值。
    """
    for i, text in enumerate(spans):
        if text.strip().rstrip(":").strip() == label and i + 1 < len(spans):
            value = spans[i + 1].strip()
            return value or None
    return None
