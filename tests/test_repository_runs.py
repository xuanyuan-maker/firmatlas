"""CrawlRunRepository 的测试。"""

from datetime import UTC, datetime

import pytest

from firmatlas.domain.errors import RepositoryError
from firmatlas.domain.model import AdapterIssue, CrawlRunStatus, CrawlStats


def _dt(hour: int) -> datetime:
    return datetime(2026, 7, 16, hour, 0, 0, tzinfo=UTC)


def test_create_run_starts_as_running(uow_factory, seeded_source):
    with uow_factory.begin() as uow:
        run = uow.runs.create_run(source_id=seeded_source.id, started_at=_dt(8))
    assert run.status is CrawlRunStatus.RUNNING
    assert run.is_complete is False
    assert run.finished_at is None
    assert run.started_at == _dt(8)
    assert run.products_seen == 0
    assert run.issues == ()


def test_finalize_run_persists_stats_and_issues(uow_factory, seeded_source):
    stats = CrawlStats(
        products_seen=3, releases_seen=5, artifacts_seen=6,
        items_added=10, items_updated=4, items_disappeared=1,
        items_skipped=2, error_count=1,
    )
    issues = [AdapterIssue(code="RATE_LIMITED", detail="命中限速，已退避重试", source_url=None)]
    with uow_factory.begin() as uow:
        run = uow.runs.create_run(source_id=seeded_source.id, started_at=_dt(8))
        uow.runs.finalize_run(
            run_id=run.id,
            status=CrawlRunStatus.COMPLETED,
            is_complete=True,
            finished_at=_dt(9),
            stats=stats,
            error_summary=None,
            issues=issues,
        )
        runs = uow.runs.list_runs()
    assert len(runs) == 1
    saved = runs[0]
    assert saved.status is CrawlRunStatus.COMPLETED
    assert saved.is_complete is True
    assert saved.finished_at == _dt(9)
    assert saved.items_added == 10
    assert saved.items_disappeared == 1
    assert saved.issues == tuple(issues)  # JSON 落库后原样读回


def test_finalize_unknown_run_raises(uow_factory):
    with pytest.raises(RepositoryError, match="不存在"):
        with uow_factory.begin() as uow:
            uow.runs.finalize_run(
                run_id="no-such-run",
                status=CrawlRunStatus.FAILED,
                is_complete=False,
                finished_at=_dt(9),
                stats=CrawlStats(),
                error_summary="boom",
                issues=[],
            )


def test_list_runs_orders_and_filters(uow_factory, seeded_source, make_source):
    other = make_source(source_key="tp-link-us", region_code="US")
    with uow_factory.begin() as uow:
        uow.sources.ensure_seed_sources([other])
        early = uow.runs.create_run(source_id=seeded_source.id, started_at=_dt(8))
        late = uow.runs.create_run(source_id=seeded_source.id, started_at=_dt(10))
        other_run = uow.runs.create_run(source_id=other.id, started_at=_dt(9))

        all_runs = uow.runs.list_runs()
        cn_runs = uow.runs.list_runs(source_id=seeded_source.id)
        latest_only = uow.runs.list_runs(limit=1)

    assert [r.id for r in all_runs] == [late.id, other_run.id, early.id]  # 开始时间倒序
    assert [r.id for r in cn_runs] == [late.id, early.id]
    assert [r.id for r in latest_only] == [late.id]


def test_find_stale_running_only_returns_running(uow_factory, seeded_source):
    with uow_factory.begin() as uow:
        stale = uow.runs.create_run(source_id=seeded_source.id, started_at=_dt(8))
        done = uow.runs.create_run(source_id=seeded_source.id, started_at=_dt(9))
        uow.runs.finalize_run(
            run_id=done.id,
            status=CrawlRunStatus.PARTIAL,
            is_complete=False,
            finished_at=_dt(10),
            stats=CrawlStats(),
            error_summary=None,
            issues=[],
        )
        remaining = uow.runs.find_stale_running()
    assert [r.id for r in remaining] == [stale.id]
