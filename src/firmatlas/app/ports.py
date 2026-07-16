"""业务层依赖的 Repository 接口（接口设计 §6）。

这些 Protocol 是业务用例与基础设施层之间的边界：
- 用例代码只允许通过这里的签名访问数据库，看不到 SQLAlchemy 的任何类型；
- infra/repository.py 提供 SQLite 实现，测试可以用内存实现替换（AC-19）；
- 事务边界由 UnitOfWorkFactory.begin() 的 with 块表达：
  正常退出提交，抛异常回滚。

CatalogQueryService（list/show 的跨表查询）属于阶段 3 的 CLI 工作，届时再补充。
"""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from firmatlas.domain.candidates import (
    FirmwareArtifactCandidate,
    FirmwareReleaseCandidate,
    HardwareRevisionCandidate,
    ProductCandidate,
)
from firmatlas.domain.model import (
    AdapterIssue,
    ArtifactContext,
    CrawlRun,
    CrawlRunStatus,
    CrawlStats,
    DisappearanceSummary,
    DownloadPatch,
    DownloadRecord,
    DownloadStatus,
    FirmwareSource,
    UpsertResult,
)


class SourceRepository(Protocol):
    def list_sources(self) -> list[FirmwareSource]: ...

    def get_by_source_key(self, source_key: str) -> FirmwareSource | None: ...

    def ensure_seed_sources(self, seeds: Sequence[FirmwareSource]) -> None:
        """幂等写入内置来源（firmatlas init 用）：source_key 已存在则跳过。"""
        ...


class CatalogRepository(Protocol):
    """目录写入全部为幂等 upsert：以 (父ID, candidate.source_key) 匹配既有行；
    命中则更新非身份字段和 last_seen_at/last_seen_run_id、保留 first_seen_at，
    未命中则新增（AC-13、AC-14）。
    """

    def upsert_product(
        self, *, source_id: str, candidate: ProductCandidate, run_id: str, seen_at: datetime
    ) -> UpsertResult: ...

    def upsert_hardware_revision(
        self,
        *,
        product_id: str,
        candidate: HardwareRevisionCandidate,
        run_id: str,
        seen_at: datetime,
    ) -> UpsertResult: ...

    def upsert_release(
        self,
        *,
        hardware_revision_id: str,
        candidate: FirmwareReleaseCandidate,
        run_id: str,
        seen_at: datetime,
    ) -> UpsertResult:
        """此前 disappeared 的发布重新出现时恢复 active 并清空 disappeared_at（AC-17）。"""
        ...

    def upsert_artifact(
        self, *, release_id: str, candidate: FirmwareArtifactCandidate, run_id: str,
        seen_at: datetime,
    ) -> UpsertResult:
        """同 upsert_release；download_url 变化时刷新 url_last_resolved_at。"""
        ...

    def mark_unseen_as_disappeared(
        self, *, source_id: str, run_id: str, confirmed_at: datetime
    ) -> DisappearanceSummary:
        """将该来源下 last_seen_run_id != run_id 的 active 发布/Artifact 置 disappeared。

        只允许在一次完整采集成功后调用（AC-15、AC-16 的判定在业务用例层）。
        """
        ...

    def update_artifact_url(
        self,
        *,
        artifact_id: str,
        download_url: str,
        url_expires_at: datetime | None,
        resolved_at: datetime,
    ) -> None:
        """地址刷新落库；绝不改动 source_key（AC-29）。"""
        ...

    def get_artifact_context(self, artifact_id: str) -> ArtifactContext | None: ...


class CrawlRunRepository(Protocol):
    def create_run(self, *, source_id: str, started_at: datetime) -> CrawlRun: ...

    def finalize_run(
        self,
        *,
        run_id: str,
        status: CrawlRunStatus,
        is_complete: bool,
        finished_at: datetime,
        stats: CrawlStats,
        error_summary: str | None,
        issues: Sequence[AdapterIssue],
    ) -> None: ...

    def list_runs(self, *, source_id: str | None = None, limit: int = 50) -> list[CrawlRun]: ...

    def find_stale_running(self) -> list[CrawlRun]:
        """启动时识别上次崩溃遗留的 running 状态运行。"""
        ...


class DownloadRepository(Protocol):
    def create_download(self, *, artifact_id: str, requested_at: datetime) -> DownloadRecord:
        """同一 Artifact 已有 queued/downloading 记录时抛 ActiveDownloadExistsError（AC-30）。"""
        ...

    def transition(self, *, download_id: str, patch: DownloadPatch) -> DownloadRecord:
        """按状态机推进下载记录；非法变迁抛 InvalidTransitionError。"""
        ...

    def list_downloads(
        self,
        *,
        status: DownloadStatus | None = None,
        artifact_id: str | None = None,
        limit: int = 50,
    ) -> list[DownloadRecord]: ...

    def find_stale_active(self) -> list[DownloadRecord]:
        """启动时识别遗留的 queued/downloading 记录。"""
        ...


class UnitOfWork(Protocol):
    sources: SourceRepository
    catalog: CatalogRepository
    runs: CrawlRunRepository
    downloads: DownloadRepository


class UnitOfWorkFactory(Protocol):
    def begin(self) -> AbstractContextManager[UnitOfWork]:
        """开启一个事务：with 块正常退出提交，抛异常回滚。"""
        ...
