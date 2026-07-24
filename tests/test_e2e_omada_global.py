"""omada-global 端到端测试：真实适配器经 crawl 写入 SQLite，再由 CLI 查询。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from firmatlas.adapters.omada_global.adapter import OmadaGlobalAdapter
from firmatlas.app import registry
from firmatlas.cli.main import cli
from firmatlas.infra.http_client import FetchedJson

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "omada-global"
MENU_API = "https://support.omadanetworks.com/api/v1/menu/tourist/findProductMenuByTree"
MODEL_API = "https://support.omadanetworks.com/api/v1/resource/tourist/findFirmwareModelByTypeId"
FIRMWARE_API = "https://support.omadanetworks.com/api/v1/resource/tourist/findFirmwareByModel"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class FixtureHttpFetcher:
    async def post_json(self, url: str, body: Any, *, headers=None) -> FetchedJson:
        if url == MENU_API:
            data = _fixture("product-menu.json")
        elif url == MODEL_API:
            data = _fixture("model-list.json")
        elif url == FIRMWARE_API:
            model_name = body["modelName"]
            data = _fixture("firmware-samples.json")
            data["result"] = [
                item for item in data["result"] if item["title"].startswith(f"{model_name}(")
            ]
        else:
            raise AssertionError(f"unexpected URL: {url}")
        return FetchedJson(url=url, status_code=200, data=data)


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


def test_omada_global_cli_full_flow_and_recrawl(tmp_path, monkeypatch) -> None:
    def build_adapter(source_key, http, data_dir=None):
        assert source_key == "omada-global"
        return OmadaGlobalAdapter(FixtureHttpFetcher())

    monkeypatch.setattr(registry, "build_adapter", build_adapter)
    _disable_proxies(monkeypatch)

    runner = CliRunner()
    data_dir = str(tmp_path / "data")

    initialized = runner.invoke(cli, ["--data-dir", data_dir, "init"])
    assert initialized.exit_code == 0, initialized.output

    sources = runner.invoke(cli, ["--data-dir", data_dir, "sources"])
    assert sources.exit_code == 0, sources.output
    assert "omada-global" in sources.output
    assert "Omada" in sources.output
    assert "WW" in sources.output

    first = runner.invoke(cli, ["--data-dir", data_dir, "crawl", "omada-global"])
    assert first.exit_code == 0, first.output
    assert "completed" in first.output
    assert "产品 2" in first.output
    assert "发布 2" in first.output
    assert "Artifact 2" in first.output

    listed = runner.invoke(
        cli,
        [
            "--data-dir",
            data_dir,
            "list",
            "--source",
            "omada-global",
            "--format",
            "json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["total"] == 2
    assert all(row["source_key"] == "omada-global" for row in payload["rows"])
    assert {row["product_type"] for row in payload["rows"]} == {"router", "wireless_ap"}
    assert {row["hardware"] for row in payload["rows"]} == {
        "EU-V3",
        "UN-V2.20",
    }

    release_id = payload["rows"][0]["release_id"]
    shown = runner.invoke(cli, ["--data-dir", data_dir, "show", release_id])
    assert shown.exit_code == 0, shown.output
    assert "omada-global" in shown.output
    assert "https://support.omadanetworks.com/en/download/firmware/" in shown.output

    second = runner.invoke(cli, ["--data-dir", data_dir, "crawl", "omada-global"])
    assert second.exit_code == 0, second.output
    assert "新增 0" in second.output
    assert "消失 0" in second.output


def test_omada_global_registry_contract() -> None:
    assert "omada-global" in registry.supported_source_keys()
    source = next(
        source for source in registry.seed_sources() if source.source_key == "omada-global"
    )

    assert source.vendor_key == "omada"
    assert source.vendor_name == "Omada"
    assert source.region_code == "WW"
    assert source.locale == "en"
    assert source.adapter_key == "omada_global"
    assert source.base_url == "https://support.omadanetworks.com/en/"
    assert registry.requires_legacy_tls("omada-global") is False
