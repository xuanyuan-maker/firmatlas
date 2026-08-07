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
        minimum_firmatlas_version="0.1.0",
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


def test_catalog_update_without_replace_rejects_first_install(tmp_path):
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

    result = CliRunner().invoke(cli, ["--config", str(config_path), "catalog", "update"])

    assert result.exit_code != 0
    assert "首次安装必须使用 --replace" in result.output


def test_catalog_update_json_failure_is_machine_readable(tmp_path):
    config_path = tmp_path / "managed.toml"
    config_path.write_text(
        f"""
data_dir = "{tmp_path / "data"}"
[catalog]
mode = "managed"
manifest_url = "{(tmp_path / "missing-manifest.json").as_uri()}"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        ["--config", str(config_path), "catalog", "update", "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["error_code"] == "catalog_source_error"
    assert result.stderr == ""


def test_catalog_update_check_replace_json_rejects_invalid_arguments(tmp_path):
    config_path = tmp_path / "managed.toml"
    config_path.write_text(
        f"""
data_dir = "{tmp_path / "data"}"
[catalog]
mode = "managed"
manifest_url = "{(tmp_path / "manifest.json").as_uri()}"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--config",
            str(config_path),
            "catalog",
            "update",
            "--check",
            "--replace",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error_code"] == "invalid_arguments"
    assert result.stderr == ""


def test_catalog_update_replace_json(tmp_path):
    runner = CliRunner()
    server_data = tmp_path / "server"
    client_data = tmp_path / "client"
    release_dir = tmp_path / "release"
    server_init = runner.invoke(cli, ["--data-dir", str(server_data), "init"])
    assert server_init.exit_code == 0, server_init.output
    export = runner.invoke(
        cli,
        ["--data-dir", str(server_data), "catalog", "export", "--output", str(release_dir)],
    )
    assert export.exit_code == 0, export.output
    client_init = runner.invoke(cli, ["--data-dir", str(client_data), "init"])
    assert client_init.exit_code == 0, client_init.output
    config_path = tmp_path / "managed.toml"
    config_path.write_text(
        f"""
data_dir = "{client_data}"
[catalog]
mode = "managed"
manifest_url = "{(release_dir / "manifest.json").as_uri()}"
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        cli,
        ["--config", str(config_path), "catalog", "update", "--replace", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "replaced"
    assert payload["migrated_downloads"] == 0
    assert payload["backup_path"] is not None
