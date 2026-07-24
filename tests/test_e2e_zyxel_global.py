"""zyxel-global 端到端测试：适配器经 crawl 写入 SQLite，再由 CLI 查询。"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from click.testing import CliRunner

from firmatlas.adapters.zyxel_global.adapter import ZyxelGlobalAdapter
from firmatlas.app import registry
from firmatlas.cli.main import cli
from firmatlas.infra.http_client import FetchedText

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "zyxel-global"
AUTOCOMPLETE_PATH = "/global/en/search_api_autocomplete/product_list_by_model"
DOWNLOAD_PATH = "/global/en/support/download"


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class FixtureHttpFetcher:
    async def get_text(self, url: str, *, headers=None) -> FetchedText:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.path == AUTOCOMPLETE_PATH:
            prefix = query["q"][0]
            entries = json.loads(_fixture("autocomplete-targets.json"))
            text = json.dumps([item for item in entries if item["value"].startswith(prefix)])
        elif parsed.path == DOWNLOAD_PATH:
            model = query["model"][0]
            text = {
                "usg-flex-100h": _fixture("download-usg-flex-100h.html"),
                "nwa50ax": _fixture("download-nwa50ax.html"),
            }[model]
        else:
            raise AssertionError(f"unexpected URL: {url}")
        return FetchedText(url=url, status_code=200, text=text)


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


def test_zyxel_global_cli_full_flow_and_recrawl(tmp_path, monkeypatch) -> None:
    def build_adapter(source_key, http, data_dir=None):
        assert source_key == "zyxel-global"
        return ZyxelGlobalAdapter(FixtureHttpFetcher())

    monkeypatch.setattr(registry, "build_adapter", build_adapter)
    _disable_proxies(monkeypatch)

    runner = CliRunner()
    data_dir = str(tmp_path / "data")

    initialized = runner.invoke(cli, ["--data-dir", data_dir, "init"])
    assert initialized.exit_code == 0, initialized.output

    sources = runner.invoke(cli, ["--data-dir", data_dir, "sources"])
    assert sources.exit_code == 0, sources.output
    assert "zyxel-global" in sources.output
    assert "Zyxel" in sources.output
    assert "WW" in sources.output

    first = runner.invoke(cli, ["--data-dir", data_dir, "crawl", "zyxel-global"])
    assert first.exit_code == 0, first.output
    assert "completed" in first.output
    assert "产品 1" in first.output
    assert "发布 1" in first.output
    assert "Artifact 1" in first.output

    listed = runner.invoke(
        cli,
        [
            "--data-dir",
            data_dir,
            "list",
            "--source",
            "zyxel-global",
            "--format",
            "json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["total"] == 1
    assert all(row["source_key"] == "zyxel-global" for row in payload["rows"])
    assert {row["product_type"] for row in payload["rows"]} == {"wireless_ap"}
    assert {row["hardware"] for row in payload["rows"]} == {"unspecified"}

    release_id = payload["rows"][0]["release_id"]
    shown = runner.invoke(cli, ["--data-dir", data_dir, "show", release_id])
    assert shown.exit_code == 0, shown.output
    assert "zyxel-global" in shown.output
    assert "https://www.zyxel.com/global/en/support/download" in shown.output

    second = runner.invoke(cli, ["--data-dir", data_dir, "crawl", "zyxel-global"])
    assert second.exit_code == 0, second.output
    assert "新增 0" in second.output
    assert "消失 0" in second.output


def test_zyxel_global_registry_contract() -> None:
    assert "zyxel-global" in registry.supported_source_keys()
    source = next(
        source for source in registry.seed_sources() if source.source_key == "zyxel-global"
    )

    assert source.vendor_key == "zyxel"
    assert source.vendor_name == "Zyxel"
    assert source.region_code == "WW"
    assert source.locale == "en"
    assert source.adapter_key == "zyxel_global"
    assert source.base_url == "https://www.zyxel.com/global/en/"
