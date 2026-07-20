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
from datetime import date, datetime

import sqlalchemy as sa

from firmatlas.domain.candidates import (
    FirmwareArtifactCandidate,
    FirmwareReleaseCandidate,
    HardwareRevisionCandidate,
    ProductCandidate,
)
from firmatlas.domain.errors import (
    ActiveDownloadExistsError,
    FirmAtlasError,
    InvalidTransitionError,
    RepositoryError,
)
from firmatlas.domain.ids import new_id
from firmatlas.domain.model import (
    AdapterIssue,
    ArtifactContext,
    ArtifactType,
    CrawlRun,
    CrawlRunStatus,
    CrawlStats,
    DisappearanceSummary,
    DiscoveryMethod,
    DownloadPatch,
    DownloadRecord,
    DownloadStatus,
    FirmwareArtifact,
    FirmwareRelease,
    FirmwareSource,
    HardwareRevision,
    OfficialChecksum,
    Product,
    ProductFamily,
    ProductType,
    UpsertResult,
    VerificationStatus,
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


def _product_from_row(row: sa.Row) -> Product:
    return Product(
        id=row.id,
        source_id=row.source_id,
        source_key=row.source_key,
        display_name=row.display_name,
        model_raw=row.model_raw,
        model_normalized=row.model_normalized,
        series=row.series,
        product_family=ProductFamily(row.product_family),
        product_type=ProductType(row.product_type),
        source_category=row.source_category,
        source_url=row.source_url,
        first_seen_at=parse_rfc3339(row.first_seen_at),
        last_seen_at=parse_rfc3339(row.last_seen_at),
        last_seen_run_id=row.last_seen_run_id,
        created_at=parse_rfc3339(row.created_at),
        updated_at=parse_rfc3339(row.updated_at),
    )


def _revision_from_row(row: sa.Row) -> HardwareRevision:
    return HardwareRevision(
        id=row.id,
        product_id=row.product_id,
        source_key=row.source_key,
        raw_revision=row.raw_revision,
        normalized_revision=row.normalized_revision,
        revision_explicit=bool(row.revision_explicit),
        source_url=row.source_url,
        first_seen_at=parse_rfc3339(row.first_seen_at),
        last_seen_at=parse_rfc3339(row.last_seen_at),
        last_seen_run_id=row.last_seen_run_id,
        created_at=parse_rfc3339(row.created_at),
        updated_at=parse_rfc3339(row.updated_at),
    )


def _release_from_row(row: sa.Row) -> FirmwareRelease:
    return FirmwareRelease(
        id=row.id,
        hardware_revision_id=row.hardware_revision_id,
        source_key=row.source_key,
        version_raw=row.version_raw,
        version_normalized=row.version_normalized,
        release_date=date.fromisoformat(row.release_date) if row.release_date else None,
        title=row.title,
        release_notes=row.release_notes,
        release_notes_url=row.release_notes_url,
        source_url=row.source_url,
        visibility_status=VisibilityStatus(row.visibility_status),
        first_seen_at=parse_rfc3339(row.first_seen_at),
        last_seen_at=parse_rfc3339(row.last_seen_at),
        disappeared_at=_opt_parse(row.disappeared_at),
        last_seen_run_id=row.last_seen_run_id,
        created_at=parse_rfc3339(row.created_at),
        updated_at=parse_rfc3339(row.updated_at),
    )


def _artifact_from_row(row: sa.Row) -> FirmwareArtifact:
    checksum = None
    if row.official_checksum_algorithm is not None and row.official_checksum_value is not None:
        checksum = OfficialChecksum(
            algorithm=row.official_checksum_algorithm, value=row.official_checksum_value
        )
    return FirmwareArtifact(
        id=row.id,
        release_id=row.release_id,
        source_key=row.source_key,
        artifact_type=ArtifactType(row.artifact_type),
        original_filename=row.original_filename,
        download_url=row.download_url,
        url_last_resolved_at=parse_rfc3339(row.url_last_resolved_at),
        url_expires_at=_opt_parse(row.url_expires_at),
        advertised_size=row.advertised_size,
        media_type=row.media_type,
        official_checksum=checksum,
        visibility_status=VisibilityStatus(row.visibility_status),
        first_seen_at=parse_rfc3339(row.first_seen_at),
        last_seen_at=parse_rfc3339(row.last_seen_at),
        disappeared_at=_opt_parse(row.disappeared_at),
        last_seen_run_id=row.last_seen_run_id,
        created_at=parse_rfc3339(row.created_at),
        updated_at=parse_rfc3339(row.updated_at),
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

    def mark_unseen_as_disappeared(
        self, *, source_id: str, run_id: str, confirmed_at: datetime
    ) -> DisappearanceSummary:
        """将该来源下本轮未见到（last_seen_run_id != run_id）的 active 发布
        与 Artifact 置为 disappeared。只在完整采集成功后由用例调用（AC-15）。
        """
        prod = schema.products
        rev = schema.hardware_revisions
        rel = schema.firmware_releases
        art = schema.firmware_artifacts
        confirmed_text = format_rfc3339(confirmed_at)

        # 该来源下所有发布/Artifact 的 ID（经 产品→硬件版本→发布 外键链回溯）
        release_ids = (
            sa.select(rel.c.id)
            .select_from(
                rel.join(rev, rel.c.hardware_revision_id == rev.c.id).join(
                    prod, rev.c.product_id == prod.c.id
                )
            )
            .where(prod.c.source_id == source_id)
        )
        artifact_ids = (
            sa.select(art.c.id)
            .select_from(
                art.join(rel, art.c.release_id == rel.c.id)
                .join(rev, rel.c.hardware_revision_id == rev.c.id)
                .join(prod, rev.c.product_id == prod.c.id)
            )
            .where(prod.c.source_id == source_id)
        )
        disappeared_values = {
            "visibility_status": VisibilityStatus.DISAPPEARED.value,
            "disappeared_at": confirmed_text,
            "updated_at": confirmed_text,
        }
        with _wrap_errors("消失对账"):
            releases_result = self._conn.execute(
                rel.update()
                .where(
                    rel.c.id.in_(release_ids),
                    rel.c.visibility_status == VisibilityStatus.ACTIVE.value,
                    rel.c.last_seen_run_id != run_id,
                )
                .values(**disappeared_values)
            )
            artifacts_result = self._conn.execute(
                art.update()
                .where(
                    art.c.id.in_(artifact_ids),
                    art.c.visibility_status == VisibilityStatus.ACTIVE.value,
                    art.c.last_seen_run_id != run_id,
                )
                .values(**disappeared_values)
            )
        return DisappearanceSummary(
            releases_disappeared=releases_result.rowcount,
            artifacts_disappeared=artifacts_result.rowcount,
        )

    def update_artifact_url(
        self,
        *,
        artifact_id: str,
        download_url: str,
        url_expires_at: datetime | None,
        resolved_at: datetime,
    ) -> None:
        t = schema.firmware_artifacts
        resolved_text = format_rfc3339(resolved_at)
        with _wrap_errors("更新下载地址"):
            result = self._conn.execute(
                t.update()
                .where(t.c.id == artifact_id)
                .values(
                    download_url=download_url,
                    url_expires_at=_opt_format(url_expires_at),
                    url_last_resolved_at=resolved_text,
                    updated_at=resolved_text,
                )
            )
        if result.rowcount == 0:
            raise RepositoryError(f"更新下载地址失败：Artifact {artifact_id} 不存在")

    def get_artifact_context(self, artifact_id: str) -> ArtifactContext | None:
        with _wrap_errors("查询 Artifact 上下文"):
            art_row = self._conn.execute(
                sa.select(schema.firmware_artifacts).where(
                    schema.firmware_artifacts.c.id == artifact_id
                )
            ).first()
            if art_row is None:
                return None
            rel_row = self._conn.execute(
                sa.select(schema.firmware_releases).where(
                    schema.firmware_releases.c.id == art_row.release_id
                )
            ).one()
            rev_row = self._conn.execute(
                sa.select(schema.hardware_revisions).where(
                    schema.hardware_revisions.c.id == rel_row.hardware_revision_id
                )
            ).one()
            prod_row = self._conn.execute(
                sa.select(schema.products).where(schema.products.c.id == rev_row.product_id)
            ).one()
            source_row = self._conn.execute(
                sa.select(schema.firmware_sources).where(
                    schema.firmware_sources.c.id == prod_row.source_id
                )
            ).one()
        return ArtifactContext(
            source=_source_from_row(source_row),
            product=_product_from_row(prod_row),
            hardware_revision=_revision_from_row(rev_row),
            release=_release_from_row(rel_row),
            artifact=_artifact_from_row(art_row),
        )


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


def _download_from_row(row: sa.Row) -> DownloadRecord:
    return DownloadRecord(
        id=row.id,
        artifact_id=row.artifact_id,
        status=DownloadStatus(row.status),
        verification_status=VerificationStatus(row.verification_status),
        requested_at=parse_rfc3339(row.requested_at),
        started_at=_opt_parse(row.started_at),
        finished_at=_opt_parse(row.finished_at),
        resolved_url=row.resolved_url,
        url_refresh_count=row.url_refresh_count,
        temporary_relative_path=row.temporary_relative_path,
        final_relative_path=row.final_relative_path,
        bytes_received=row.bytes_received,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        attempt_count=row.attempt_count,
        http_etag=row.http_etag,
        http_last_modified=row.http_last_modified,
        error_code=row.error_code,
        error_message=row.error_message,
    )


# 下载记录状态机：downloading → downloading 用于过程中更新进度字段
_ALLOWED_TRANSITIONS: dict[DownloadStatus, frozenset[DownloadStatus]] = {
    DownloadStatus.QUEUED: frozenset(
        {
            DownloadStatus.DOWNLOADING,
            DownloadStatus.FAILED,
            DownloadStatus.CANCELLED,
            DownloadStatus.INTERRUPTED,
        }
    ),
    DownloadStatus.DOWNLOADING: frozenset(
        {
            DownloadStatus.DOWNLOADING,
            DownloadStatus.COMPLETED,
            DownloadStatus.FAILED,
            DownloadStatus.CANCELLED,
            DownloadStatus.INTERRUPTED,
        }
    ),
    DownloadStatus.COMPLETED: frozenset(),
    DownloadStatus.FAILED: frozenset(),
    DownloadStatus.CANCELLED: frozenset(),
    DownloadStatus.INTERRUPTED: frozenset(),
}

# DownloadPatch 中不需要类型转换、可直接写入同名列的字段
_PATCH_PLAIN_FIELDS = (
    "resolved_url",
    "url_refresh_count",
    "temporary_relative_path",
    "final_relative_path",
    "bytes_received",
    "size_bytes",
    "sha256",
    "attempt_count",
    "http_etag",
    "http_last_modified",
    "error_code",
    "error_message",
)


class SqliteDownloadRepository:
    def __init__(self, conn: sa.Connection) -> None:
        self._conn = conn

    def create_download(self, *, artifact_id: str, requested_at: datetime) -> DownloadRecord:
        t = schema.download_records
        active_statuses = [DownloadStatus.QUEUED.value, DownloadStatus.DOWNLOADING.value]
        with _wrap_errors("创建下载记录"):
            active = self._conn.execute(
                sa.select(t.c.id).where(
                    t.c.artifact_id == artifact_id, t.c.status.in_(active_statuses)
                )
            ).first()
            if active is not None:
                raise ActiveDownloadExistsError(
                    f"Artifact {artifact_id} 已有进行中的下载任务（记录 {active.id}），"
                    f"不允许重复发起（AC-30）"
                )
            download_id = new_id()
            try:
                self._conn.execute(
                    t.insert().values(
                        id=download_id,
                        artifact_id=artifact_id,
                        status=DownloadStatus.QUEUED.value,
                        verification_status=VerificationStatus.NOT_CHECKED.value,
                        requested_at=format_rfc3339(requested_at),
                    )
                )
            except sa.exc.IntegrityError as exc:
                # 部分唯一索引兜底（理论上被上面的预检查挡住）
                if "uq_downloads_one_active_per_artifact" in str(exc):
                    raise ActiveDownloadExistsError(
                        f"Artifact {artifact_id} 已有进行中的下载任务，不允许重复发起（AC-30）"
                    ) from exc
                raise
            row = self._conn.execute(sa.select(t).where(t.c.id == download_id)).one()
        return _download_from_row(row)

    def transition(self, *, download_id: str, patch: DownloadPatch) -> DownloadRecord:
        t = schema.download_records
        with _wrap_errors("推进下载状态"):
            row = self._conn.execute(sa.select(t).where(t.c.id == download_id)).first()
            if row is None:
                raise RepositoryError(f"推进下载状态失败：下载记录 {download_id} 不存在")
            current = DownloadStatus(row.status)
            if patch.status not in _ALLOWED_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"下载记录 {download_id} 不允许从 {current.value} 变迁到 {patch.status.value}"
                )
            values: dict = {"status": patch.status.value}
            if patch.verification_status is not None:
                values["verification_status"] = patch.verification_status.value
            if patch.started_at is not None:
                values["started_at"] = format_rfc3339(patch.started_at)
            if patch.finished_at is not None:
                values["finished_at"] = format_rfc3339(patch.finished_at)
            for field in _PATCH_PLAIN_FIELDS:
                value = getattr(patch, field)
                if value is not None:
                    values[field] = value
            self._conn.execute(t.update().where(t.c.id == download_id).values(**values))
            updated = self._conn.execute(sa.select(t).where(t.c.id == download_id)).one()
        return _download_from_row(updated)

    def list_downloads(
        self,
        *,
        status: DownloadStatus | None = None,
        artifact_id: str | None = None,
        limit: int = 50,
    ) -> list[DownloadRecord]:
        t = schema.download_records
        query = sa.select(t).order_by(t.c.requested_at.desc(), t.c.id).limit(limit)
        if status is not None:
            query = query.where(t.c.status == status.value)
        if artifact_id is not None:
            query = query.where(t.c.artifact_id == artifact_id)
        with _wrap_errors("查询下载记录"):
            rows = self._conn.execute(query).all()
        return [_download_from_row(row) for row in rows]

    def find_stale_active(self) -> list[DownloadRecord]:
        """启动时识别崩溃遗留的 queued/downloading 记录。

        遗留的 queued 也要找出来：它会占住“单活动任务”约束，阻塞后续下载。
        """
        t = schema.download_records
        query = sa.select(t).where(
            t.c.status.in_([DownloadStatus.QUEUED.value, DownloadStatus.DOWNLOADING.value])
        )
        with _wrap_errors("查询遗留下载"):
            rows = self._conn.execute(query).all()
        return [_download_from_row(row) for row in rows]


class SqliteUnitOfWork:
    """一次事务内可用的各 Repository 集合。由工厂创建，业务层不直接构造。"""

    def __init__(self, conn: sa.Connection) -> None:
        self.sources = SqliteSourceRepository(conn)
        self.catalog = SqliteCatalogRepository(conn)
        self.runs = SqliteCrawlRunRepository(conn)
        self.downloads = SqliteDownloadRepository(conn)


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
