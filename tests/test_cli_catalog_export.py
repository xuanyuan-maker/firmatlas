"""catalog export CLI 测试。"""

import json

from click.testing import CliRunner

from firmatlas.cli.main import cli


def test_catalog_export_creates_snapshot(tmp_path):
    runner = CliRunner()
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "catalog-release"

    init = runner.invoke(cli, ["--data-dir", str(data_dir), "init"])
    assert init.exit_code == 0, init.output

    result = runner.invoke(
        cli,
        [
            "--data-dir",
            str(data_dir),
            "catalog",
            "export",
            "--output",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Catalog 快照已导出" in result.output
    assert (output_dir / "manifest.json").exists()
    assert (output_dir / "firmatlas.db.gz").exists()


def test_catalog_export_json_is_machine_readable(tmp_path):
    runner = CliRunner()
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "catalog-release"
    init = runner.invoke(cli, ["--data-dir", str(data_dir), "init"])
    assert init.exit_code == 0, init.output

    result = runner.invoke(
        cli,
        [
            "--data-dir",
            str(data_dir),
            "catalog",
            "export",
            "--output",
            str(output_dir),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["output_dir"] == str(output_dir.resolve())
    assert payload["counts"] == {
        "sources": 12,
        "products": 0,
        "releases": 0,
        "artifacts": 0,
        "downloads": 0,
    }


def test_catalog_export_rejects_existing_output_directory(tmp_path):
    runner = CliRunner()
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "catalog-release"
    init = runner.invoke(cli, ["--data-dir", str(data_dir), "init"])
    assert init.exit_code == 0, init.output

    first = runner.invoke(
        cli,
        ["--data-dir", str(data_dir), "catalog", "export", "--output", str(output_dir)],
    )
    assert first.exit_code == 0, first.output
    second = runner.invoke(
        cli,
        ["--data-dir", str(data_dir), "catalog", "export", "--output", str(output_dir)],
    )

    assert second.exit_code != 0
    assert "已存在" in second.output
