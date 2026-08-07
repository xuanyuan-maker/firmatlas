"""Catalog manifest v1 DTO 测试。"""

from datetime import UTC, datetime

import pytest

from firmatlas.app.catalog_manifest import (
    MANIFEST_FORMAT_VERSION,
    CatalogCounts,
    CatalogDatabase,
    CatalogManifest,
    CatalogSource,
)
from firmatlas.domain.errors import CatalogManifestError


def make_manifest(**overrides) -> CatalogManifest:
    fields = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "lineage_id": "firmatlas-official-2026",
        "catalog_version": "2026.08.07.1",
        "created_at": datetime(2026, 8, 7, 2, 0, tzinfo=UTC),
        "schema_version": 1,
        "minimum_firmatlas_version": "1.0.0",
        "database": CatalogDatabase(
            url="firmatlas.db.gz",
            compression="gzip",
            compressed_size=123,
            uncompressed_size=456,
            compressed_sha256="a" * 64,
            database_sha256="b" * 64,
        ),
        "counts": CatalogCounts(sources=1, products=2, releases=3, artifacts=4, downloads=0),
        "sources": (
            CatalogSource(
                source_key="tp-link-cn",
                last_success_at=datetime(2026, 8, 7, 1, 0, tzinfo=UTC),
                last_status="completed",
                products=2,
                releases=3,
                artifacts=4,
            ),
        ),
    }
    fields.update(overrides)
    return CatalogManifest(**fields)


def test_manifest_json_round_trip_is_deterministic():
    manifest = make_manifest()

    encoded = manifest.to_json()
    decoded = CatalogManifest.from_json(encoded)

    assert decoded == manifest
    assert encoded == manifest.to_json()
    assert '"format_version": 1' in encoded
    assert encoded.endswith("\n")


def test_manifest_rejects_unknown_root_field():
    payload = make_manifest().to_dict()
    payload["unexpected"] = True

    with pytest.raises(CatalogManifestError, match="未知字段：unexpected"):
        CatalogManifest.from_dict(payload)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("format_version", 2, "不支持的 manifest format_version"),
        ("schema_version", 0, "schema_version 必须大于 0"),
        ("database.compressed_sha256", "bad", "必须是 64 位"),
        ("counts.artifacts", -1, "不能小于 0"),
    ],
)
def test_manifest_rejects_invalid_values(path, value, message):
    payload = make_manifest().to_dict()
    target = payload
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value

    with pytest.raises(CatalogManifestError, match=message):
        CatalogManifest.from_dict(payload)


def test_manifest_rejects_count_source_mismatch():
    payload = make_manifest().to_dict()
    payload["counts"]["sources"] = 2

    with pytest.raises(CatalogManifestError, match="实际数量不一致"):
        CatalogManifest.from_dict(payload)


def test_manifest_rejects_duplicate_source_keys():
    payload = make_manifest(
        sources=(
            make_manifest().sources[0],
            CatalogSource(
                source_key="tp-link-cn",
                last_success_at=None,
                last_status="failed",
                products=0,
                releases=0,
                artifacts=0,
            ),
        ),
        counts=CatalogCounts(sources=2, products=2, releases=3, artifacts=4, downloads=0),
    ).to_dict()

    with pytest.raises(CatalogManifestError, match="不得重复"):
        CatalogManifest.from_dict(payload)


def test_manifest_rejects_malformed_json():
    with pytest.raises(CatalogManifestError, match="不是有效 JSON"):
        CatalogManifest.from_json("{")
