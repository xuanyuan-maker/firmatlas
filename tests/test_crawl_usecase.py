"""crawl 用例测试：事件消费、逐产品事务、完整性判定与收尾（AC-08、AC-15、AC-16）。

适配器用内存假实现（预置事件序列），Repository 用真实 SQLite 实现，
验证的是用例的编排逻辑而非适配器解析。
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa

from firmatlas.adapters.events import (
    AdapterIssueSummary,
    DiscoveredProduct,
    DiscoveryCompleted,
    SkippedCandidate,
    SkipReason,
)
from firmatlas.app.crawl import CrawlReport, UnknownSourceError, crawl_source
from firmatlas.domain.model import CrawlRunStatus, VisibilityStatus
from firmatlas.infra import schema


class FakeAdapter:
    """按预置列表逐个产出事件；events 里出现异常对象时抛出（模拟致命错误）。"""

    source_key = "tp-link-cn"

    def __init__(self, events):
        self._events = events

    async def discover(self):
        for event in self._events:
            if isinstance(event, Exception):
                raise event
            yield event


def run_crawl(adapter, uow_factory) -> CrawlReport:
    return asyncio.run(crawl_source(adapter=adapter, uow_factory=uow_factory))


def completed(**overrides) -> DiscoveryCompleted:
    fields = {"is_complete": True, "incomplete_reason": None, "issues": ()}
    fields.update(overrides)
    return DiscoveryCompleted(**fields)


# ---------------------------------------------------------------------------
# 完整采集：completed + 消失对账
# ---------------------------------------------------------------------------


def test_complete_crawl_persists_tree_and_completes(
    uow_factory, seeded_source, make_product_candidate
):
    adapter = FakeAdapter([DiscoveredProduct(product=make_product_candidate()), completed()])

    report = run_crawl(adapter, uow_factory)

    assert report.status is CrawlRunStatus.COMPLETED
    assert report.is_complete is True
    assert report.error_summary is None
    assert report.stats.products_seen == 1
    assert report.stats.releases_seen == 1
    assert report.stats.artifacts_seen == 1
    # 树上 4 个实体全部新增
    assert report.stats.items_added == 4
    assert report.stats.items_updated == 0

    # run 已落库为 completed
    with uow_factory.begin() as uow:
        runs = uow.runs.list_runs(source_id=seeded_source.id)
    assert len(runs) == 1
    assert runs[0].status is CrawlRunStatus.COMPLETED
    assert runs[0].is_complete is True
    assert runs[0].finished_at is not None


def test_recrawl_is_idempotent_and_counts_updates(
    uow_factory, seeded_source, make_product_candidate
):
    product = make_product_candidate()
    run_crawl(FakeAdapter([DiscoveredProduct(product=product), completed()]), uow_factory)

    report = run_crawl(
        FakeAdapter([DiscoveredProduct(product=product), completed()]), uow_factory
    )

    assert report.status is CrawlRunStatus.COMPLETED
    assert report.stats.items_added == 0
    assert report.stats.items_updated == 4
    assert report.stats.items_disappeared == 0


def test_complete_crawl_marks_unseen_as_disappeared(
    engine, uow_factory, seeded_source, make_product_candidate
):
    """第一次采集入库；第二次完整采集没看到它 → 置 disappeared（AC-15）。"""
    product = make_product_candidate()
    run_crawl(FakeAdapter([DiscoveredProduct(product=product), completed()]), uow_factory)

    report = run_crawl(FakeAdapter([completed()]), uow_factory)

    assert report.status is CrawlRunStatus.COMPLETED
    # 1 个发布 + 1 个 Artifact 消失
    assert report.stats.items_disappeared == 2

    # 直接查表验证落库状态（验证辅助，不属于业务层访问路径）
    with engine.connect() as conn:
        status = conn.execute(
            sa.select(schema.firmware_releases.c.visibility_status)
        ).scalar_one()
    assert status == VisibilityStatus.DISAPPEARED


def test_reappeared_release_restored_to_active(
    engine, uow_factory, seeded_source, make_product_candidate
):
    """消失后的固件在下一次完整采集重新出现 → 恢复 active（AC-17）。"""
    product = make_product_candidate()
    run_crawl(FakeAdapter([DiscoveredProduct(product=product), completed()]), uow_factory)
    run_crawl(FakeAdapter([completed()]), uow_factory)  # 消失

    report = run_crawl(
        FakeAdapter([DiscoveredProduct(product=product), completed()]), uow_factory
    )

    assert report.status is CrawlRunStatus.COMPLETED
    with engine.connect() as conn:
        status = conn.execute(
            sa.select(schema.firmware_releases.c.visibility_status)
        ).scalar_one()
    assert status == VisibilityStatus.ACTIVE


# ---------------------------------------------------------------------------
# 跳过记录（AC-08）
# ---------------------------------------------------------------------------


def test_skipped_candidates_recorded_in_issues(uow_factory, seeded_source):
    skip = SkippedCandidate(
        stage="artifact",
        reason_code=SkipReason.UNMAPPED_TYPE,
        detail="产品类型不在采集范围：交换机",
        source_url="https://example.com/item/123",
        raw_hint="record-123",
    )
    adapter = FakeAdapter([skip, completed()])

    report = run_crawl(adapter, uow_factory)

    assert report.status is CrawlRunStatus.COMPLETED  # 跳过不影响完整性
    assert report.stats.items_skipped == 1
    codes = [i.code for i in report.issues]
    assert "skipped_unmapped_type" in codes

    # 跳过原因随 run 落库
    with uow_factory.begin() as uow:
        run = uow.runs.list_runs(source_id=seeded_source.id)[0]
    assert run.items_skipped == 1
    assert any(i.code == "skipped_unmapped_type" for i in run.issues)


def test_adapter_issues_merged_into_run(uow_factory, seeded_source):
    adapter = FakeAdapter(
        [completed(issues=(AdapterIssueSummary(code="api_status", detail="品类 2501 返回 500"),))]
    )

    report = run_crawl(adapter, uow_factory)

    assert any(i.code == "api_status" for i in report.issues)
    with uow_factory.begin() as uow:
        run = uow.runs.list_runs(source_id=seeded_source.id)[0]
    assert any(i.code == "api_status" for i in run.issues)


# ---------------------------------------------------------------------------
# 不完整采集：partial / failed，不得触发消失对账（AC-16）
# ---------------------------------------------------------------------------


def test_incomplete_declaration_yields_partial_without_reconcile(
    uow_factory, seeded_source, make_product_candidate
):
    product = make_product_candidate()
    run_crawl(FakeAdapter([DiscoveredProduct(product=product), completed()]), uow_factory)

    # 第二次采集没看到产品，但适配器声明不完整
    report = run_crawl(
        FakeAdapter([completed(is_complete=False, incomplete_reason="品类 2501 请求失败")]),
        uow_factory,
    )

    assert report.status is CrawlRunStatus.PARTIAL
    assert report.is_complete is False
    assert report.error_summary == "品类 2501 请求失败"
    assert report.stats.items_disappeared == 0  # 未触发对账


def test_missing_completion_event_yields_partial(uow_factory, seeded_source):
    report = run_crawl(FakeAdapter([]), uow_factory)  # 流提前结束，无 DiscoveryCompleted

    assert report.status is CrawlRunStatus.PARTIAL
    assert report.is_complete is False
    assert "DiscoveryCompleted" in report.error_summary


def test_fatal_error_yields_failed(uow_factory, seeded_source, make_product_candidate):
    adapter = FakeAdapter(
        [DiscoveredProduct(product=make_product_candidate()), RuntimeError("API 入口不可达")]
    )

    report = run_crawl(adapter, uow_factory)

    assert report.status is CrawlRunStatus.FAILED
    assert "API 入口不可达" in report.error_summary
    # 致命错误前已成功入库的产品不回滚
    assert report.stats.products_seen == 1

    with uow_factory.begin() as uow:
        run = uow.runs.list_runs(source_id=seeded_source.id)[0]
    assert run.status is CrawlRunStatus.FAILED


def test_persist_failure_yields_partial_and_continues(
    uow_factory, seeded_source, make_product_candidate
):
    """某个产品入库失败：只丢该产品，其余照常，run 置 partial。"""

    class BoomCatalog:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def upsert_product(self, *, source_id, candidate, run_id, seen_at):
            if candidate.source_key == "boom":
                raise RuntimeError("模拟数据库写入失败")
            return self._inner.upsert_product(
                source_id=source_id, candidate=candidate, run_id=run_id, seen_at=seen_at
            )

    class WrappedUow:
        def __init__(self, inner):
            self._inner = inner
            self.catalog = BoomCatalog(inner.catalog)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class WrappedFactory:
        def __init__(self, inner):
            self._inner = inner

        def begin(self):
            from contextlib import contextmanager

            @contextmanager
            def _begin():
                with self._inner.begin() as uow:
                    yield WrappedUow(uow)

            return _begin()

    good = make_product_candidate()
    bad = make_product_candidate(source_key="boom", model_normalized="tl-boom")
    adapter = FakeAdapter(
        [DiscoveredProduct(product=bad), DiscoveredProduct(product=good), completed()]
    )

    report = run_crawl(adapter, WrappedFactory(uow_factory))

    assert report.status is CrawlRunStatus.PARTIAL
    assert report.is_complete is False
    assert "1 个产品子树入库失败" in report.error_summary
    assert report.stats.products_seen == 1  # 只有 good 计入
    assert report.stats.error_count == 1
    assert any(i.code == "persist_error" for i in report.issues)


# ---------------------------------------------------------------------------
# 来源未注册
# ---------------------------------------------------------------------------


def test_unknown_source_raises(uow_factory):
    with pytest.raises(UnknownSourceError):
        run_crawl(FakeAdapter([completed()]), uow_factory)
