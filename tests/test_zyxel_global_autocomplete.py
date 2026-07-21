"""Zyxel Global Autocomplete 解析与递归枚举测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from firmatlas.adapters.zyxel_global.autocomplete import (
    ProductModelEntry,
    enumerate_product_models,
    parse_autocomplete_response,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "zyxel-global"


def test_parse_autocomplete_response_supports_drupal_field_variants() -> None:
    entries = parse_autocomplete_response(
        (FIXTURE_DIR / "autocomplete-usg.json").read_text(encoding="utf-8")
    )

    assert [(entry.machine_name, entry.display_name) for entry in entries] == [
        ("usg-flex-100h", "USG FLEX 100H"),
        ("usg-flex-200h", "USG FLEX 200H"),
        ("gs1920-24hpv2", "GS1920-24HPv2"),
    ]


def test_invalid_autocomplete_envelope_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_autocomplete_response('{"value":"usg-flex-100h"}')


@pytest.mark.anyio
async def test_saturated_prefix_is_recursively_split_and_deduplicated() -> None:
    responses = {
        "a": [
            ProductModelEntry("a-model", "A Model"),
            ProductModelEntry("aa-one", "AA One"),
            ProductModelEntry("ab-one", "AB One"),
        ],
        "aa": [ProductModelEntry("aa-one", "AA One")],
        "ab": [ProductModelEntry("ab-one", "AB One")],
    }
    calls: list[str] = []

    async def search(prefix: str) -> list[ProductModelEntry]:
        calls.append(prefix)
        return responses.get(prefix, [])

    result = await enumerate_product_models(
        search,
        result_limit=3,
        max_prefix_length=2,
        initial_prefixes=("a",),
        suffixes=("a", "b"),
    )

    assert result.is_complete is True
    assert calls == ["a", "aa", "ab"]
    assert [entry.machine_name for entry in result.products] == [
        "a-model",
        "aa-one",
        "ab-one",
    ]


@pytest.mark.anyio
async def test_saturated_max_depth_marks_enumeration_incomplete() -> None:
    async def search(prefix: str) -> list[ProductModelEntry]:
        return [ProductModelEntry(f"{prefix}-{index}", f"Model {index}") for index in range(3)]

    result = await enumerate_product_models(
        search,
        result_limit=3,
        max_prefix_length=1,
        initial_prefixes=("a",),
        suffixes=("a", "b"),
    )

    assert result.is_complete is False
    assert result.saturated_prefixes == ("a",)
