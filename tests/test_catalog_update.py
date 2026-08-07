"""Catalog update --check 用例测试。"""

from datetime import UTC, datetime

from firmatlas.app.catalog_manifest import CatalogCounts, CatalogDatabase, CatalogManifest
from firmatlas.app.catalog_update import check_catalog_update
from firmatlas.app.config import AppConfig, CatalogConfig
from firmatlas.domain.errors import CatalogUpdateError


def make_manifest(*, version="2026.08.07.1", lineage="11111111-1111-4111-8111-111111111111"):
    return CatalogManifest(
        format_version=1,
        lineage_id=lineage,
        catalog_version=version,
        created_at=datetime(2026, 8, 7, tzinfo=UTC),
        schema_version=1,
        minimum_firmatlas_version="1.0.0",
        database=CatalogDatabase(
            url="firmatlas.db.gz",
            compression="gzip",
            compressed_size=1,
            uncompressed_size=2,
            compressed_sha256="a" * 64,
            database_sha256="b" * 64,
        ),
        counts=CatalogCounts(sources=0, products=2, releases=3, artifacts=4, downloads=0),
        sources=(),
    )


def test_check_reports_update_and_replace_for_first_install(tmp_path, monkeypatch):
    remote = make_manifest()
    monkeypatch.setattr(
        "firmatlas.app.catalog_update.fetch_manifest", lambda *args, **kwargs: remote
    )
    config = AppConfig(
        data_dir=tmp_path,
        catalog=CatalogConfig(mode="managed", manifest_url="file:///manifest.json"),
    )

    report = check_catalog_update(data_dir=tmp_path, config=config)

    assert report.update_available is True
    assert report.replace_required is True
    assert report.current_catalog_version is None


def test_check_reports_no_update_for_same_version(tmp_path, monkeypatch):
    remote = make_manifest()
    (tmp_path / "catalog-manifest.json").write_text(remote.to_json(), encoding="utf-8")
    monkeypatch.setattr(
        "firmatlas.app.catalog_update.fetch_manifest", lambda *args, **kwargs: remote
    )
    config = AppConfig(
        data_dir=tmp_path,
        catalog=CatalogConfig(mode="managed", manifest_url="file:///manifest.json"),
    )

    report = check_catalog_update(data_dir=tmp_path, config=config)

    assert report.update_available is False
    assert report.replace_required is False


def test_check_rejects_standalone(tmp_path):
    config = AppConfig(data_dir=tmp_path)

    try:
        check_catalog_update(data_dir=tmp_path, config=config)
    except CatalogUpdateError as exc:
        assert "Standalone" in str(exc)
    else:
        raise AssertionError("expected CatalogUpdateError")
