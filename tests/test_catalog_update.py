"""Catalog update --check 用例测试。"""

import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256

import pytest

import firmatlas.app.catalog_update as catalog_update_module
from firmatlas.app.catalog_export import export_catalog
from firmatlas.app.catalog_manifest import CatalogCounts, CatalogDatabase, CatalogManifest
from firmatlas.app.catalog_update import (
    check_catalog_update,
    update_catalog,
    validate_candidate_database,
)
from firmatlas.app.config import AppConfig, CatalogConfig
from firmatlas.domain.errors import CatalogUpdateError
from firmatlas.infra.database import initialize
from tests.test_catalog_export import _seed_catalog


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


def test_update_returns_up_to_date_without_replacing_database(tmp_path, monkeypatch):
    remote = make_manifest()
    (tmp_path / "catalog-manifest.json").write_text(remote.to_json(), encoding="utf-8")
    monkeypatch.setattr(
        "firmatlas.app.catalog_update.fetch_manifest", lambda *args, **kwargs: remote
    )
    config = AppConfig(
        data_dir=tmp_path,
        catalog=CatalogConfig(mode="managed", manifest_url="file:///manifest.json"),
    )

    report = update_catalog(data_dir=tmp_path, config=config)

    assert report.status == "up_to_date"
    assert report.migrated_downloads == 0


def test_check_rejects_standalone(tmp_path):
    config = AppConfig(data_dir=tmp_path)

    try:
        check_catalog_update(data_dir=tmp_path, config=config)
    except CatalogUpdateError as exc:
        assert "Standalone" in str(exc)
    else:
        raise AssertionError("expected CatalogUpdateError")


def test_validate_candidate_database_accepts_matching_empty_database(tmp_path):
    data_dir = tmp_path / "data"
    initialize(data_dir)
    manifest = make_manifest()
    manifest = replace(manifest, counts=CatalogCounts(0, 0, 0, 0, 0))

    stats = validate_candidate_database(database_path=data_dir / "firmatlas.db", manifest=manifest)

    assert stats.sources == 0


def test_validate_candidate_database_rejects_count_mismatch(tmp_path):
    data_dir = tmp_path / "data"
    initialize(data_dir)

    with pytest.raises(CatalogUpdateError, match="计数"):
        validate_candidate_database(
            database_path=data_dir / "firmatlas.db", manifest=make_manifest()
        )


def test_update_migrates_download_history_and_keeps_firmware(
    tmp_path, make_source, make_product_candidate
):
    server_data = tmp_path / "server"
    client_data = tmp_path / "client"
    release_dir = tmp_path / "release"
    _seed_catalog(server_data, make_source, make_product_candidate)
    server_report = export_catalog(
        data_dir=server_data,
        output_dir=release_dir,
        lineage_id="11111111-1111-4111-8111-111111111111",
        catalog_version="2026.08.07.1",
    )

    initialize(client_data)
    shutil.copy2(server_data / "firmatlas.db", client_data / "firmatlas.db")
    local_manifest = CatalogManifest.from_json(server_report.manifest_path.read_text())
    local_manifest = replace(local_manifest, catalog_version="2026.08.07.0")
    (client_data / "catalog-manifest.json").write_text(local_manifest.to_json(), encoding="utf-8")
    firmware_path = client_data / "firmware" / "keep.bin"
    firmware_path.write_bytes(b"already downloaded")
    with sqlite3.connect(client_data / "firmatlas.db") as connection:
        artifact_id = connection.execute("SELECT id FROM firmware_artifacts").fetchone()[0]
        rows = [
            ("failed-1", "failed", None, None, None),
            ("interrupted-1", "interrupted", None, None, None),
            ("completed-1", "completed", "firmware/keep.bin", 17, "a" * 64),
        ]
        for record_id, status, final_path, size, sha256 in rows:
            connection.execute(
                """
                INSERT INTO download_records
                (id, artifact_id, status, verification_status, requested_at,
                 final_relative_path, size_bytes, sha256)
                VALUES (?, ?, ?, 'not_available', '2026-08-07T00:00:00Z', ?, ?, ?)
                """,
                (record_id, artifact_id, status, final_path, size, sha256),
            )
        connection.commit()

    config = AppConfig(
        data_dir=client_data,
        catalog=CatalogConfig(
            mode="managed", manifest_url=(release_dir / "manifest.json").as_uri()
        ),
    )
    report = update_catalog(data_dir=client_data, config=config)

    assert report.status == "updated"
    assert report.migrated_downloads == 3
    assert firmware_path.read_bytes() == b"already downloaded"
    installed = CatalogManifest.from_json((client_data / "catalog-manifest.json").read_text())
    assert installed.catalog_version == "2026.08.07.1"
    with sqlite3.connect(client_data / "firmatlas.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM download_records").fetchone()[0] == 3
        assert {row[0] for row in connection.execute("SELECT status FROM download_records")} == {
            "failed",
            "interrupted",
            "completed",
        }


