"""Catalog 纯净快照导出测试。"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
from datetime import UTC, datetime

import pytest

from firmatlas.app.catalog_export import export_catalog
from firmatlas.app.catalog_manifest import CatalogManifest
from firmatlas.domain.errors import CatalogExportError
from firmatlas.domain.model import CrawlRunStatus, CrawlStats, DownloadStatus
from firmatlas.domain.timeutil import utc_now
from firmatlas.infra.database import create_engine, initialize, open_database
from firmatlas.infra.repository import SqliteUnitOfWorkFactory


def _seed_catalog(data_dir, make_source, make_product_candidate):
    initialize(data_dir)
    engine = open_database(data_dir)
    factory = SqliteUnitOfWorkFactory(engine)
    with factory.begin() as uow:
        source = make_source()
        uow.sources.ensure_seed_sources([source])
        run = uow.runs.create_run(source_id=source.id, started_at=utc_now())
        product_candidate = make_product_candidate()
        product = uow.catalog.upsert_product(
            source_id=source.id,
            candidate=product_candidate,
            run_id=run.id,
            seen_at=utc_now(),
        )
        revision_candidate = product_candidate.hardware_revisions[0]
        revision = uow.catalog.upsert_hardware_revision(
            product_id=product.entity_id,
            candidate=revision_candidate,
            run_id=run.id,
            seen_at=utc_now(),
        )
        release_candidate = revision_candidate.releases[0]
        release = uow.catalog.upsert_release(
            hardware_revision_id=revision.entity_id,
            candidate=release_candidate,
            run_id=run.id,
            seen_at=utc_now(),
        )
        uow.catalog.upsert_artifact(
            release_id=release.entity_id,
            candidate=release_candidate.artifacts[0],
            run_id=run.id,
            seen_at=utc_now(),
        )
        uow.runs.finalize_run(
            run_id=run.id,
            status=CrawlRunStatus.COMPLETED,
            is_complete=True,
            finished_at=utc_now(),
            stats=CrawlStats(products_seen=1, releases_seen=1, artifacts_seen=1),
            error_summary=None,
            issues=(),
        )
    engine.dispose()


def test_export_creates_valid_pure_snapshot(tmp_path, make_source, make_product_candidate):
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "release"
    _seed_catalog(data_dir, make_source, make_product_candidate)
    (data_dir / "firmware").mkdir(parents=True, exist_ok=True)
    (data_dir / "firmware" / "real.bin").write_bytes(b"not in catalog")
    (data_dir / "auth").mkdir()
    (data_dir / "auth" / "token").write_text("secret", encoding="utf-8")

    report = export_catalog(
        data_dir=data_dir,
        output_dir=output_dir,
        lineage_id="11111111-1111-4111-8111-111111111111",
        catalog_version="2026.08.07.1",
        created_at=datetime(2026, 8, 7, 2, 0, tzinfo=UTC),
    )

    assert report.output_dir == output_dir
    assert sorted(path.name for path in output_dir.iterdir()) == [
        "firmatlas.db.gz",
        "firmatlas.db.gz.sha256",
        "manifest.json",
    ]
    manifest = CatalogManifest.from_json((output_dir / "manifest.json").read_text())
    assert manifest.lineage_id == "11111111-1111-4111-8111-111111111111"
    assert manifest.catalog_version == "2026.08.07.1"
    assert manifest.counts.sources == 1
    assert manifest.counts.products == 1
    assert manifest.counts.releases == 1
    assert manifest.counts.artifacts == 1
    assert manifest.counts.downloads == 0
    assert manifest.sources[0].last_status == "completed"

    compressed = output_dir / "firmatlas.db.gz"
    assert (
        hashlib.sha256(compressed.read_bytes()).hexdigest() == manifest.database.compressed_sha256
    )
    with gzip.open(compressed, "rb") as handle:
        database_bytes = handle.read()
    assert hashlib.sha256(database_bytes).hexdigest() == manifest.database.database_sha256
    candidate = tmp_path / "candidate.db"
    candidate.write_bytes(database_bytes)
    with sqlite3.connect(candidate) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM download_records").fetchone()[0] == 0

    assert not (output_dir / "real.bin").exists()
    assert not (output_dir / "token").exists()


def test_export_rejects_download_records_and_leaves_no_output(
    tmp_path, make_source, make_product_candidate
):
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "release"
    _seed_catalog(data_dir, make_source, make_product_candidate)

    # 直接插入最小下载记录，避免改变导出用例本身的纯净库检查路径。
    with sqlite3.connect(data_dir / "firmatlas.db") as connection:
        artifact_id = connection.execute("SELECT id FROM firmware_artifacts").fetchone()[0]
        connection.execute(
            """
            INSERT INTO download_records
            (id, artifact_id, status, verification_status, requested_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "download-1",
                artifact_id,
                DownloadStatus.FAILED.value,
                "not_checked",
                "2026-08-07T00:00:00Z",
            ),
        )
        connection.commit()

    with pytest.raises(CatalogExportError, match="download_records"):
        export_catalog(data_dir=data_dir, output_dir=output_dir)
    assert not output_dir.exists()


