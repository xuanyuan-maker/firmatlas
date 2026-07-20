"""海康威视国际站固件目录 HTML 解析。

国际站把完整目录直接渲染在一个 HTML 页面中，浏览器中的分页和搜索不会请求分页 API。
本模块只把 HTML 翻译为与页面结构对应的不可变数据：

产品目录项 → 固件适用分组 → 固件文件 / 发布说明 / 适用型号。

分类筛选、领域对象映射和 ``source_key`` 生成属于适配器职责，不放在纯解析器中。
解析器不触网、不访问数据库，只使用 Python 标准库，便于用固定 fixture 独立测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass(frozen=True)
class FirmwareAssetEntry:
    """页面中一个 Firmware 下载链接。"""

    title: str
    download_url: str | None


@dataclass(frozen=True)
class ReleaseNoteEntry:
    """页面中一个 Release notes 链接。"""

    title: str
    url: str | None


@dataclass(frozen=True)
class FirmwareGroupEntry:
    """一个固件适用分组，可包含多个版本或同版本的地域变体。"""

    firmware_assets: tuple[FirmwareAssetEntry, ...]
    release_notes: tuple[ReleaseNoteEntry, ...]
    applied_models: tuple[str, ...]


@dataclass(frozen=True)
class FirmwareProductEntry:
    """国际站目录中的一个父级产品项。"""

    title: str
    product_url: str | None
    main_category: str
    sub_category: str
    groups: tuple[FirmwareGroupEntry, ...]


def parse_firmware_products(html: str) -> list[FirmwareProductEntry]:
    """解析国际站固件页中的全部产品项，保留原始分类和分组关系。"""
    parser = _FirmwareListingParser()
    parser.feed(html)
    parser.close()
    return parser.products


_VERSION_PATTERN = re.compile(r"(?i)(?<![A-Z0-9])V\d+(?:\.\d+)+(?:[_ ](?:BUILD[ _]?)?\d{6})?")


def extract_firmware_version(title: str) -> str | None:
    """从固件标题提取来源版本片段；无法确认时返回 ``None``。

    例如 ``Firmware_Europe_V4.30.122_201107`` 返回
    ``V4.30.122_201107``。日期仍是版本/build 的一部分，不在这里冒充发布日期。
    """
    matched = _VERSION_PATTERN.search(title)
    return matched.group(0) if matched else None


@dataclass
class _FirmwareGroupBuilder:
    firmware_assets: list[FirmwareAssetEntry] = field(default_factory=list)
    release_notes: list[ReleaseNoteEntry] = field(default_factory=list)
    applied_models: list[str] = field(default_factory=list)

    def freeze(self) -> FirmwareGroupEntry:
        return FirmwareGroupEntry(
            firmware_assets=tuple(self.firmware_assets),
            release_notes=tuple(self.release_notes),
            applied_models=tuple(self.applied_models),
        )


@dataclass
class _FirmwareProductBuilder:
    main_category: str
    sub_category: str
    title: str = ""
    product_url: str | None = None
    groups: list[FirmwareGroupEntry] = field(default_factory=list)

    def freeze(self) -> FirmwareProductEntry:
        return FirmwareProductEntry(
            title=self.title,
            product_url=self.product_url,
            main_category=self.main_category,
            sub_category=self.sub_category,
            groups=tuple(self.groups),
        )


@dataclass(frozen=True)
class _ElementState:
    tag: str
    classes: frozenset[str]


@dataclass
class _PendingLink:
    kind: str
    title: str
    url: str | None
    text_parts: list[str] = field(default_factory=list)


class _FirmwareListingParser(HTMLParser):
    """根据国际站稳定 class 名称解析目录的轻量状态机。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.products: list[FirmwareProductEntry] = []
        self._stack: list[_ElementState] = []
        self._product: _FirmwareProductBuilder | None = None
        self._group: _FirmwareGroupBuilder | None = None
        self._pending_link: _PendingLink | None = None
        self._model_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = frozenset((attr.get("class") or "").split())

        if tag == "div" and "nav-item" in classes:
            self._finish_product()
            self._product = _FirmwareProductBuilder(
                main_category=attr.get("data-main-tag") or "",
                sub_category=attr.get("data-sub-tag") or "",
            )
        elif tag == "div" and "main-item" in classes and self._product is not None:
            self._finish_group()
            self._group = _FirmwareGroupBuilder()
        elif tag == "a" and self._product is not None:
            self._start_link(attr)
        elif tag == "li" and "sub-item" in classes and self._group is not None:
            self._finish_model()
            self._model_parts = []

        self._stack.append(_ElementState(tag=tag, classes=classes))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "ul" and self._model_parts is not None:
            self._finish_model()
        state = self._pop_state(tag)
        if state is None:
            return

        if state.tag == "a" and self._pending_link is not None:
            self._finish_link()
        elif state.tag == "li" and "sub-item" in state.classes:
            self._finish_model()
        elif state.tag == "div" and "main-item" in state.classes:
            self._finish_group()
        elif state.tag == "div" and "nav-item" in state.classes:
            self._finish_product()

    def handle_data(self, data: str) -> None:
        if self._pending_link is not None:
            self._pending_link.text_parts.append(data)
        if self._model_parts is not None:
            self._model_parts.append(data)

    def close(self) -> None:
        super().close()
        self._finish_product()

    def _start_link(self, attr: dict[str, str | None]) -> None:
        kind: str | None = None
        if self._inside_class("main-title") and "link" in (attr.get("class") or "").split():
            kind = "product"
        elif self._group is not None and self._inside_class("firmware-section"):
            kind = "firmware"
        elif self._group is not None and self._inside_class("release-section"):
            kind = "release_note"

        if kind is None:
            return

        url = attr.get("data-href") or attr.get("data-link") or attr.get("href")
        if url and url.startswith("#"):
            url = None
        self._pending_link = _PendingLink(
            kind=kind,
            title=attr.get("data-title") or "",
            url=url,
        )

    def _finish_link(self) -> None:
        assert self._pending_link is not None
        pending = self._pending_link
        title = pending.title or _normalized_text(pending.text_parts)

        if pending.kind == "product" and self._product is not None:
            self._product.title = title
            self._product.product_url = pending.url
        elif pending.kind == "firmware" and self._group is not None:
            self._group.firmware_assets.append(
                FirmwareAssetEntry(title=title, download_url=pending.url)
            )
        elif pending.kind == "release_note" and self._group is not None:
            self._group.release_notes.append(ReleaseNoteEntry(title=title, url=pending.url))

        self._pending_link = None

    def _finish_model(self) -> None:
        if self._model_parts is None:
            return
        model = _normalized_text(self._model_parts)
        if model and self._group is not None:
            self._group.applied_models.append(model)
        self._model_parts = None

    def _finish_group(self) -> None:
        self._finish_model()
        if self._group is not None and self._product is not None:
            self._product.groups.append(self._group.freeze())
        self._group = None

    def _finish_product(self) -> None:
        self._finish_group()
        if self._product is not None:
            self.products.append(self._product.freeze())
        self._product = None
        self._pending_link = None

    def _inside_class(self, class_name: str) -> bool:
        return any(class_name in state.classes for state in self._stack)

    def _pop_state(self, tag: str) -> _ElementState | None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == tag:
                state = self._stack[index]
                del self._stack[index:]
                return state
        return None


def _normalized_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())
