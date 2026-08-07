"""catalog status CLI 测试。"""

import json

from click.testing import CliRunner

from firmatlas.cli.main import cli


def test_catalog_status_shows_uninstalled_state(tmp_path):
    data_dir = tmp_path / "data"
    config_path = tmp_path / "managed.toml"
    config_path.write_text(
        f"""
data_dir = "{data_dir}"
[catalog]
mode = "managed"
manifest_url = "file:///srv/catalog/manifest.json"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["--config", str(config_path), "catalog", "status"])

    assert result.exit_code == 0, result.output
    assert "Catalog 模式：managed" in result.output
    assert "本地 lineage：未安装" in result.output


def test_catalog_status_json_is_machine_readable(tmp_path):
    result = CliRunner().invoke(
        cli,
        ["--data-dir", str(tmp_path / "data"), "catalog", "status", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["mode"] == "standalone"
    assert payload["lineage_id"] is None
