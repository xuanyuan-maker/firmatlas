"""Repository 与 UnitOfWork 的 SQLite 实现（接口设计 §6）。

约定：
- 只有本模块（和 database.py、schema.py）允许接触 SQLAlchemy；
  对外输入输出一律是 domain 包里的 dataclass 与枚举（AC-19）；
- 所有 SQLAlchemy/SQLite 异常在离开本模块前包装成 RepositoryError，
  原始异常挂在 __cause__ 上供调试，错误消息不携带 SQL 字符串；
- 时间在数据库中是 RFC 3339 文本，读写时经 timeutil 与 datetime 互转。
"""

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime

import sqlalchemy as sa

from firmatlas.domain.candidates import (
    FirmwareArtifactCandidate,
    FirmwareReleaseCandidate,
    HardwareRevisionCandidate,
    ProductCandidate,
)
from firmatlas.domain.errors import FirmAtlasError, RepositoryError
from firmatlas.domain.ids import new_id
from firmatlas.domain.model import (
    AdapterIssue,
    CrawlRun,
    CrawlRunStatus,
    CrawlStats,
    DiscoveryMethod,
    FirmwareSource,
    UpsertResult,
    VisibilityStatus,
)
from firmatlas.domain.timeutil import format_rfc3339, parse_rfc3339
from firmatlas.infra import schema


@contextmanager
def _wrap_errors(operation: str) -> Iterator[None]:
    """把底层数据库异常统一转换成 RepositoryError；自家异常原样放行。"""
    try:
        yield
    except FirmAtlasError:
        raise
    except sa.exc.SQLAlchemyError as exc:
        raise RepositoryError(f"{operation}失败（{type(exc).__name__}）") from exc


def _opt_parse(text: str | None) -> datetime | None:
    return parse_rfc3339(text) if text is not None else None


def _opt_format(value: datetime | None) -> str | None:
    return format_rfc3339(value) if value is not None else None


def _source_from_row(row: sa.Row) -> FirmwareSource:
    return FirmwareSource(
        id=row.id,
        vendor_key=row.vendor_key,
        vendor_name=row.vendor_name,
        source_key=row.source_key,
        name=row.name,
        region_code=row.region_code,
        locale=row.locale,
        base_url=row.base_url,
        adapter_key=row.adapter_key,
        discovery_method=DiscoveryMethod(row.discovery_method),
        enabled=bool(row.enabled),
        created_at=parse_rfc3339(row.created_at),
        updated_at=parse_rfc3339(row.updated_at),
    )


class SqliteSourceRepository:
    def __init__(self, conn: sa.Connection) -> None:
        self._conn = conn

    def list_sources(self) -> list[FirmwareSource]:
        t = schema.firmware_sources
        with _wrap_errors("查询来源列表"):
            rows = self._conn.execute(sa.select(t).order_by(t.c.source_key)).all()
        return [_source_from_row(row) for row in rows]

    def get_by_source_key(self, source_key: str) -> FirmwareSource | None:
        t = schema.firmware_sources
        with _wrap_errors("查询来源"):
            row = self._conn.execute(sa.select(t).where(t.c.source_key == source_key)).first()
        return _source_from_row(row) if row is not None else None

    def ensure_seed_sources(self, seeds: Sequence[FirmwareSource]) -> None:
        t = schema.firmware_sources
        with _wrap_errors("写入内置来源"):
            for seed in seeds:
                exists = self._conn.execute(
                    sa.select(t.c.id).where(t.c.source_key == seed.source_key)
                ).first()
                if exists is not None:
                    continue
                self._conn.execute(
                    t.insert().values(
                        id=seed.id,
                        vendor_key=seed.vendor_key,
                        vendor_name=seed.vendor_name,
                        source_key=seed.source_key,
                        name=seed.name,
                        region_code=seed.region_code,
                        locale=seed.locale,
                        base_url=seed.base_url,
                        adapter_key=seed.adapter_key,
                        discovery_method=seed.discovery_method.value,
                        enabled=int(seed.enabled),
                        created_at=format_rfc3339(seed.created_at),
                        updated_at=format_rfc3339(seed.updated_at),
                    )
                )