def test_update_rejects_lineage_mismatch_without_touching_database(
    tmp_path, make_source, make_product_candidate
):
    server_data = tmp_path / "server"
    client_data = tmp_path / "client"
    release_dir = tmp_path / "release"
    _seed_catalog(server_data, make_source, make_product_candidate)
    server_report = export_catalog(
        data_dir=server_data,
        output_dir=release_dir,
        lineage_id="11111111-1111-4111-8111-111111111111",
        catalog_version="2026.08.07.1",
    )
    initialize(client_data)
    shutil.copy2(server_data / "firmatlas.db", client_data / "firmatlas.db")
    local = replace(
        CatalogManifest.from_json(server_report.manifest_path.read_text()),
        lineage_id="22222222-2222-4222-8222-222222222222",
        catalog_version="2026.08.07.0",
    )
    (client_data / "catalog-manifest.json").write_text(local.to_json(), encoding="utf-8")
    before = sha256((client_data / "firmatlas.db").read_bytes()).hexdigest()
    config = AppConfig(
        data_dir=client_data,
        catalog=CatalogConfig(
            mode="managed", manifest_url=(release_dir / "manifest.json").as_uri()
        ),
    )

    with pytest.raises(CatalogUpdateError, match="lineage"):
        update_catalog(data_dir=client_data, config=config)

    assert sha256((client_data / "firmatlas.db").read_bytes()).hexdigest() == before


def test_replace_skips_download_migration_and_keeps_firmware(
    tmp_path, make_source, make_product_candidate
):
    server_data = tmp_path / "server"
    client_data = tmp_path / "client"
    release_dir = tmp_path / "release"
    _seed_catalog(server_data, make_source, make_product_candidate)
    server_report = export_catalog(
        data_dir=server_data,
        output_dir=release_dir,
        lineage_id="11111111-1111-4111-8111-111111111111",
        catalog_version="2026.08.07.1",
    )
    initialize(client_data)
    shutil.copy2(server_data / "firmatlas.db", client_data / "firmatlas.db")
    local = replace(
        CatalogManifest.from_json(server_report.manifest_path.read_text()),
        lineage_id="22222222-2222-4222-8222-222222222222",
        catalog_version="2026.08.07.0",
    )
    (client_data / "catalog-manifest.json").write_text(local.to_json(), encoding="utf-8")
    firmware_path = client_data / "firmware" / "orphan.bin"
    firmware_path.write_bytes(b"keep")
    with sqlite3.connect(client_data / "firmatlas.db") as connection:
        artifact_id = connection.execute("SELECT id FROM firmware_artifacts").fetchone()[0]
        connection.execute(
            """
            INSERT INTO download_records
            (id, artifact_id, status, verification_status, requested_at)
            VALUES ('old-1', ?, 'failed', 'not_checked', '2026-08-07T00:00:00Z')
            """,
            (artifact_id,),
        )
        connection.commit()
    config = AppConfig(
        data_dir=client_data,
        catalog=CatalogConfig(
            mode="managed", manifest_url=(release_dir / "manifest.json").as_uri()
        ),
    )

    report = update_catalog(data_dir=client_data, config=config, replace=True)

    assert report.status == "replaced"
    assert report.migrated_downloads == 0
    assert any("未迁移" in warning for warning in report.warnings)
    assert firmware_path.read_bytes() == b"keep"
    with sqlite3.connect(client_data / "firmatlas.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM download_records").fetchone()[0] == 0


