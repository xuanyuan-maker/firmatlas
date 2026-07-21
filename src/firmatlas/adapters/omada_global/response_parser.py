"""Omada Worldwide 公开 API 响应解析。

解析器只把 JSON 响应转换成不可变数据，不访问网络或数据库。型号分类和领域候选
对象映射由适配器负责；标题无法识别时保留原始条目，供适配器记录跳过原因。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FirmwareModelEntry:
    """型号目录接口中的一个产品。"""

    model_id: int
    model_name: str
    image_url: str | None


@dataclass(frozen=True)
class FirmwareTitle:
    """从固件标题中提取的显式身份字段。"""

    model_name: str
    region: str
    hardware_revision: str
    version_raw: str
    version_normalized: str


@dataclass(frozen=True)
class FirmwareEntry:
    """固件接口中的一个发布条目。"""

    title: str
    parsed_title: FirmwareTitle | None
    download_url: str | None
    size_text: str | None
    language: str | None
    publish_date_text: str | None
    notes_html: str | None
    modifications_html: str | None
    release_notes_url: str | None


_TITLE_PATTERN = re.compile(
    r"^(?P<model>.+?)\((?P<region>[A-Z0-9-]+)\)\s*_"
    r"(?P<hardware>V\d+(?:\.\d+)*)_(?P<version>.+)$",
    re.IGNORECASE,
)


def parse_model_response(payload: str) -> list[FirmwareModelEntry]:
    """解析型号目录响应；无效成功响应抛出 ``ValueError``。"""
    result = _parse_result_list(payload)
    models: list[FirmwareModelEntry] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        model_id = item.get("modelId")
        model_name = _optional_string(item.get("modelName"))
        if not isinstance(model_id, int) or model_name is None:
            continue
        models.append(
            FirmwareModelEntry(
                model_id=model_id,
                model_name=model_name,
                image_url=_optional_string(item.get("defaultImageUrl")),
            )
        )
    return models


def parse_firmware_response(payload: str) -> list[FirmwareEntry]:
    """解析某型号的固件历史响应。"""
    result = _parse_result_list(payload)
    entries: list[FirmwareEntry] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        title = _optional_string(item.get("title")) or _optional_string(item.get("name"))
        if title is None:
            continue
        entries.append(
            FirmwareEntry(
                title=title,
                parsed_title=parse_firmware_title(title),
                download_url=_optional_string(item.get("awsUrl")),
                size_text=_optional_string(item.get("size")),
                language=_optional_string(item.get("language")),
                publish_date_text=_optional_string(item.get("publishDate")),
                notes_html=_optional_string(item.get("notes")),
                modifications_html=_optional_string(item.get("modificationsAndBugFixes")),
                release_notes_url=_optional_string(item.get("releaseNotesUrl")),
            )
        )
    return entries


def parse_firmware_title(title: str) -> FirmwareTitle | None:
    """解析标题中的型号、地区、硬件版本和固件版本。"""
    matched = _TITLE_PATTERN.fullmatch(title.strip())
    if matched is None:
        return None

    version_raw = matched.group("version").strip()
    version_normalized = " ".join(version_raw.replace("_", " ").split())
    return FirmwareTitle(
        model_name=matched.group("model").strip(),
        region=matched.group("region").upper(),
        hardware_revision=matched.group("hardware").upper(),
        version_raw=version_raw,
        version_normalized=version_normalized,
    )


def _parse_result_list(payload: str) -> list[Any]:
    try:
        envelope = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Omada API returned invalid JSON") from exc

    if not isinstance(envelope, dict):
        raise ValueError("Omada API response must be an object")
    if envelope.get("errorCode") != 0:
        message = _optional_string(envelope.get("message")) or "unknown error"
        raise ValueError(f"Omada API request failed: {message}")

    result = envelope.get("result")
    if not isinstance(result, list):
        raise ValueError("Omada API result must be a list")
    return result


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
