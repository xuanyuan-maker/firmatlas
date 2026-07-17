"""CatalogQueryService 的 SQLite 实现（接口设计 §6.4）。

只读跨表查询：sources → products → hardware_revisions → firmware_releases，
Artifact 计数用子查询；download/verification 筛选用 EXISTS（发布下任一
Artifact 的最近一次下载满足条件即命中）。
"""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa

from firmatlas.app.queries import (
    ArtifactDetail,
    CatalogFilter,
    CatalogPage,
    FirmwareListRow,
    ReleaseDetail,
)
from firmatlas.domain.model import (
    ArtifactType,
    DownloadStatus,
    OfficialChecksum,
    ProductType,
    VerificationStatus,
    VisibilityStatus,
)
from firmatlas.domain.timeutil import parse_rfc3339
from firmatlas.infra import schema

_S = schema.firmware_sources
_P = schema.products
_H = schema.hardware_revisions
_R = schema.firmware_releases
_A = schema.firmware_artifacts
_D = schema.download_records


class SqliteCatalogQueryService:
    """用法：service = SqliteCatalogQueryService(engine)；每次查询独立连接。"""

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    # -- list ------------------------------------------------------------

    def list_firmware(self, f: CatalogFilter) -> CatalogPage:
        conditions = self._build_conditions(f)
        joined = (
            _R.join(_H, _R.c.hardware_revision_id == _H.c.id)
            .join(_P, _H.c.product_id == _P.c.id)
            .join(_S, _P.c.source_id == _S.c.id)
        )

        artifact_count = (
            sa.select(sa.func.count())
            .where(_A.c.release_id == _R.c.id)
            .scalar_subquery()
            .label("artifact_count")
        )

        stmt = (
            sa.select(
                _R.c.id,
                _S.c.source_key,
                _S.c.vendor_key,
                _S.c.region_code,
                _P.c.model_raw,
                _P.c.product_type,
                _P.c.series,
                _H.c.normalized_revision,
                _R.c.version_raw,
                _R.c.version_normalized,
                _R.c.release_date,
                _R.c.visibility_status,
                artifact_count,
                _R.c.last_seen_at,
            )
            .select_from(joined)
            .where(*conditions)
            .order_by(_P.c.model_normalized, _H.c.normalized_revision, _R.c.version_raw)
            .limit(f.limit)
            .offset(f.offset)
        )
        count_stmt = sa.select(sa.func.count()).select_from(joined).where(*conditions)

        with self._engine.connect() as conn:
            total = conn.execute(count_stmt).scalar_one()
            rows = [
                FirmwareListRow(
                    release_id=row.id,
                    source_key=row.source_key,
                    vendor_key=row.vendor_key,
                    region_code=row.region_code,
                    model=row.model_raw,
                    product_type=ProductType(row.product_type),
                    series=row.series,
                    hardware=row.normalized_revision,
                    version=row.version_raw,
                    version_normalized=row.version_normalized,
                    release_date=date.fromisoformat(row.release_date)
                    if row.release_date
                    else None,
                    visibility=VisibilityStatus(row.visibility_status),
                    artifact_count=row.artifact_count,
                    last_seen_at=parse_rfc3339(row.last_seen_at),
                )
                for row in conn.execute(stmt)
            ]
        return CatalogPage(rows=rows, total=total)

    def _build_conditions(self, f: CatalogFilter) -> list[sa.ColumnElement[bool]]:
        conditions: list[sa.ColumnElement[bool]] = []
        if f.vendor is not None:
            conditions.append(_S.c.vendor_key == f.vendor)
        if f.source is not None:
            conditions.append(_S.c.source_key == f.source)
        if f.region is not None:
            conditions.append(sa.func.upper(_S.c.region_code) == f.region.upper())
        if f.family is not None:
            conditions.append(_P.c.product_family == f.family.value)
        if f.type is not None:
            conditions.append(_P.c.product_type == f.type.value)
        if f.series is not None:
            conditions.append(_P.c.series.ilike(f"%{_escape_like(f.series)}%", escape="\\"))
        if f.model is not None:
            conditions.append(
                _P.c.model_normalized.ilike(f"%{_escape_like(f.model.lower())}%", escape="\\")
            )
        if f.hardware is not None:
            conditions.append(
                _H.c.normalized_revision.ilike(f"%{_escape_like(f.hardware)}%", escape="\\")
            )
        if f.version is not None:
            conditions.append(
                sa.or_(
                    _R.c.version_raw.ilike(f"%{_escape_like(f.version)}%", escape="\\"),
                    _R.c.version_normalized.ilike(f"%{_escape_like(f.version)}%", escape="\\"),
                )
            )
        if f.visibility is not None:
            conditions.append(_R.c.visibility_status == f.visibility.value)
        if f.download_status is not None:
            conditions.append(self._exists_download(_D.c.status, f.download_status.value))
        if f.verification_status is not None:
            conditions.append(
                self._exists_download(_D.c.verification_status, f.verification_status.value)
            )
        return conditions

    @staticmethod
    def _exists_download(column: sa.Column, value: str) -> sa.ColumnElement[bool]:
        """发布下任一 Artifact 的最近一次下载记录满足条件即命中。"""
        latest = (
            sa.select(_D.c.id)
            .where(_D.c.artifact_id == _A.c.id)
            .order_by(_D.c.requested_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        return sa.exists(
            sa.select(_A.c.id).where(
                _A.c.release_id == _R.c.id,
                sa.exists(sa.select(_D.c.id).where(_D.c.id == latest, column == value)),
            )
        )

    def find_release_ids_by_prefix(self, prefix: str, *, limit: int = 5) -> list[str]:
        """按 ID 前缀查找发布 ID（show 支持 list 输出的短 ID）。"""
        stmt = (
            sa.select(_R.c.id)
            .where(_R.c.id.like(f"{_escape_like(prefix)}%", escape="\\"))
            .limit(limit)
        )
        with self._engine.connect() as conn:
            return [row.id for row in conn.execute(stmt)]

    def find_artifact_ids_by_prefix(self, prefix: str, *, limit: int = 5) -> list[str]:
        """按 ID 前缀查找 Artifact ID（download 支持 show 输出的完整/前缀 ID）。"""
        stmt = (
            sa.select(_A.c.id)
            .where(_A.c.id.like(f"{_escape_like(prefix)}%", escape="\\"))
            .limit(limit)
        )
        with self._engine.connect() as conn:
            return [row.id for row in conn.execute(stmt)]

    # -- show ------------------------------------------------------------

    def show_release(self, release_id: str) -> ReleaseDetail | None:
        stmt = (
            sa.select(_R, _H, _P, _S)
            .select_from(
                _R.join(_H, _R.c.hardware_revision_id == _H.c.id)
                .join(_P, _H.c.product_id == _P.c.id)
                .join(_S, _P.c.source_id == _S.c.id)
            )
            .where(_R.c.id == release_id)
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
            if row is None:
                return None
            artifacts = self._load_artifacts(conn, release_id)

        return ReleaseDetail(
            release_id=row[_R.c.id],
            source_key=row[_S.c.source_key],
            vendor_key=row[_S.c.vendor_key],
            region_code=row[_S.c.region_code],
            display_name=row[_P.c.display_name],
            model=row[_P.c.model_raw],
            product_type=ProductType(row[_P.c.product_type]),
            series=row[_P.c.series],
            hardware=row[_H.c.normalized_revision],
            hardware_raw=row[_H.c.raw_revision],
            version=row[_R.c.version_raw],
            version_normalized=row[_R.c.version_normalized],
            release_date=date.fromisoformat(row[_R.c.release_date])
            if row[_R.c.release_date]
            else None,
            title=row[_R.c.title],
            release_notes=row[_R.c.release_notes],
            source_url=row[_R.c.source_url],
            visibility=VisibilityStatus(row[_R.c.visibility_status]),
            first_seen_at=parse_rfc3339(row[_R.c.first_seen_at]),
            last_seen_at=parse_rfc3339(row[_R.c.last_seen_at]),
            disappeared_at=parse_rfc3339(row[_R.c.disappeared_at])
            if row[_R.c.disappeared_at]
            else None,
            artifacts=artifacts,
        )

    def _load_artifacts(
        self, conn: sa.Connection, release_id: str
    ) -> tuple[ArtifactDetail, ...]:
        rows = conn.execute(
            sa.select(_A).where(_A.c.release_id == release_id).order_by(_A.c.source_key)
        ).all()
        details = []
        for row in rows:
            last = conn.execute(
                sa.select(_D.c.status, _D.c.verification_status)
                .where(_D.c.artifact_id == row.id)
                .order_by(_D.c.requested_at.desc())
                .limit(1)
            ).first()
            checksum = None
            if row.official_checksum_algorithm and row.official_checksum_value:
                checksum = OfficialChecksum(
                    algorithm=row.official_checksum_algorithm,
                    value=row.official_checksum_value,
                )
            details.append(
                ArtifactDetail(
                    artifact_id=row.id,
                    artifact_type=ArtifactType(row.artifact_type),
                    original_filename=row.original_filename,
                    download_url=row.download_url,
                    advertised_size=row.advertised_size,
                    media_type=row.media_type,
                    official_checksum=checksum,
                    visibility=VisibilityStatus(row.visibility_status),
                    last_download_status=DownloadStatus(last.status) if last else None,
                    last_verification_status=VerificationStatus(last.verification_status)
                    if last
                    else None,
                )
            )
        return tuple(details)


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，让用户输入的 % 和 _ 按字面匹配。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