class SqliteCatalogRepository:
    """目录写入。幂等键为 (父ID, source_key)：命中则更新非身份字段并推进
    last_seen_at/last_seen_run_id、保留 first_seen_at；未命中则新增（AC-13、AC-14）。
    """

    def __init__(self, conn: sa.Connection) -> None:
        self._conn = conn

    def upsert_product(
        self, *, source_id: str, candidate: ProductCandidate, run_id: str, seen_at: datetime
    ) -> UpsertResult:
        t = schema.products
        seen_text = format_rfc3339(seen_at)
        with _wrap_errors("保存产品"):
            existing = self._conn.execute(
                sa.select(t.c.id).where(
                    t.c.source_id == source_id, t.c.source_key == candidate.source_key
                )
            ).first()
            if existing is None:
                product_id = new_id()
                self._conn.execute(
                    t.insert().values(
                        id=product_id,
                        source_id=source_id,
                        source_key=candidate.source_key,
                        display_name=candidate.display_name,
                        model_raw=candidate.model_raw,
                        model_normalized=candidate.model_normalized,
                        series=candidate.series,
                        product_family=candidate.product_family.value,
                        product_type=candidate.product_type.value,
                        source_category=candidate.source_category,
                        source_url=candidate.source_url,
                        first_seen_at=seen_text,
                        last_seen_at=seen_text,
                        last_seen_run_id=run_id,
                        created_at=seen_text,
                        updated_at=seen_text,
                    )
                )
                return UpsertResult(entity_id=product_id, created=True)
            self._conn.execute(
                t.update()
                .where(t.c.id == existing.id)
                .values(
                    display_name=candidate.display_name,
                    model_raw=candidate.model_raw,
                    model_normalized=candidate.model_normalized,
                    series=candidate.series,
                    product_family=candidate.product_family.value,
                    product_type=candidate.product_type.value,
                    source_category=candidate.source_category,
                    source_url=candidate.source_url,
                    last_seen_at=seen_text,
                    last_seen_run_id=run_id,
                    updated_at=seen_text,
                )
            )
            return UpsertResult(entity_id=existing.id, created=False)

    def upsert_hardware_revision(
        self,
        *,
        product_id: str,
        candidate: HardwareRevisionCandidate,
        run_id: str,
        seen_at: datetime,
    ) -> UpsertResult:
        t = schema.hardware_revisions
        seen_text = format_rfc3339(seen_at)
        with _wrap_errors("保存硬件版本"):
            existing = self._conn.execute(
                sa.select(t.c.id).where(
                    t.c.product_id == product_id, t.c.source_key == candidate.source_key
                )
            ).first()
            if existing is None:
                revision_id = new_id()
                self._conn.execute(
                    t.insert().values(
                        id=revision_id,
                        product_id=product_id,
                        source_key=candidate.source_key,
                        raw_revision=candidate.raw_revision,
                        normalized_revision=candidate.normalized_revision,
                        revision_explicit=int(candidate.revision_explicit),
                        source_url=candidate.source_url,
                        first_seen_at=seen_text,
                        last_seen_at=seen_text,
                        last_seen_run_id=run_id,
                        created_at=seen_text,
                        updated_at=seen_text,
                    )
                )
                return UpsertResult(entity_id=revision_id, created=True)
            self._conn.execute(
                t.update()
                .where(t.c.id == existing.id)
                .values(
                    raw_revision=candidate.raw_revision,
                    normalized_revision=candidate.normalized_revision,
                    revision_explicit=int(candidate.revision_explicit),
                    source_url=candidate.source_url,
                    last_seen_at=seen_text,
                    last_seen_run_id=run_id,
                    updated_at=seen_text,
                )
            )
            return UpsertResult(entity_id=existing.id, created=False)

    def upsert_release(
        self,
        *,
        hardware_revision_id: str,
        candidate: FirmwareReleaseCandidate,
        run_id: str,
        seen_at: datetime,
    ) -> UpsertResult:
        t = schema.firmware_releases
        seen_text = format_rfc3339(seen_at)
        release_date_text = (
            candidate.release_date.isoformat() if candidate.release_date is not None else None
        )
        with _wrap_errors("保存固件发布"):
            existing = self._conn.execute(
                sa.select(t.c.id).where(
                    t.c.hardware_revision_id == hardware_revision_id,
                    t.c.source_key == candidate.source_key,
                )
            ).first()
            if existing is None:
                release_id = new_id()
                self._conn.execute(
                    t.insert().values(
                        id=release_id,
                        hardware_revision_id=hardware_revision_id,
                        source_key=candidate.source_key,
                        version_raw=candidate.version_raw,
                        version_normalized=candidate.version_normalized,
                        release_date=release_date_text,
                        title=candidate.title,
                        release_notes=candidate.release_notes,
                        release_notes_url=candidate.release_notes_url,
                        source_url=candidate.source_url,
                        visibility_status=VisibilityStatus.ACTIVE.value,
                        first_seen_at=seen_text,
                        last_seen_at=seen_text,
                        disappeared_at=None,
                        last_seen_run_id=run_id,
                        created_at=seen_text,
                        updated_at=seen_text,
                    )
                )
                return UpsertResult(entity_id=release_id, created=True)
            # 重新出现：恢复 active 并清空 disappeared_at（AC-17）
            self._conn.execute(
                t.update()
                .where(t.c.id == existing.id)
                .values(
                    version_raw=candidate.version_raw,
                    version_normalized=candidate.version_normalized,
                    release_date=release_date_text,
                    title=candidate.title,
                    release_notes=candidate.release_notes,
                    release_notes_url=candidate.release_notes_url,
                    source_url=candidate.source_url,
                    visibility_status=VisibilityStatus.ACTIVE.value,
                    disappeared_at=None,
                    last_seen_at=seen_text,
                    last_seen_run_id=run_id,
                    updated_at=seen_text,
                )
            )
            return UpsertResult(entity_id=existing.id, created=False)

    def upsert_artifact(
        self,
        *,
        release_id: str,
        candidate: FirmwareArtifactCandidate,
        run_id: str,
        seen_at: datetime,
    ) -> UpsertResult:
        t = schema.firmware_artifacts
        seen_text = format_rfc3339(seen_at)
        checksum = candidate.official_checksum
        with _wrap_errors("保存固件文件"):
            existing = self._conn.execute(
                sa.select(t.c.id, t.c.download_url).where(
                    t.c.release_id == release_id, t.c.source_key == candidate.source_key
                )
            ).first()
            if existing is None:
                artifact_id = new_id()
                self._conn.execute(
                    t.insert().values(
                        id=artifact_id,
                        release_id=release_id,
                        source_key=candidate.source_key,
                        artifact_type=candidate.artifact_type.value,
                        original_filename=candidate.original_filename,
                        download_url=candidate.download_url,
                        url_last_resolved_at=seen_text,
                        url_expires_at=_opt_format(candidate.url_expires_at),
                        advertised_size=candidate.advertised_size,
                        media_type=candidate.media_type,
                        official_checksum_algorithm=checksum.algorithm if checksum else None,
                        official_checksum_value=checksum.value if checksum else None,
                        visibility_status=VisibilityStatus.ACTIVE.value,
                        first_seen_at=seen_text,
                        last_seen_at=seen_text,
                        disappeared_at=None,
                        last_seen_run_id=run_id,
                        created_at=seen_text,
                        updated_at=seen_text,
                    )
                )
                return UpsertResult(entity_id=artifact_id, created=True)
            values = {
                "artifact_type": candidate.artifact_type.value,
                "original_filename": candidate.original_filename,
                "download_url": candidate.download_url,
                "url_expires_at": _opt_format(candidate.url_expires_at),
                "advertised_size": candidate.advertised_size,
                "media_type": candidate.media_type,
                "official_checksum_algorithm": checksum.algorithm if checksum else None,
                "official_checksum_value": checksum.value if checksum else None,
                "visibility_status": VisibilityStatus.ACTIVE.value,
                "disappeared_at": None,
                "last_seen_at": seen_text,
                "last_seen_run_id": run_id,
                "updated_at": seen_text,
            }
            # 下载地址发生变化才刷新解析时间，方便判断地址新旧
            if candidate.download_url != existing.download_url:
                values["url_last_resolved_at"] = seen_text
            self._conn.execute(t.update().where(t.c.id == existing.id).values(**values))
            return UpsertResult(entity_id=existing.id, created=False)


