"""Catalog manifest v1 的严格 DTO 与 JSON 编解码。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from firmatlas.domain.errors import CatalogManifestError
from firmatlas.domain.timeutil import format_rfc3339, parse_rfc3339

MANIFEST_FORMAT_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CatalogDatabase:
    url: str
    compression: str
    compressed_size: int
    uncompressed_size: int
    compressed_sha256: str
    database_sha256: str


@dataclass(frozen=True)
class CatalogCounts:
    sources: int
    products: int
    releases: int
    artifacts: int
    downloads: int


@dataclass(frozen=True)
class CatalogSource:
    source_key: str
    last_success_at: datetime | None
    last_status: str
    products: int
    releases: int
    artifacts: int


@dataclass(frozen=True)
class CatalogManifest:
    format_version: int
    lineage_id: str
    catalog_version: str
    created_at: datetime
    schema_version: int
    minimum_firmatlas_version: str
    database: CatalogDatabase
    counts: CatalogCounts
    sources: tuple[CatalogSource, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "lineage_id": self.lineage_id,
            "catalog_version": self.catalog_version,
            "created_at": format_rfc3339(self.created_at),
            "schema_version": self.schema_version,
            "minimum_firmatlas_version": self.minimum_firmatlas_version,
            "database": {
                "url": self.database.url,
                "compression": self.database.compression,
                "compressed_size": self.database.compressed_size,
                "uncompressed_size": self.database.uncompressed_size,
                "compressed_sha256": self.database.compressed_sha256,
                "database_sha256": self.database.database_sha256,
            },
            "counts": {
                "sources": self.counts.sources,
                "products": self.counts.products,
                "releases": self.counts.releases,
                "artifacts": self.counts.artifacts,
                "downloads": self.counts.downloads,
            },
            "sources": [
                {
                    "source_key": source.source_key,
                    "last_success_at": (
                        format_rfc3339(source.last_success_at)
                        if source.last_success_at is not None
                        else None
                    ),
                    "last_status": source.last_status,
                    "products": source.products,
                    "releases": source.releases,
                    "artifacts": source.artifacts,
                }
                for source in self.sources
            ],
        }

    def to_json(self) -> str:
        """序列化为确定性 JSON，末尾带换行，便于发布与审查。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, value: object) -> CatalogManifest:
        root = _object(value, "manifest")
        _exact_keys(
            root,
            {
                "format_version",
                "lineage_id",
                "catalog_version",
                "created_at",
                "schema_version",
                "minimum_firmatlas_version",
                "database",
                "counts",
                "sources",
            },
            "manifest",
        )

        format_version = _int(root["format_version"], "format_version")
        if format_version != MANIFEST_FORMAT_VERSION:
            raise CatalogManifestError(
                f"不支持的 manifest format_version：{format_version}，"
                f"当前支持 {MANIFEST_FORMAT_VERSION}。"
            )
        schema_version = _positive_int(root["schema_version"], "schema_version")
        created_at = _timestamp(root["created_at"], "created_at")
        lineage_id = _non_empty_string(root["lineage_id"], "lineage_id")
        catalog_version = _non_empty_string(root["catalog_version"], "catalog_version")
        minimum_version = _non_empty_string(
            root["minimum_firmatlas_version"], "minimum_firmatlas_version"
        )

        database = _database(root["database"])
        counts = _counts(root["counts"])
        sources_value = root["sources"]
        if not isinstance(sources_value, list):
            raise CatalogManifestError("manifest.sources 必须是数组。")
        sources = tuple(_source(item) for item in sources_value)
        source_keys = [source.source_key for source in sources]
        if len(source_keys) != len(set(source_keys)):
            raise CatalogManifestError("manifest.sources 中的 source_key 不得重复。")
        if counts.sources != len(sources):
            raise CatalogManifestError(
                f"manifest.counts.sources={counts.sources} 与 sources 实际数量不一致。"
            )

        return cls(
            format_version=format_version,
            lineage_id=lineage_id,
            catalog_version=catalog_version,
            created_at=created_at,
            schema_version=schema_version,
            minimum_firmatlas_version=minimum_version,
            database=database,
            counts=counts,
            sources=sources,
        )

    @classmethod
    def from_json(cls, text: str) -> CatalogManifest:
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CatalogManifestError(f"manifest 不是有效 JSON：{exc}") from exc
        return cls.from_dict(value)


