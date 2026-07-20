"""恢复上次进程异常退出后遗留的活动状态。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from firmatlas.app.ports import UnitOfWorkFactory
from firmatlas.domain.model import (
    AdapterIssue,
    CrawlRunStatus,
    CrawlStats,
    DownloadErrorCode,
    DownloadPatch,
    DownloadStatus,
)
from firmatlas.domain.timeutil import utc_now

_CRAWL_INTERRUPTED = "上次 FirmAtlas 进程异常终止，采集未正常收尾"
_DOWNLOAD_INTERRUPTED = "上次 FirmAtlas 进程异常终止，下载未正常收尾"


@dataclass(frozen=True)
class RecoveryReport:
    crawl_runs_recovered: int
    downloads_recovered: int


def recover_stale_operations(
    *, uow_factory: UnitOfWorkFactory, recovered_at: datetime | None = None
) -> RecoveryReport:
    """在一个事务中终止全部遗留采集和下载任务。

    调用方必须先取得数据目录进程锁，以保证查到的活动记录不属于其他仍在
    运行的 FirmAtlas 进程。临时下载路径和已接收字节保留用于诊断，不执行归档。
    """
    finished_at = recovered_at or utc_now()
    with uow_factory.begin() as uow:
        stale_runs = uow.runs.find_stale_running()
        for run in stale_runs:
            stats = CrawlStats(
                products_seen=run.products_seen,
                releases_seen=run.releases_seen,
                artifacts_seen=run.artifacts_seen,
                items_added=run.items_added,
                items_updated=run.items_updated,
                items_disappeared=run.items_disappeared,
                items_skipped=run.items_skipped,
                error_count=run.error_count + 1,
            )
            issue = AdapterIssue(code="process_interrupted", detail=_CRAWL_INTERRUPTED)
            uow.runs.finalize_run(
                run_id=run.id,
                status=CrawlRunStatus.FAILED,
                is_complete=False,
                finished_at=finished_at,
                stats=stats,
                error_summary=_CRAWL_INTERRUPTED,
                issues=(*run.issues, issue),
            )

        stale_downloads = uow.downloads.find_stale_active()
        for record in stale_downloads:
            uow.downloads.transition(
                download_id=record.id,
                patch=DownloadPatch(
                    status=DownloadStatus.INTERRUPTED,
                    finished_at=finished_at,
                    error_code=DownloadErrorCode.INTERRUPTED.value,
                    error_message=_DOWNLOAD_INTERRUPTED,
                ),
            )

    return RecoveryReport(
        crawl_runs_recovered=len(stale_runs),
        downloads_recovered=len(stale_downloads),
    )