def test_export_rejects_running_crawl(tmp_path, make_source, make_product_candidate):
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "release"
    _seed_catalog(data_dir, make_source, make_product_candidate)
    with sqlite3.connect(data_dir / "firmatlas.db") as connection:
        source_id = connection.execute("SELECT id FROM firmware_sources").fetchone()[0]
        connection.execute(
            """
            INSERT INTO crawl_runs (id, source_id, status, is_complete, started_at, created_at)
            VALUES (?, ?, 'running', 0, ?, ?)
            """,
            ("running-1", source_id, "2026-08-07T00:00:00Z", "2026-08-07T00:00:00Z"),
        )
        connection.commit()

    with pytest.raises(CatalogExportError, match="running"):
        export_catalog(data_dir=data_dir, output_dir=output_dir)
    assert not output_dir.exists()


def test_export_rejects_foreign_key_error(tmp_path):
    data_dir = tmp_path / "data"
    initialize(data_dir)
    database_path = data_dir / "firmatlas.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO products
            (id, source_id, source_key, display_name, model_raw, model_normalized,
             product_family, product_type, source_url, first_seen_at, last_seen_at,
             last_seen_run_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "orphan-product",
                "missing-source",
                "orphan",
                "Orphan",
                "Orphan",
                "orphan",
                "router",
                "router",
                "https://example.invalid/orphan",
                "2026-08-07T00:00:00Z",
                "2026-08-07T00:00:00Z",
                "missing-run",
                "2026-08-07T00:00:00Z",
                "2026-08-07T00:00:00Z",
            ),
        )
        connection.commit()

    with pytest.raises(CatalogExportError, match="foreign_key_check"):
        export_catalog(data_dir=data_dir, output_dir=tmp_path / "release")


def test_export_rejects_corrupt_database(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "firmatlas.db").write_bytes(b"not a sqlite database")

    with pytest.raises(CatalogExportError):
        export_catalog(data_dir=data_dir, output_dir=tmp_path / "release")


@pytest.mark.parametrize("bad_version", [0, 2])
def test_export_rejects_incompatible_schema(tmp_path, bad_version):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    database_path = data_dir / "firmatlas.db"
    engine = create_engine(database_path)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"PRAGMA user_version = {bad_version}")
        connection.commit()

    with pytest.raises(CatalogExportError, match="schema_version"):
        export_catalog(data_dir=data_dir, output_dir=tmp_path / "release")


def test_export_rejects_existing_output_directory(tmp_path):
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "release"
    output_dir.mkdir()

    with pytest.raises(CatalogExportError, match="已存在"):
        export_catalog(data_dir=data_dir, output_dir=output_dir)
