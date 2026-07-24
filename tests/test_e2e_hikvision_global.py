"""hikvision-global 端到端测试：真实适配器经 crawl 写入 SQLite，再由 CLI 查询。"""

import json
from pathlib import Path

from click.testing import CliRunner

from firmatlas.adapters.hikvision_global.adapter import HikvisionGlobalAdapter
from firmatlas.app import registry
from firmatlas.cli.main import cli
from firmatlas.infra.http_client import FetchedText

FIXTURE = Path(__file__).parent / "fixtures" / "hikvision-global" / "firmware_camera_samples.html"
INDEX_URL = "https://www.hikvision.com/en/support/download/firmware/"


class FixtureHttpFetcher:
    async def get_text(self, url: str, *, headers=None) -> FetchedText:
        assert url == INDEX_URL
        return FetchedText(
            url=url,
            status_code=200,
            text=FIXTURE.read_text(encoding="utf-8"),
        )


def _disable_proxies(monkeypatch) -> None:
    for variable in (
        "all_proxy",
        "ALL_PROXY",
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
    ):
        monkeypatch.delenv(variable, raising=False)


def test_hikvision_global_cli_full_flow_and_recrawl(tmp_path, monkeypatch) -> None:
    def build_adapter(source_key, http, data_dir=None):
        assert source_key == "hikvision-global"
        return HikvisionGlobalAdapter(FixtureHttpFetcher())

    monkeypatch.setattr(registry, "build_adapter", build_adapter)
    _disable_proxies(monkeypatch)

    runner = CliRunner()
    data_dir = str(tmp_path / "data")

    result = runner.invoke(cli, ["--data-dir", data_dir, "init"])
    assert result.exit_code == 0, result.output

    sources = runner.invoke(cli, ["--data-dir", data_dir, "sources"])
    assert sources.exit_code == 0, sources.output
    assert "hikvision-global" in sources.output
    assert "WW" in sources.output

    first = runner.invoke(
        cli,
        ["--data-dir", data_dir, "crawl", "hikvision-global"],
    )
    assert first.exit_code == 0, first.output
    assert "completed" in first.output
    assert "产品 5" in first.output
    assert "发布 6" in first.output
    assert "Artifact 7" in first.output

    listed = runner.invoke(
        cli,
        [
            "--data-dir",
            data_dir,
            "list",
            "--source",
            "hikvision-global",
            "--format",
            "json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["total"] == 6
    assert all(row["source_key"] == "hikvision-global" for row in payload["rows"])
    assert all(row["product_type"] == "camera" for row in payload["rows"])

    release_id = payload["rows"][0]["release_id"]
    shown = runner.invoke(cli, ["--data-dir", data_dir, "show", release_id])
    assert shown.exit_code == 0, shown.output
    assert "hikvision-global" in shown.output
    assert "https://assets.hikvision.com/" in shown.output

    second = runner.invoke(
        cli,
        ["--data-dir", data_dir, "crawl", "hikvision-global"],
    )
    assert second.exit_code == 0, second.output
    assert "新增 0" in second.output
    assert "消失 0" in second.output


def test_hikvision_global_registry_contract() -> None:
    assert "hikvision-global" in registry.supported_source_keys()
    source = next(
        source for source in registry.seed_sources() if source.source_key == "hikvision-global"
    )

    assert source.vendor_key == "hikvision"
    assert source.region_code == "WW"
    assert source.locale == "en"
    assert source.adapter_key == "hikvision_global"
