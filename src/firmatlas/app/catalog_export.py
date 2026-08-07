"""Catalog 纯净快照导出用例。"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from firmatlas import __version__
from firmatlas.app.catalog_manifest import (
    MANIFEST_FORMAT_VERSION,
    CatalogCounts,
    CatalogDatabase,
    CatalogManifest,
    CatalogSource,
)
from firmatlas.domain.errors import CatalogExportError
from firmatlas.domain.timeutil import utc_now
from firmatlas.infra.catalog_snapshot import (
    backup_database,
    compress_database,
    inspect_clean_database,
    write_sha256_file,
)
from firmatlas.infra.database import DB_FILENAME
from firmatlas.infra.schema import SCHEMA_VERSION

LINEAGE_FILENAME = "catalog-lineage-id"


@dataclass(frozen=True)
class CatalogExportReport:
    output_dir: Path
    manifest_path: Path
    database_path: Path
    lineage_id: str
    catalog_version: str
    counts: CatalogCounts


def export_catalog(
    *,
    data_dir: Path,
    output_dir: Path,
    lineage_id: str | None = None,
    catalog_version: str | None = None,
    minimum_firmatlas_version: str = __version__,
    created_at: datetime | None = None,
) -> CatalogExportReport:
    """从规范数据库导出纯净 Catalog 目录，并在成功后原子落地输出目录。"""
    source_path = data_dir / DB_FILENAME
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise CatalogExportError(f"Catalog 输出目录 {output_dir} 已存在，拒绝覆盖。")
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    selected_lineage = lineage_id or _load_or_create_lineage_id(data_dir)
    selected_version = catalog_version or _default_catalog_version(created_at)
    selected_created_at = created_at or utc_now()
    temp_dir = Path(tempfile.mkdtemp(prefix=".firmatlas-catalog-", dir=output_dir.parent))
    candidate_path = temp_dir / DB_FILENAME
    compressed_path = temp_dir / "firmatlas.db.gz"
    try:
        backup_database(source_path, candidate_path)
        stats = inspect_clean_database(candidate_path)
        compressed = compress_database(candidate_path, compressed_path)
        manifest = CatalogManifest(
            format_version=MANIFEST_FORMAT_VERSION,
            lineage_id=selected_lineage,
            catalog_version=selected_version,
            created_at=selected_created_at,
            schema_version=SCHEMA_VERSION,
            minimum_firmatlas_version=minimum_firmatlas_version,
            database=CatalogDatabase(
                url="firmatlas.db.gz",
                compression="gzip",
                compressed_size=compressed.compressed_size,
                uncompressed_size=compressed.uncompressed_size,
                compressed_sha256=compressed.compressed_sha256,
                database_sha256=compressed.database_sha256,
            ),
            counts=CatalogCounts(
                sources=stats.sources,
                products=stats.products,
                releases=stats.releases,
                artifacts=stats.artifacts,
                downloads=stats.downloads,
            ),
            sources=tuple(
                CatalogSource(
                    source_key=source.source_key,
                    last_success_at=_parse_optional_timestamp(source.last_success_at),
                    last_status=source.last_status,
                    products=source.products,
                    releases=source.releases,
                    artifacts=source.artifacts,
                )
                for source in stats.source_stats
            ),
        )
        (temp_dir / "manifest.json").write_text(manifest.to_json(), encoding="utf-8")
        write_sha256_file(
            temp_dir / "firmatlas.db.gz.sha256",
            compressed.compressed_sha256,
            "firmatlas.db.gz",
        )
        candidate_path.unlink()
        _fsync_file(compressed_path)
        _fsync_file(temp_dir / "manifest.json")
        _fsync_file(temp_dir / "firmatlas.db.gz.sha256")
        os.replace(temp_dir, output_dir)
    except CatalogExportError:
        _remove_temp_dir(temp_dir)
        raise
    except (OSError, ValueError) as exc:
        _remove_temp_dir(temp_dir)
        raise CatalogExportError(f"写入 Catalog 快照失败：{exc}") from exc

    return CatalogExportReport(
        output_dir=output_dir,
        manifest_path=output_dir / "manifest.json",
        database_path=output_dir / "firmatlas.db.gz",
        lineage_id=selected_lineage,
        catalog_version=selected_version,
        counts=manifest.counts,
    )


def _load_or_create_lineage_id(data_dir: Path) -> str:
    path = data_dir / LINEAGE_FILENAME
    try:
        if path.exists():
            value = path.read_text(encoding="ascii").strip()
            if value:
                uuid.UUID(value)
                return value
            raise ValueError("文件为空")
        data_dir.mkdir(parents=True, exist_ok=True)
        value = str(uuid.uuid4())
        temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
        temporary.write_text(f"{value}\n", encoding="ascii")
        _fsync_file(temporary)
        os.replace(temporary, path)
        return value
    except (OSError, ValueError) as exc:
        raise CatalogExportError(f"无法读取或创建 lineage_id：{exc}") from exc


def _default_catalog_version(created_at: datetime | None) -> str:
    value = created_at or datetime.now(UTC)
    value = value.astimezone(UTC)
    return value.strftime("%Y.%m.%d.%H%M%S")


def _parse_optional_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    from firmatlas.domain.timeutil import parse_rfc3339

    return parse_rfc3339(value)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _remove_temp_dir(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink()
    path.rmdir()
