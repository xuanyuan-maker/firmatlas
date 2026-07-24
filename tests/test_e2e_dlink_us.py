"""dlink-us 端到端测试：真实适配器经 crawl 写入 SQLite，再由 CLI 查询。"""

import json
from pathlib import Path

from click.testing import CliRunner

from firmatlas.adapters.dlink_us.adapter import DlinkUsAdapter
from firmatlas.app import registry
from firmatlas.cli.main import cli
from firmatlas.infra.http_client import FetchedText

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dlink-us"
INDEX_URL = "https://support.dlink.com/resource/PRODUCTS/"


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class FixtureHttpFetcher:
    def __init__(self) -> None:
        self._routes = {
            INDEX_URL: _fixture("adapter-products-index.html"),
            "https://support.dlink.com/resource/PRODUCTS/DCS-8302LH/": _fixture(
                "product-dcs-8302lh.html"
            ),
            "https://support.dlink.com/resource/products/DCS-8302LH/REVA/": _fixture(
                "revision-dcs-8302lh-reva.html"
            ),
            "https://support.dlink.com/resource/products/DCS-8302LH/REVA/FIRMWARE/": (
                _fixture("firmware-dcs-8302lh-reva.html")
            ),
            "https://support.dlink.com/resource/PRODUCTS/DIR-X5460/": _fixture(
                "product-dir-x5460.html"
            ),
            "https://support.dlink.com/resource/products/DIR-X5460/FIRMWARE/": _fixture(
                "firmware-dir-x5460.html"
            ),
            "https://support.dlink.com/resource/PRODUCTS/DSR-250V2/": _fixture(
                "product-dsr-250v2.html"
            ),
            "https://support.dlink.com/resource/products/DSR-250V2/REVA/": _fixture(
                "revision-dsr-250v2-reva.html"
            ),
            "https://support.dlink.com/resource/products/DSR-250V2/REVB/": _fixture(
                "firmware-dsr-250v2-revb.html"
            ),
        }

    async def get_text(self, url: str, *, headers=None) -> FetchedText:
        response_url = next(
            (candidate for candidate in self._routes if candidate.casefold() == url.casefold()),
            None,
        )
        if response_url is None:
            raise AssertionError(f"unexpected URL: {url}")
        return FetchedText(url=url, status_code=200, text=self._routes[response_url])


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


def test_dlink_us_cli_full_flow_and_recrawl(tmp_path, monkeypatch) -> None:
    def build_adapter(source_key, http, data_dir=None):
        assert source_key == "dlink-us"
        return DlinkUsAdapter(FixtureHttpFetcher())

    monkeypatch.setattr(registry, "build_adapter", build_adapter)
    _disable_proxies(monkeypatch)

    runner = CliRunner()
    data_dir = str(tmp_path / "data")

    initialized = runner.invoke(cli, ["--data-dir", data_dir, "init"])
    assert initialized.exit_code == 0, initialized.output

    sources = runner.invoke(cli, ["--data-dir", data_dir, "sources"])
    assert sources.exit_code == 0, sources.output
    assert "dlink-us" in sources.output
    assert "D-Link" in sources.output
    assert "US" in sources.output

    first = runner.invoke(cli, ["--data-dir", data_dir, "crawl", "dlink-us"])
    assert first.exit_code == 0, first.output
    assert "completed" in first.output
    assert "产品 3" in first.output
    assert "发布 5" in first.output
    assert "Artifact 5" in first.output

    listed = runner.invoke(
        cli,
        [
            "--data-dir",
            data_dir,
            "list",
            "--source",
            "dlink-us",
            "--format",
            "json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["total"] == 5
    assert all(row["source_key"] == "dlink-us" for row in payload["rows"])
    assert {row["product_type"] for row in payload["rows"]} == {"camera", "router"}

    release_id = payload["rows"][0]["release_id"]
    shown = runner.invoke(cli, ["--data-dir", data_dir, "show", release_id])
    assert shown.exit_code == 0, shown.output
    assert "dlink-us" in shown.output
    assert "https://support.dlink.com/resource/products/" in shown.output.lower()

    second = runner.invoke(cli, ["--data-dir", data_dir, "crawl", "dlink-us"])
    assert second.exit_code == 0, second.output
    assert "新增 0" in second.output
    assert "消失 0" in second.output


def test_dlink_us_registry_contract() -> None:
    assert "dlink-us" in registry.supported_source_keys()
    source = next(source for source in registry.seed_sources() if source.source_key == "dlink-us")

    assert source.vendor_key == "d-link"
    assert source.vendor_name == "D-Link"
    assert source.region_code == "US"
    assert source.locale == "en-US"
    assert source.adapter_key == "dlink_us"
    assert source.base_url == "https://support.dlink.com/"
    assert registry.requires_legacy_tls("dlink-us") is True
    assert registry.requires_legacy_tls("tp-link-us") is False
