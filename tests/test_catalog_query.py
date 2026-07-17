"""CatalogQueryService 与 CLI list/show 的测试（AC-21 ~ AC-24）。

数据准备走 crawl 用例（假适配器 + 真 SQLite），查询走被测服务，
验证组合筛选、分页、table/JSON 双格式与 schema_version。
"""

from __future__ import annotations

import asyncio
import json
import re

from click.testing import CliRunner

from firmatlas.adapters.events import DiscoveredProduct, DiscoveryCompleted
from firmatlas.app.crawl import crawl_source
from firmatlas.app.queries import CatalogFilter
from firmatlas.cli.main import cli
from firmatlas.domain.model import ProductFamily, ProductType, VisibilityStatus
from firmatlas.infra.query_service import SqliteCatalogQueryService


class FakeAdapter:
    source_key = "tp-link-cn"

    def __init__(self, events):
        self._events = events

    async def discover(self):
        for event in self._events:
            yield event


def crawl_products(uow_factory, products) -> None:
    events = [DiscoveredProduct(product=p) for p in products]
    events.append(DiscoveryCompleted(is_complete=True, incomplete_reason=None, issues=()))
    asyncio.run(crawl_source(adapter=FakeAdapter(events), uow_factory=uow_factory))


def make_two_products(make_product_candidate, make_revision_candidate, make_release_candidate):
    """一台路由器（默认 fixture）+ 一台摄像头。"""
    router = make_product_candidate()
    camera = make_product_candidate(
        source_key="ipc44aw",
        display_name="TL-IPC44AW 云台摄像机",
        model_raw="TL-IPC44AW",
        model_normalized="tl-ipc44aw",
        series="TL-IPC",
        product_family=ProductFamily.CAMERA,
        product_type=ProductType.CAMERA,
        source_category="摄像机",
        source_url="https://example.com/product/ipc44aw",
        hardware_revisions=(
            make_revision_candidate(
                source_key="v2",
                raw_revision="V2.0",
                normalized_revision="v2",
                releases=(
                    make_release_candidate(
                        source_key="release-cam",
                        version_raw="TL-IPC44AW V2 20260601",
                        version_normalized="20260601",
                    ),
                ),
            ),
        ),
    )
    return router, camera


# ---------------------------------------------------------------------------
# 查询服务
# ---------------------------------------------------------------------------


def test_list_returns_all_without_filter(
    engine, uow_factory, seeded_source,
    make_product_candidate, make_revision_candidate, make_release_candidate,
):
    crawl_products(
        uow_factory,
        make_two_products(make_product_candidate, make_revision_candidate, make_release_candidate),
    )

    page = SqliteCatalogQueryService(engine).list_firmware(CatalogFilter())

    assert page.total == 2
    assert {row.model for row in page.rows} == {"TL-WR841N", "TL-IPC44AW"}
    row = next(r for r in page.rows if r.model == "TL-WR841N")
    assert row.product_type is ProductType.HOME_ROUTER
    assert row.hardware == "v14"
    assert row.artifact_count == 1
    assert row.visibility is VisibilityStatus.ACTIVE


def test_list_combined_filters(
    engine, uow_factory, seeded_source,
    make_product_candidate, make_revision_candidate, make_release_candidate,
):
    crawl_products(
        uow_factory,
        make_two_products(make_product_candidate, make_revision_candidate, make_release_candidate),
    )
    service = SqliteCatalogQueryService(engine)

    # 类型 + 型号包含匹配（大小写不敏感）
    page = service.list_firmware(
        CatalogFilter(type=ProductType.CAMERA, model="ipc44")
    )
    assert page.total == 1
    assert page.rows[0].model == "TL-IPC44AW"

    # 组合到不存在的组合 → 空
    page = service.list_firmware(CatalogFilter(type=ProductType.CAMERA, model="wr841"))
    assert page.total == 0

    # source + region + hardware
    page = service.list_firmware(
        CatalogFilter(source="tp-link-cn", region="cn", hardware="v14")
    )
    assert page.total == 1
    assert page.rows[0].model == "TL-WR841N"

    # 版本包含匹配
    page = service.list_firmware(CatalogFilter(version="20260601"))
    assert page.total == 1


def test_list_pagination(
    engine, uow_factory, seeded_source,
    make_product_candidate, make_revision_candidate, make_release_candidate,
):
    crawl_products(
        uow_factory,
        make_two_products(make_product_candidate, make_revision_candidate, make_release_candidate),
    )
    service = SqliteCatalogQueryService(engine)

    page = service.list_firmware(CatalogFilter(limit=1, offset=0))
    assert page.total == 2
    assert len(page.rows) == 1
    first_id = page.rows[0].release_id

    page = service.list_firmware(CatalogFilter(limit=1, offset=1))
    assert len(page.rows) == 1
    assert page.rows[0].release_id != first_id


