"""catalog update --check CLI 测试。"""

import json

from click.testing import CliRunner

from firmatlas.app.catalog_manifest import CatalogCounts, CatalogDatabase, CatalogManifest
from firmatlas.cli.main import cli


def make_manifest() -> CatalogManifest:
    from datetime import UTC, datetime

    return CatalogManifest(
        format_version=1,
        lineage_id="11111111-1111-4111-8111-111111111111",
        catalog_version="2026.08.07.1",
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
        counts=CatalogCounts(sources=0, products=0, releases=0, artifacts=0, downloads=0),
        sources=(),
    )


def test_catalog_update_check_json(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "manifest.json").write_text(make_manifest().to_json(), encoding="utf-8")
    config_path = tmp_path / "managed.toml"
    config_path.write_text(
        f"""
data_dir = "{tmp_path / "data"}"
[catalog]
mode = "managed"
manifest_url = "{(remote / "manifest.json").as_uri()}"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        ["--config", str(config_path), "catalog", "update", "--check", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["update_available"] is True
    assert payload["replace_required"] is True


def test_catalog_update_without_check_is_not_yet_allowed(tmp_path):
    config_path = tmp_path / "managed.toml"
    config_path.write_text(
        f"""
data_dir = "{tmp_path / "data"}"
[catalog]
mode = "managed"
manifest_url = "file:///tmp/manifest.json"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["--config", str(config_path), "catalog", "update"])

    assert result.exit_code != 0
    assert "尚未执行" in result.output