def _database(value: object) -> CatalogDatabase:
    data = _object(value, "database")
    _exact_keys(
        data,
        {
            "url",
            "compression",
            "compressed_size",
            "uncompressed_size",
            "compressed_sha256",
            "database_sha256",
        },
        "database",
    )
    compression = _non_empty_string(data["compression"], "database.compression")
    if compression != "gzip":
        raise CatalogManifestError("database.compression 当前必须是 gzip。")
    return CatalogDatabase(
        url=_non_empty_string(data["url"], "database.url"),
        compression=compression,
        compressed_size=_non_negative_int(data["compressed_size"], "database.compressed_size"),
        uncompressed_size=_non_negative_int(
            data["uncompressed_size"], "database.uncompressed_size"
        ),
        compressed_sha256=_sha256(data["compressed_sha256"], "database.compressed_sha256"),
        database_sha256=_sha256(data["database_sha256"], "database.database_sha256"),
    )


def _counts(value: object) -> CatalogCounts:
    data = _object(value, "counts")
    _exact_keys(data, {"sources", "products", "releases", "artifacts", "downloads"}, "counts")
    return CatalogCounts(
        sources=_non_negative_int(data["sources"], "counts.sources"),
        products=_non_negative_int(data["products"], "counts.products"),
        releases=_non_negative_int(data["releases"], "counts.releases"),
        artifacts=_non_negative_int(data["artifacts"], "counts.artifacts"),
        downloads=_non_negative_int(data["downloads"], "counts.downloads"),
    )


def _source(value: object) -> CatalogSource:
    data = _object(value, "sources[]")
    _exact_keys(
        data,
        {"source_key", "last_success_at", "last_status", "products", "releases", "artifacts"},
        "sources[]",
    )
    last_success_at = data["last_success_at"]
    return CatalogSource(
        source_key=_non_empty_string(data["source_key"], "sources[].source_key"),
        last_success_at=(
            None
            if last_success_at is None
            else _timestamp(last_success_at, "sources[].last_success_at")
        ),
        last_status=_non_empty_string(data["last_status"], "sources[].last_status"),
        products=_non_negative_int(data["products"], "sources[].products"),
        releases=_non_negative_int(data["releases"], "sources[].releases"),
        artifacts=_non_negative_int(data["artifacts"], "sources[].artifacts"),
    )


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogManifestError(f"manifest.{field} 必须是对象。")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise CatalogManifestError(f"{field} 包含未知字段：{', '.join(unknown)}。")
    if missing:
        raise CatalogManifestError(f"{field} 缺少字段：{', '.join(missing)}。")


def _non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogManifestError(f"{field} 必须是非空字符串。")
    return value


def _int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogManifestError(f"{field} 必须是整数。")
    return value


def _positive_int(value: object, field: str) -> int:
    result = _int(value, field)
    if result <= 0:
        raise CatalogManifestError(f"{field} 必须大于 0。")
    return result


def _non_negative_int(value: object, field: str) -> int:
    result = _int(value, field)
    if result < 0:
        raise CatalogManifestError(f"{field} 不能小于 0。")
    return result


def _sha256(value: object, field: str) -> str:
    result = _non_empty_string(value, field)
    if _SHA256_RE.fullmatch(result) is None:
        raise CatalogManifestError(f"{field} 必须是 64 位小写十六进制 SHA-256。")
    return result


def _timestamp(value: object, field: str) -> datetime:
    text = _non_empty_string(value, field)
    try:
        return parse_rfc3339(text)
    except ValueError as exc:
        raise CatalogManifestError(f"{field} 不是有效的 RFC 3339 时间。") from exc