def test_update_aborts_when_old_artifact_is_missing(tmp_path, make_source, make_product_candidate):
    server_data = tmp_path / "server"
    client_data = tmp_path / "client"
    release_dir = tmp_path / "release"
    initialize(server_data)
    export_catalog(
        data_dir=server_data,
        output_dir=release_dir,
        lineage_id="11111111-1111-4111-8111-111111111111",
        catalog_version="2026.08.07.1",
    )
    _seed_catalog(client_data, make_source, make_product_candidate)
    remote_manifest = CatalogManifest.from_json((release_dir / "manifest.json").read_text())
    local = replace(remote_manifest, catalog_version="2026.08.07.0")
    (client_data / "catalog-manifest.json").write_text(local.to_json(), encoding="utf-8")
    with sqlite3.connect(client_data / "firmatlas.db") as connection:
        artifact_id = connection.execute("SELECT id FROM firmware_artifacts").fetchone()[0]
        connection.execute(
            """
            INSERT INTO download_records
            (id, artifact_id, status, verification_status, requested_at)
            VALUES ('old-1', ?, 'failed', 'not_checked', '2026-08-07T00:00:00Z')
            """,
            (artifact_id,),
        )
        connection.commit()
    database_path = client_data / "firmatlas.db"
    before = sha256(database_path.read_bytes()).hexdigest()
    config = AppConfig(
        data_dir=client_data,
        catalog=CatalogConfig(
            mode="managed", manifest_url=(release_dir / "manifest.json").as_uri()
        ),
    )

    with pytest.raises(CatalogUpdateError, match="Artifact"):
        update_catalog(data_dir=client_data, config=config)

    assert sha256(database_path.read_bytes()).hexdigest() == before


def test_update_rolls_back_when_manifest_replace_fails(
    tmp_path, monkeypatch, make_source, make_product_candidate
):
    server_data = tmp_path / "server"
    client_data = tmp_path / "client"
    release_dir = tmp_path / "release"
    _seed_catalog(server_data, make_source, make_product_candidate)
    server_report = export_catalog(
        data_dir=server_data,
        output_dir=release_dir,
        lineage_id="11111111-1111-4111-8111-111111111111",
        catalog_version="2026.08.07.1",
    )
    initialize(client_data)
    shutil.copy2(server_data / "firmatlas.db", client_data / "firmatlas.db")
    local = replace(
        CatalogManifest.from_json(server_report.manifest_path.read_text()),
        catalog_version="2026.08.07.0",
    )
    manifest_path = client_data / "catalog-manifest.json"
    manifest_path.write_text(local.to_json(), encoding="utf-8")
    database_path = client_data / "firmatlas.db"
    before_database = sha256(database_path.read_bytes()).hexdigest()
    before_manifest = manifest_path.read_bytes()
    original_write = catalog_update_module._atomic_write_manifest
    calls = 0

    def fail_once(path, content):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("模拟 manifest 写入失败")
        return original_write(path, content)

    monkeypatch.setattr(catalog_update_module, "_atomic_write_manifest", fail_once)
    config = AppConfig(
        data_dir=client_data,
        catalog=CatalogConfig(
            mode="managed", manifest_url=(release_dir / "manifest.json").as_uri()
        ),
    )

    with pytest.raises(CatalogUpdateError, match="原子替换失败"):
        update_catalog(data_dir=client_data, config=config)

    assert sha256(database_path.read_bytes()).hexdigest() == before_database
    assert manifest_path.read_bytes() == before_manifest
