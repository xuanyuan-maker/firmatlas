"""Omada Worldwide API 响应解析器测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from firmatlas.adapters.omada_global.response_parser import (
    parse_firmware_response,
    parse_firmware_title,
    parse_model_response,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "omada-global"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def test_parse_model_response_preserves_stable_model_id() -> None:
    models = parse_model_response(_load("model-list.json"))

    assert [(model.model_id, model.model_name) for model in models] == [
        (1402, "ER605"),
        (110, "EAP225"),
        (968, "OC200"),
    ]
    assert models[0].image_url == "https://static.tp-link.com/example/er605.png"


def test_parse_firmware_response_preserves_metadata() -> None:
    entries = parse_firmware_response(_load("firmware-samples.json"))

    assert len(entries) == 3
    entry = entries[0]
    assert entry.title == "ER605(UN) _V2.20_2.4.4 Build 20260630"
    assert entry.download_url == "https://static.tp-link.com/example/ER605_V2.20_2.4.4.zip"
    assert entry.size_text == "26.20 MB"
    assert entry.language == "English"
    assert entry.publish_date_text == "07-16-2026"
    assert entry.notes_html == "<p>Minimum firmware version applies.</p>"
    assert entry.modifications_html == "<p>Improved system stability.</p>"
    assert entry.release_notes_url == ("https://static.tp-link.com/example/ER605_V2.20_2.4.4.pdf")


@pytest.mark.parametrize(
    ("title", "model", "region", "hardware", "version_raw", "version_normalized"),
    [
        (
            "ER605(UN) _V2.20_2.4.4 Build 20260630",
            "ER605",
            "UN",
            "V2.20",
            "2.4.4 Build 20260630",
            "2.4.4 Build 20260630",
        ),
        (
            "EAP225(EU)_V3_5.2.3 Build 20250709",
            "EAP225",
            "EU",
            "V3",
            "5.2.3 Build 20250709",
            "5.2.3 Build 20250709",
        ),
        (
            "ER605(UN)_V2.30_2.4.3_Build 20260512",
            "ER605",
            "UN",
            "V2.30",
            "2.4.3_Build 20260512",
            "2.4.3 Build 20260512",
        ),
        (
            "ER7212PC(UN)_V2.20_2.3.1_20260117",
            "ER7212PC",
            "UN",
            "V2.20",
            "2.3.1_20260117",
            "2.3.1 20260117",
        ),
    ],
)
def test_parse_firmware_title_variants(
    title: str,
    model: str,
    region: str,
    hardware: str,
    version_raw: str,
    version_normalized: str,
) -> None:
    result = parse_firmware_title(title)

    assert result is not None
    assert result.model_name == model
    assert result.region == region
    assert result.hardware_revision == hardware
    assert result.version_raw == version_raw
    assert result.version_normalized == version_normalized


def test_unrecognized_title_is_retained_for_adapter_skip_reporting() -> None:
    payload = (
        '{"errorCode":0,"result":[{"title":"legacy firmware",'
        '"awsUrl":"https://static.tp-link.com/example/legacy.zip"}]}'
    )

    entries = parse_firmware_response(payload)

    assert len(entries) == 1
    assert entries[0].title == "legacy firmware"
    assert entries[0].parsed_title is None


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        '{"errorCode":-20003,"message":"request rejected","result":null}',
        '{"errorCode":0,"result":null}',
    ],
)
def test_invalid_api_envelope_raises_value_error(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_firmware_response(payload)
