"""tp-link-cn 端到端测试（阶段 3 退出条件）：crawl → SQLite → list/show。

除 HTTP 层用 fixture 回放外，全链路都是真实实现：
真适配器（解析/分类/建树）→ 真 crawl 用例（事务/统计/对账）
→ 真 SQLite Repository → 真查询服务 → 真 CLI 渲染。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from firmatlas.adapters.tplink_cn.adapter import TplinkCnAdapter
from firmatlas.adapters.tplink_cn.classification import candidate_product_class_ids
from firmatlas.app import registry
from firmatlas.app.crawl import crawl_source
from firmatlas.app.queries import CatalogFilter
from firmatlas.cli.main import cli
from firmatlas.domain.model import CrawlRunStatus, ProductType
from firmatlas.infra.http_client import FetchedJson
from firmatlas.infra.query_service import SqliteCatalogQueryService

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tp-link-cn"


class FixtureHttpFetcher:
    """按 (品类, 页码) 回放 fixture 的 HttpFetcher；未配置的组合返回空页。"""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses

    async def post_json(self, url: str, body: Any, *, headers=None) -> FetchedJson:
        cid = (body.get("productClassIds") or ["unknown"])[0]
        key = f"{cid}_p{body.get('pageIndex', 1)}"
        data = self._responses.get(key, {"result": {"total": 0, "collection": []}})
        return FetchedJson(url=url, status_code=200, data=data)


def fixture_responses() -> dict[str, Any]:
    """2502（企业路由器）与 2549（摄像机）返回真实脱敏 fixture，其余品类空。

    fixture 的 total 大于单页记录数，将 total 压到实际条数以免适配器翻空页。
    """
    responses: dict[str, Any] = {}
    for cid, name in (("2502", "search_2502.json"), ("2549", "search_2549.json")):
        data = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
        data["result"]["total"] = len(data["result"]["collection"])
        responses[f"{cid}_p1"] = data
    for cid in candidate_product_class_ids():
        responses.setdefault(f"{cid}_p1", {"result": {"total": 0, "collection": []}})
    return responses


def run_real_crawl(uow_factory):
    adapter = TplinkCnAdapter(FixtureHttpFetcher(fixture_responses()))
    return asyncio.run(crawl_source(adapter=adapter, uow_factory=uow_factory))


# ---------------------------------------------------------------------------
# 用例层端到端：crawl → SQLite → 查询服务
# ---------------------------------------------------------------------------


def test_e2e_crawl_to_query(engine, uow_factory, seeded_source):
    report = run_real_crawl(uow_factory)

    assert report.status is CrawlRunStatus.COMPLETED
    assert report.is_complete is True
    assert report.stats.products_seen > 0
    assert report.stats.artifacts_seen > 0
    assert report.stats.error_count == 0

    service = SqliteCatalogQueryService(engine)
    page = service.list_firmware(CatalogFilter(limit=100))
    assert page.total == report.stats.releases_seen

    # 路由器与摄像头两类都应入库
    types = {row.product_type for row in page.rows}
    assert ProductType.CAMERA in types
    assert types & {ProductType.ROUTER, ProductType.CELLULAR_CPE}

    # show 任一发布：所属链完整、Artifact 有下载地址
    detail = service.show_release(page.rows[0].release_id)
    assert detail is not None
    assert detail.source_key == "tp-link-cn"
    assert detail.artifacts
    assert all(a.download_url.startswith("http") for a in detail.artifacts)


def test_e2e_recrawl_is_idempotent(engine, uow_factory, seeded_source):
    """同一 fixture 采集两次：目录不重复、第二次全为更新（AC-13、AC-14）。"""
    first = run_real_crawl(uow_factory)
    second = run_real_crawl(uow_factory)

    assert second.stats.items_added == 0
    assert second.stats.items_updated > 0
    assert second.stats.items_disappeared == 0

    page = SqliteCatalogQueryService(engine).list_firmware(CatalogFilter(limit=100))
    assert page.total == first.stats.releases_seen  # 没有变多


# ---------------------------------------------------------------------------
# CLI 层端到端：init → crawl → list → show（真适配器 + fixture HTTP）
# ---------------------------------------------------------------------------


def test_e2e_cli_full_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(
        registry,
        "build_adapter",
        lambda key, http, data_dir=None: TplinkCnAdapter(FixtureHttpFetcher(fixture_responses())),
    )
    for var in ("all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(var, raising=False)

    runner = CliRunner()
    data = str(tmp_path / "data")

    assert runner.invoke(cli, ["--data-dir", data, "init"]).exit_code == 0

    result = runner.invoke(cli, ["--data-dir", data, "crawl", "tp-link-cn"])
    assert result.exit_code == 0, result.output
    assert "completed" in result.output

    # 组合筛选：只看摄像头
    result = runner.invoke(
        cli, ["--data-dir", data, "list", "--type", "camera", "--format", "json"]
    )
    payload = json.loads(result.output)
    assert payload["total"] > 0
    assert all(row["product_type"] == "camera" for row in payload["rows"])
    assert all("IPC" in row["model"] for row in payload["rows"])

    # show 详情
    release_id = payload["rows"][0]["release_id"]
    result = runner.invoke(cli, ["--data-dir", data, "show", release_id])
    assert result.exit_code == 0, result.output
    assert "tp-link-cn" in result.output
    assert "https://" in result.output