def test_like_wildcards_are_literal(
    engine, uow_factory, seeded_source, make_product_candidate
):
    crawl_products(uow_factory, [make_product_candidate()])
    service = SqliteCatalogQueryService(engine)

    # "%" 若不转义会匹配所有行
    assert service.list_firmware(CatalogFilter(model="%")).total == 0
    assert service.list_firmware(CatalogFilter(model="wr_841")).total == 0


def test_show_release_detail(
    engine, uow_factory, seeded_source, make_product_candidate
):
    crawl_products(uow_factory, [make_product_candidate()])
    service = SqliteCatalogQueryService(engine)
    release_id = service.list_firmware(CatalogFilter()).rows[0].release_id

    detail = service.show_release(release_id)

    assert detail is not None
    assert detail.model == "TL-WR841N"
    assert detail.hardware == "v14"
    assert detail.visibility is VisibilityStatus.ACTIVE
    assert len(detail.artifacts) == 1
    artifact = detail.artifacts[0]
    assert artifact.download_url.startswith("https://")
    assert artifact.last_download_status is None  # 尚未下载

    assert service.show_release("no-such-id") is None


def test_find_release_ids_by_prefix(
    engine, uow_factory, seeded_source, make_product_candidate
):
    crawl_products(uow_factory, [make_product_candidate()])
    service = SqliteCatalogQueryService(engine)
    release_id = service.list_firmware(CatalogFilter()).rows[0].release_id

    assert service.find_release_ids_by_prefix(release_id[:8]) == [release_id]
    assert service.find_release_ids_by_prefix("zzzz") == []


# ---------------------------------------------------------------------------
# CLI list / show
# ---------------------------------------------------------------------------


def seeded_cli_env(tmp_path, uow_factory_unused=None):
    return CliRunner(), str(tmp_path / "data")


def crawl_via_cli(runner, data, monkeypatch, products):
    from firmatlas.app import registry

    events = [DiscoveredProduct(product=p) for p in products]
    events.append(DiscoveryCompleted(is_complete=True, incomplete_reason=None, issues=()))
    monkeypatch.setattr(registry, "build_adapter", lambda key, http: FakeAdapter(events))
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    result = runner.invoke(cli, ["--data-dir", data, "crawl", "tp-link-cn"])
    assert result.exit_code == 0, result.output


def test_cli_list_table_and_filters(tmp_path, monkeypatch, make_product_candidate):
    runner, data = seeded_cli_env(tmp_path)
    runner.invoke(cli, ["--data-dir", data, "init"])
    crawl_via_cli(runner, data, monkeypatch, [make_product_candidate()])

    result = runner.invoke(cli, ["--data-dir", data, "list"])
    assert result.exit_code == 0, result.output
    assert "TL-WR841N" in result.output
    assert "home_router" in result.output

    result = runner.invoke(cli, ["--data-dir", data, "list", "--type", "camera"])
    assert result.exit_code == 0
    assert "没有符合条件" in result.output


def test_cli_list_json_schema_and_no_ansi(tmp_path, monkeypatch, make_product_candidate):
    runner, data = seeded_cli_env(tmp_path)
    runner.invoke(cli, ["--data-dir", data, "init"])
    crawl_via_cli(runner, data, monkeypatch, [make_product_candidate()])

    result = runner.invoke(
        cli, ["--data-dir", data, "list", "--format", "json"], color=True
    )
    assert result.exit_code == 0, result.output

    # 纯 JSON：可整体解析、含 schema_version、无 ANSI 转义（AC-23）
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["total"] == 1
    assert payload["rows"][0]["model"] == "TL-WR841N"
    assert not re.search(r"\x1b\[", result.output)


def test_cli_show_by_prefix_and_json(tmp_path, monkeypatch, make_product_candidate):
    runner, data = seeded_cli_env(tmp_path)
    runner.invoke(cli, ["--data-dir", data, "init"])
    crawl_via_cli(runner, data, monkeypatch, [make_product_candidate()])

    listed = json.loads(
        runner.invoke(cli, ["--data-dir", data, "list", "--format", "json"]).output
    )
    release_id = listed["rows"][0]["release_id"]

    # 短前缀（list 表格显示前 8 位）
    result = runner.invoke(cli, ["--data-dir", data, "show", release_id[:8]])
    assert result.exit_code == 0, result.output
    assert "TL-WR841N" in result.output
    assert "Artifact" in result.output

    result = runner.invoke(
        cli, ["--data-dir", data, "show", release_id, "--format", "json"]
    )
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["release"]["model"] == "TL-WR841N"
    assert len(payload["release"]["artifacts"]) == 1

    result = runner.invoke(cli, ["--data-dir", data, "show", "no-such-id"])
    assert result.exit_code != 0
    assert "未找到" in result.output


def test_cli_has_no_csv_format(tmp_path):
    """AC-24：不提供 CSV 输出。"""
    runner, data = seeded_cli_env(tmp_path)
    runner.invoke(cli, ["--data-dir", data, "init"])
    result = runner.invoke(cli, ["--data-dir", data, "list", "--format", "csv"])
    assert result.exit_code != 0
