"""Zyxel Drupal Autocomplete 响应解析与递归前缀枚举。"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser

_MACHINE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_DEFAULT_INITIAL_PREFIXES = tuple("abcdefghijklmnopqrstuvwxyz0123456789")
_DEFAULT_SUFFIXES = (*_DEFAULT_INITIAL_PREFIXES, "-")


@dataclass(frozen=True)
class ProductModelEntry:
    """Autocomplete API 中的一个产品型号。"""

    machine_name: str
    display_name: str


@dataclass(frozen=True)
class EnumerationResult:
    """递归枚举结果；饱和分支表示无法确认完整。"""

    products: tuple[ProductModelEntry, ...]
    saturated_prefixes: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return not self.saturated_prefixes


type SearchAutocomplete = Callable[[str], Awaitable[list[ProductModelEntry]]]


def parse_autocomplete_response(payload: str) -> list[ProductModelEntry]:
    """解析 Drupal Autocomplete JSON，忽略缺少稳定 machine_name 的条目。"""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Zyxel Autocomplete returned invalid JSON") from exc
    if not isinstance(data, list):
        raise ValueError("Zyxel Autocomplete response must be a list")

    products: dict[str, ProductModelEntry] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        machine_name = _machine_name(item)
        if machine_name is None:
            continue
        display_name = _display_name(item) or machine_name
        products.setdefault(
            machine_name,
            ProductModelEntry(machine_name=machine_name, display_name=display_name),
        )
    return list(products.values())


async def enumerate_product_models(
    search: SearchAutocomplete,
    *,
    result_limit: int = 25,
    max_prefix_length: int = 8,
    initial_prefixes: Iterable[str] = _DEFAULT_INITIAL_PREFIXES,
    suffixes: Iterable[str] = _DEFAULT_SUFFIXES,
) -> EnumerationResult:
    """递归细分达到结果上限的前缀，并按 machine_name 去重。"""
    queue = list(initial_prefixes)
    suffix_choices = tuple(suffixes)
    seen_prefixes: set[str] = set()
    products: dict[str, ProductModelEntry] = {}
    saturated: list[str] = []

    while queue:
        prefix = queue.pop(0).casefold()
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)

        entries = await search(prefix)
        for entry in entries:
            products.setdefault(entry.machine_name, entry)

        if len(entries) < result_limit:
            continue
        if len(prefix) >= max_prefix_length:
            saturated.append(prefix)
            continue
        queue.extend(f"{prefix}{suffix}" for suffix in suffix_choices)

    return EnumerationResult(
        products=tuple(sorted(products.values(), key=lambda item: item.machine_name)),
        saturated_prefixes=tuple(sorted(saturated)),
    )


def _machine_name(item: dict[object, object]) -> str | None:
    for key in ("machine_name", "model_machine_name", "value"):
        value = item.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.strip().casefold().replace("_", "-")
        if _MACHINE_NAME.fullmatch(normalized):
            return normalized
    return None


def _display_name(item: dict[object, object]) -> str | None:
    for key in ("display_name", "model", "label"):
        value = item.get(key)
        if not isinstance(value, str):
            continue
        parser = _TextParser()
        parser.feed(value)
        parser.close()
        normalized = " ".join("".join(parser.parts).split())
        if normalized:
            return normalized
    return None


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
