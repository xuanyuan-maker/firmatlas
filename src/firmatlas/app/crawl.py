"""crawl 用例：消费适配器事件流，经 Repository 入库（接口设计 §6.1、§7）。

数据流：
  1. 事务①：按 adapter.source_key 查来源，创建 CrawlRun（running）
  2. 逐事件消费 adapter.discover()：
     - DiscoveredProduct → 每产品一个事务做四级 upsert，失败只丢当前产品
     - SkippedCandidate  → 记入跳过统计与 issues（AC-08）
     - DiscoveryCompleted → 记录完整性声明，随后结束消费
  3. 收尾事务：完整且无错 → 消失对账 + run 置 completed（AC-15）；
     否则 run 置 partial/failed，跳过对账（AC-16）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from firmatlas.adapters.events import (
    DiscoveredProduct,
    DiscoveryCompleted,
    DiscoveryEvent,
    SkippedCandidate,
)
from firmatlas.app.ports import UnitOfWork, UnitOfWorkFactory
from firmatlas.domain.candidates import ProductCandidate
from firmatlas.domain.errors import FirmAtlasError
from firmatlas.domain.model import AdapterIssue, CrawlRunStatus, CrawlStats
from firmatlas.domain.timeutil import utc_now


class UnknownSourceError(FirmAtlasError):
    """adapter.source_key 在 sources 表中不存在（数据库未 init 或来源未注册）。"""


class SourceAdapter(Protocol):
    """crawl 用例对适配器的最小要求（接口设计 §4）。"""

    source_key: str

    def discover(self) -> AsyncIterator[DiscoveryEvent]: ...


@dataclass(frozen=True)
class CrawlReport:
    """crawl 用例的返回值，供 CLI 打印。"""

    run_id: str
    source_key: str
    status: CrawlRunStatus
    is_complete: bool
    stats: CrawlStats
    error_summary: str | None
    issues: tuple[AdapterIssue, ...]


async def crawl_source(*, adapter: SourceAdapter, uow_factory: UnitOfWorkFactory) -> CrawlReport:
    """执行一次采集。返回 CrawlReport；来源未注册时抛 UnknownSourceError。"""

    # --- 事务①：查来源 + 创建 CrawlRun ---------------------------------
    with uow_factory.begin() as uow:
        source = uow.sources.get_by_source_key(adapter.source_key)
        if source is None:
            raise UnknownSourceError(f"来源 {adapter.source_key!r} 未注册，请先执行 firmatlas init")
        run = uow.runs.create_run(source_id=source.id, started_at=utc_now())

    # --- 逐事件消费 ------------------------------------------------------
    counter = _StatsCounter()
    issues: list[AdapterIssue] = []
    completion: DiscoveryCompleted | None = None
    persist_failures = 0
    fatal_error: str | None = None

    try:
        async for event in adapter.discover():
            if isinstance(event, DiscoveredProduct):
                try:
                    with uow_factory.begin() as uow:
                        tree_counter = _StatsCounter()
                        _persist_product_tree(
                            uow,
                            source_id=source.id,
                            run_id=run.id,
                            product=event.product,
                            counter=tree_counter,
                        )
                    # 事务成功提交后才并入总计数，回滚的产品不计入
                    counter.merge(tree_counter)
                except Exception as exc:  # 单个产品失败不中断整个流
                    persist_failures += 1
                    counter.error_count += 1
                    issues.append(
                        AdapterIssue(
                            code="persist_error",
                            detail=f"产品 {event.product.source_key} 入库失败: {exc}",
                            source_url=event.product.source_url,
                        )
                    )
            elif isinstance(event, SkippedCandidate):
                counter.items_skipped += 1
                issues.append(
                    AdapterIssue(
                        code=f"skipped_{event.reason_code}",
                        detail=f"[{event.stage}] {event.detail}",
                        source_url=event.source_url,
                    )
                )
            elif isinstance(event, DiscoveryCompleted):
                completion = event
                break  # 契约：DiscoveryCompleted 是最后一个事件
    except Exception as exc:  # 来源级致命错误（如 API 入口不可达）
        fatal_error = f"{type(exc).__name__}: {exc}"
        counter.error_count += 1

    if completion is not None:
        issues.extend(
            AdapterIssue(code=i.code, detail=i.detail, source_url=i.source_url)
            for i in completion.issues
        )

    # --- 收尾事务 --------------------------------------------------------
    # 消失对账的充要条件（AC-15/16）：适配器声明完整 且 无致命错误 且 所有子树入库成功
    reconcile = completion is not None and completion.is_complete and fatal_error is None and (
        persist_failures == 0
    )

    if reconcile:
        status = CrawlRunStatus.COMPLETED
        error_summary = None
    elif fatal_error is not None:
        status = CrawlRunStatus.FAILED
        error_summary = fatal_error
    else:
        status = CrawlRunStatus.PARTIAL
        if completion is None:
            error_summary = "适配器未产出 DiscoveryCompleted，采集视为不完整"
        elif not completion.is_complete:
            error_summary = completion.incomplete_reason or "适配器声明采集不完整"
        else:
            error_summary = f"{persist_failures} 个产品子树入库失败"

    finished_at = utc_now()
    with uow_factory.begin() as uow:
        if reconcile:
            summary = uow.catalog.mark_unseen_as_disappeared(
                source_id=source.id, run_id=run.id, confirmed_at=finished_at
            )
            counter.items_disappeared = (
                summary.releases_disappeared + summary.artifacts_disappeared
            )
        stats = counter.to_stats()
        uow.runs.finalize_run(
            run_id=run.id,
            status=status,
            is_complete=reconcile,
            finished_at=finished_at,
            stats=stats,
            error_summary=error_summary,
            issues=issues,
        )

    return CrawlReport(
        run_id=run.id,
        source_key=adapter.source_key,
        status=status,
        is_complete=reconcile,
        stats=stats,
        error_summary=error_summary,
        issues=tuple(issues),
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


@dataclass
class _StatsCounter:
    """可变的统计累加器，收尾时转为 frozen 的 CrawlStats。"""

    products_seen: int = 0
    releases_seen: int = 0
    artifacts_seen: int = 0
    items_added: int = 0
    items_updated: int = 0
    items_disappeared: int = 0
    items_skipped: int = 0
    error_count: int = 0

    def count_upsert(self, created: bool) -> None:
        if created:
            self.items_added += 1
        else:
            self.items_updated += 1

    def merge(self, other: _StatsCounter) -> None:
        """把单产品事务的计数并入总计数（事务提交成功后调用）。"""
        self.products_seen += other.products_seen
        self.releases_seen += other.releases_seen
        self.artifacts_seen += other.artifacts_seen
        self.items_added += other.items_added
        self.items_updated += other.items_updated

    def to_stats(self) -> CrawlStats:
        return CrawlStats(
            products_seen=self.products_seen,
            releases_seen=self.releases_seen,
            artifacts_seen=self.artifacts_seen,
            items_added=self.items_added,
            items_updated=self.items_updated,
            items_disappeared=self.items_disappeared,
            items_skipped=self.items_skipped,
            error_count=self.error_count,
        )


def _persist_product_tree(
    uow: UnitOfWork,
    *,
    source_id: str,
    run_id: str,
    product: ProductCandidate,
    counter: _StatsCounter,
) -> None:
    """四级幂等 upsert 一棵产品子树（在调用方的事务内执行），同时累加计数。"""
    seen_at = utc_now()
    p = uow.catalog.upsert_product(
        source_id=source_id, candidate=product, run_id=run_id, seen_at=seen_at
    )
    counter.products_seen += 1
    counter.count_upsert(p.created)
    for hw in product.hardware_revisions:
        h = uow.catalog.upsert_hardware_revision(
            product_id=p.entity_id, candidate=hw, run_id=run_id, seen_at=seen_at
        )
        counter.count_upsert(h.created)
        for release in hw.releases:
            r = uow.catalog.upsert_release(
                hardware_revision_id=h.entity_id, candidate=release, run_id=run_id, seen_at=seen_at
            )
            counter.releases_seen += 1
            counter.count_upsert(r.created)
            for artifact in release.artifacts:
                a = uow.catalog.upsert_artifact(
                    release_id=r.entity_id, candidate=artifact, run_id=run_id, seen_at=seen_at
                )
                counter.artifacts_seen += 1
                counter.count_upsert(a.created)