def _issues_to_json(issues: Sequence[AdapterIssue]) -> str:
    return json.dumps(
        [{"code": i.code, "detail": i.detail, "source_url": i.source_url} for i in issues],
        ensure_ascii=False,
    )


def _issues_from_json(text: str) -> tuple[AdapterIssue, ...]:
    return tuple(AdapterIssue(**item) for item in json.loads(text))


def _run_from_row(row: sa.Row) -> CrawlRun:
    return CrawlRun(
        id=row.id,
        source_id=row.source_id,
        status=CrawlRunStatus(row.status),
        is_complete=bool(row.is_complete),
        started_at=parse_rfc3339(row.started_at),
        finished_at=_opt_parse(row.finished_at),
        products_seen=row.products_seen,
        releases_seen=row.releases_seen,
        artifacts_seen=row.artifacts_seen,
        items_added=row.items_added,
        items_updated=row.items_updated,
        items_disappeared=row.items_disappeared,
        items_skipped=row.items_skipped,
        error_count=row.error_count,
        error_summary=row.error_summary,
        issues=_issues_from_json(row.issues_json),
        created_at=parse_rfc3339(row.created_at),
    )


class SqliteCrawlRunRepository:
    def __init__(self, conn: sa.Connection) -> None:
        self._conn = conn

    def create_run(self, *, source_id: str, started_at: datetime) -> CrawlRun:
        t = schema.crawl_runs
        run_id = new_id()
        started_text = format_rfc3339(started_at)
        with _wrap_errors("创建采集运行"):
            self._conn.execute(
                t.insert().values(
                    id=run_id,
                    source_id=source_id,
                    status=CrawlRunStatus.RUNNING.value,
                    is_complete=0,
                    started_at=started_text,
                    finished_at=None,
                    created_at=started_text,
                )
            )
            row = self._conn.execute(sa.select(t).where(t.c.id == run_id)).one()
        return _run_from_row(row)

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
    ) -> None:
        t = schema.crawl_runs
        with _wrap_errors("收尾采集运行"):
            result = self._conn.execute(
                t.update()
                .where(t.c.id == run_id)
                .values(
                    status=status.value,
                    is_complete=int(is_complete),
                    finished_at=format_rfc3339(finished_at),
                    products_seen=stats.products_seen,
                    releases_seen=stats.releases_seen,
                    artifacts_seen=stats.artifacts_seen,
                    items_added=stats.items_added,
                    items_updated=stats.items_updated,
                    items_disappeared=stats.items_disappeared,
                    items_skipped=stats.items_skipped,
                    error_count=stats.error_count,
                    error_summary=error_summary,
                    issues_json=_issues_to_json(issues),
                )
            )
        if result.rowcount == 0:
            raise RepositoryError(f"收尾采集运行失败：运行 {run_id} 不存在")

    def list_runs(self, *, source_id: str | None = None, limit: int = 50) -> list[CrawlRun]:
        t = schema.crawl_runs
        query = sa.select(t).order_by(t.c.started_at.desc(), t.c.id).limit(limit)
        if source_id is not None:
            query = query.where(t.c.source_id == source_id)
        with _wrap_errors("查询采集运行"):
            rows = self._conn.execute(query).all()
        return [_run_from_row(row) for row in rows]

    def find_stale_running(self) -> list[CrawlRun]:
        t = schema.crawl_runs
        query = sa.select(t).where(t.c.status == CrawlRunStatus.RUNNING.value)
        with _wrap_errors("查询遗留运行"):
            rows = self._conn.execute(query).all()
        return [_run_from_row(row) for row in rows]


class SqliteUnitOfWork:
    """一次事务内可用的各 Repository 集合。由工厂创建，业务层不直接构造。"""

    def __init__(self, conn: sa.Connection) -> None:
        self.sources = SqliteSourceRepository(conn)
        self.catalog = SqliteCatalogRepository(conn)
        self.runs = SqliteCrawlRunRepository(conn)


class SqliteUnitOfWorkFactory:
    """用法：with factory.begin() as uow: ...  正常退出提交，抛异常回滚。"""

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    @contextmanager
    def begin(self) -> Iterator[SqliteUnitOfWork]:
        try:
            with self._engine.begin() as conn:
                yield SqliteUnitOfWork(conn)
        except FirmAtlasError:
            raise
        except sa.exc.SQLAlchemyError as exc:
            raise RepositoryError(f"数据库事务失败（{type(exc).__name__}）") from exc
