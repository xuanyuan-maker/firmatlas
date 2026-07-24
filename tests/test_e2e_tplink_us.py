"""tp-link-us 端到端测试（阶段 5 退出条件）：crawl → SQLite → list/show。

除 HTTP 层用 fixture 回放外，全链路都是真实实现：
真适配器（productTree 解析/分类/HTML 固件解析/建树）→ 真 crawl 用例
→ 真 SQLite Repository → 真查询服务 → 真 CLI 渲染。

同时验证 CN/US 独立运行、不跨地区合并（AC-04、AC-11）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from click.testing import CliRunner

from firmatlas.adapters.tplink_us.adapter import TplinkUsAdapter
from firmatlas.app import registry
from firmatlas.app.crawl import crawl_source
from firmatlas.app.queries import CatalogFilter
from firmatlas.cli.main import cli
from firmatlas.domain.model import CrawlRunStatus, ProductType
from firmatlas.infra.http_client import FetchedText
from firmatlas.infra.query_service import SqliteCatalogQueryService

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tp-link-us"
_BASE = "https://www.tp-link.com/us/support/download/"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class FixtureHttpFetcher:
    """按 URL 回放 US HTML fixture 的 HttpFetcher。

    Omada 型号 URL 抛异常（模拟站外重定向失败）；未配置 URL 返回空页。
    """

    def __init__(self) -> None:
        self._routes: dict[str, str] = {
            _BASE: _load("index.html"),
            f"{_BASE}archer-be670/": _load("download_archer-be670.html"),
            f"{_BASE}deco-x55/": _load("download_deco-x55.html"),
            f"{_BASE}deco-x55/v3/": _load("download_deco-x55_v3.html"),
            f"{_BASE}deco-x55/v2/": "<html></html>",
            f"{_BASE}deco-x55/v1/": "<html></html>",
            f"{_BASE}tapo-c100/": _load("download_tapo-c100.html"),
            f"{_BASE}ac500/": "<html></html>",
            f"{_BASE}tl-mr3220/": "<html></html>",
        }
        self._raise = {f"{_BASE}omada-8mp-bullet/"}

    async def get_text(self, url: str, *, headers=None) -> FetchedText:
        if url in self._raise:
            raise ConnectionError(f"simulated offsite redirect for {url}")
        return FetchedText(url=url, status_code=200, text=self._routes.get(url, "<html></html>"))


def run_real_crawl(uow_factory):
    adapter = TplinkUsAdapter(FixtureHttpFetcher())
    return asyncio.run(crawl_source(adapter=adapter, uow_factory=uow_factory))


# ---------------------------------------------------------------------------
# 用例层端到端
# ---------------------------------------------------------------------------


def test_e2e_us_crawl_to_query(engine, uow_factory, seeded_us_source):
    report = run_real_crawl(uow_factory)

    assert report.status is CrawlRunStatus.COMPLETED
    assert report.is_complete is True
    assert report.stats.products_seen > 0
    assert report.stats.artifacts_seen > 0
    assert report.stats.error_count == 0

    service = SqliteCatalogQueryService(engine)
    page = service.list_firmware(CatalogFilter(limit=100))
    assert page.total == report.stats.releases_seen

    # 路由器与 mesh 都应入库（Archer BE670 + Deco X55）
    types = {row.product_type for row in page.rows}
    assert ProductType.ROUTER in types
    assert ProductType.MESH_ROUTER in types

    # show 任一发布：所属链完整、Artifact 下载地址为 static 直链
    detail = service.show_release(page.rows[0].release_id)
    assert detail is not None
    assert detail.source_key == "tp-link-us"
    assert detail.artifacts
    assert all(
        a.download_url.startswith("https://static.tp-link.com/") for a in detail.artifacts
    )


def test_e2e_us_recrawl_idempotent(engine, uow_factory, seeded_us_source):
    """同一 fixture 采集两次：目录不重复、第二次全为更新（AC-13、AC-14）。"""
    first = run_real_crawl(uow_factory)
    second = run_real_crawl(uow_factory)

    assert second.stats.items_added == 0
    assert second.stats.items_updated > 0
    assert second.stats.items_disappeared == 0

    page = SqliteCatalogQueryService(engine).list_firmware(CatalogFilter(limit=100))
    assert page.total == first.stats.releases_seen


# ---------------------------------------------------------------------------
# CLI 层端到端：init → crawl → list → show
# ---------------------------------------------------------------------------


def test_e2e_us_cli_full_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(
        registry, "build_adapter", lambda key, http, data_dir=None: TplinkUsAdapter(FixtureHttpFetcher())
    )
    for var in ("all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(var, raising=False)

    runner = CliRunner()
    data = str(tmp_path / "data")

    assert runner.invoke(cli, ["--data-dir", data, "init"]).exit_code == 0

    result = runner.invoke(cli, ["--data-dir", data, "crawl", "tp-link-us"])
    assert result.exit_code == 0, result.output
    assert "completed" in result.output

    # 只看路由器
    result = runner.invoke(
        cli, ["--data-dir", data, "list", "--source", "tp-link-us", "--format", "json"]
    )
    payload = json.loads(result.output)
    assert payload["total"] > 0
    assert all(row["source_key"] == "tp-link-us" for row in payload["rows"])

    release_id = payload["rows"][0]["release_id"]
    result = runner.invoke(cli, ["--data-dir", data, "show", release_id])
    assert result.exit_code == 0, result.output
    assert "tp-link-us" in result.output
    assert "https://static.tp-link.com/" in result.output


# ---------------------------------------------------------------------------
# CN/US 独立、不跨地区合并（AC-04、AC-11）
# ---------------------------------------------------------------------------


def test_cn_us_isolated_no_cross_region(tmp_path, monkeypatch):
    """两来源各自 crawl，数据按 source_key 隔离，不跨地区合并。"""
    from firmatlas.adapters.tplink_cn.adapter import TplinkCnAdapter
    from firmatlas.adapters.tplink_cn.classification import candidate_product_class_ids
    from firmatlas.infra.http_client import FetchedJson

    cn_fixture_dir = Path(__file__).parent / "fixtures" / "tp-link-cn"

    def cn_responses() -> dict:
        responses: dict = {}
        for cid, name in (("2502", "search_2502.json"), ("2549", "search_2549.json")):
            data = json.loads((cn_fixture_dir / name).read_text(encoding="utf-8"))
            data["result"]["total"] = len(data["result"]["collection"])
            responses[f"{cid}_p1"] = data
        for cid in candidate_product_class_ids():
            responses.setdefault(f"{cid}_p1", {"result": {"total": 0, "collection": []}})
        return responses

    class CnFetcher:
        def __init__(self, responses: dict) -> None:
            self._responses = responses

        async def post_json(self, url, body, *, headers=None) -> FetchedJson:
            cid = (body.get("productClassIds") or ["unknown"])[0]
            key = f"{cid}_p{body.get('pageIndex', 1)}"
            data = self._responses.get(key, {"result": {"total": 0, "collection": []}})
            return FetchedJson(url=url, status_code=200, data=data)

    def build(key, http, data_dir=None):
        if key == "tp-link-cn":
            return TplinkCnAdapter(CnFetcher(cn_responses()))
        return TplinkUsAdapter(FixtureHttpFetcher())

    monkeypatch.setattr(registry, "build_adapter", build)
    for var in ("all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(var, raising=False)

    runner = CliRunner()
    data = str(tmp_path / "data")
    assert runner.invoke(cli, ["--data-dir", data, "init"]).exit_code == 0
    assert runner.invoke(cli, ["--data-dir", data, "crawl", "tp-link-cn"]).exit_code == 0
    assert runner.invoke(cli, ["--data-dir", data, "crawl", "tp-link-us"]).exit_code == 0

    # 每个来源的记录只带自己的 source_key
    cn = json.loads(
        runner.invoke(
            cli, ["--data-dir", data, "list", "--source", "tp-link-cn", "--format", "json"]
        ).output
    )
    us = json.loads(
        runner.invoke(
            cli, ["--data-dir", data, "list", "--source", "tp-link-us", "--format", "json"]
        ).output
    )
    assert cn["total"] > 0 and us["total"] > 0
    assert all(r["source_key"] == "tp-link-cn" for r in cn["rows"])
    assert all(r["source_key"] == "tp-link-us" for r in us["rows"])

    # 两来源合计 = 全量，且无型号被跨地区合并（各来源独立计数）
    all_rows = json.loads(
        runner.invoke(cli, ["--data-dir", data, "list", "--format", "json"]).output
    )
    assert all_rows["total"] == cn["total"] + us["total"]
