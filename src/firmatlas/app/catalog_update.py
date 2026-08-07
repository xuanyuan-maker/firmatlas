"""Catalog 更新前置检查与差异报告。"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from firmatlas.app.catalog_manifest import CatalogCounts, CatalogManifest
from firmatlas.app.config import AppConfig
from firmatlas.domain.errors import CatalogUpdateError
from firmatlas.domain.timeutil import format_rfc3339, utc_now
from firmatlas.infra.catalog_snapshot import CatalogSnapshotStats, inspect_clean_database
from firmatlas.infra.catalog_source import (
    fetch_manifest,
    read_local_manifest,
    resolve_database_url,
)
from firmatlas.infra.catalog_updater import (
    download_and_extract_database,
    migrate_download_records,
    verify_updated_database,
)
from firmatlas.infra.database import DB_FILENAME
from firmatlas.infra.schema import SCHEMA_VERSION


@dataclass(frozen=True)
class CatalogCheckReport:
    current_lineage_id: str | None
    current_catalog_version: str | None
    remote_lineage_id: str
    remote_catalog_version: str
    update_available: bool
    replace_required: bool
    current_counts: CatalogCounts | None
    remote_counts: CatalogCounts


@dataclass(frozen=True)
class CatalogUpdateReport:
    status: str
    current_catalog_version: str | None
    catalog_version: str
    lineage_id: str
    backup_path: Path | None
    migrated_downloads: int
    warnings: tuple[str, ...]


def check_catalog_update(*, data_dir: Path, config: AppConfig) -> CatalogCheckReport:
    if config.catalog.mode != "managed":
        raise CatalogUpdateError("Standalone 模式不允许执行 Catalog 更新。")
    assert config.catalog.manifest_url is not None
    remote = fetch_manifest(
        config.catalog.manifest_url,
        allow_insecure_http=config.catalog.allow_insecure_http,
        timeout=config.http.request_timeout,
    )
    local = read_local_manifest(data_dir)
    current_version = local.catalog_version if local else None
    update_available = local is None or remote.catalog_version > local.catalog_version
    replace_required = (
        local is None
        or remote.lineage_id != local.lineage_id
        or remote.schema_version != local.schema_version
    )
    return CatalogCheckReport(
        current_lineage_id=local.lineage_id if local else None,
        current_catalog_version=current_version,
        remote_lineage_id=remote.lineage_id,
        remote_catalog_version=remote.catalog_version,
        update_available=update_available,
        replace_required=replace_required,
        current_counts=local.counts if local else None,
        remote_counts=remote.counts,
    )


def validate_candidate_database(
    *, database_path: Path, manifest: CatalogManifest
) -> CatalogSnapshotStats:
    """校验候选库结构、纯净约束、总计数和来源统计。"""
    stats = inspect_clean_database(database_path)
    if manifest.schema_version != SCHEMA_VERSION:
        raise CatalogUpdateError(
            f"manifest schema_version={manifest.schema_version} 与当前程序不兼容。"
        )
    actual_counts = CatalogCounts(
        sources=stats.sources,
        products=stats.products,
        releases=stats.releases,
        artifacts=stats.artifacts,
        downloads=stats.downloads,
    )
    if actual_counts != manifest.counts:
        raise CatalogUpdateError(
            f"候选数据库计数 {actual_counts} 与 manifest 计数 {manifest.counts} 不一致。"
        )
    expected_sources = {source.source_key: source for source in manifest.sources}
    actual_sources = {source.source_key: source for source in stats.source_stats}
    if set(expected_sources) != set(actual_sources):
        raise CatalogUpdateError("候选数据库来源集合与 manifest 不一致。")
    for source_key, expected in expected_sources.items():
        actual = actual_sources[source_key]
        if (
            actual.last_success_at
            != (
                format_rfc3339(expected.last_success_at)
                if expected.last_success_at is not None
                else None
            )
            or actual.last_status != expected.last_status
            or actual.products != expected.products
            or actual.releases != expected.releases
            or actual.artifacts != expected.artifacts
        ):
            raise CatalogUpdateError(f"来源 {source_key} 的 manifest 统计与候选数据库不一致。")
    return stats


def update_catalog(
    *, data_dir: Path, config: AppConfig, replace: bool = False
) -> CatalogUpdateReport:
    """下载候选快照、迁移下载记录并原子替换本地目录数据库。"""
    if config.catalog.mode != "managed":
        raise CatalogUpdateError("Standalone 模式不允许执行 Catalog 更新。")
    assert config.catalog.manifest_url is not None
    remote = fetch_manifest(
        config.catalog.manifest_url,
        allow_insecure_http=config.catalog.allow_insecure_http,
        timeout=config.http.request_timeout,
    )
    local = read_local_manifest(data_dir)
    if local is not None and remote.catalog_version == local.catalog_version:
        return CatalogUpdateReport(
            status="up_to_date",
            current_catalog_version=local.catalog_version,
            catalog_version=remote.catalog_version,
            lineage_id=remote.lineage_id,
            backup_path=None,
            migrated_downloads=0,
            warnings=(),
        )
    _check_remote_version(local, remote)
    if not replace:
        if local is None:
            raise CatalogUpdateError("本地 Catalog 来源未知，首次安装必须使用 --replace。")
        if remote.lineage_id != local.lineage_id:
            raise CatalogUpdateError(
                "Catalog lineage 不一致，普通 update 拒绝操作，请使用 --replace。"
            )
        if remote.schema_version != local.schema_version:
            raise CatalogUpdateError("Catalog schema_version 不兼容，请使用 --replace。")
    if remote.schema_version != SCHEMA_VERSION:
        raise CatalogUpdateError(
            f"远程 Catalog schema_version={remote.schema_version} 与当前程序不兼容。"
        )

    data_dir.mkdir(parents=True, exist_ok=True)
    work_parent = data_dir / "tmp"
    work_parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="catalog-update-", dir=work_parent))
    candidate_path = work_dir / DB_FILENAME
    compressed_path = work_dir / "firmatlas.db.gz"
    database_path = data_dir / DB_FILENAME
    manifest_path = data_dir / "catalog-manifest.json"
    backup_path: Path | None = None
    replaced = False
    old_manifest_exists = manifest_path.exists()
    old_manifest_bytes = manifest_path.read_bytes() if old_manifest_exists else None
    try:
        database_url = resolve_database_url(config.catalog.manifest_url, remote.database.url)
        download_and_extract_database(
            url=database_url,
            compressed_path=compressed_path,
            database_path=candidate_path,
            expected_compressed_sha256=remote.database.compressed_sha256,
            expected_database_sha256=remote.database.database_sha256,
            allow_insecure_http=config.catalog.allow_insecure_http,
            timeout=config.http.request_timeout,
        )
        validate_candidate_database(database_path=candidate_path, manifest=remote)
        migration = migrate_download_records(
            candidate_path=candidate_path,
            old_path=database_path if not replace else None,
            data_dir=data_dir,
        )
        verify_updated_database(candidate_path, migration.migrated_records)
        backup_path = _backup_current_state(
            data_dir=data_dir,
            database_path=database_path,
            manifest_path=manifest_path,
        )
        _fsync_file(candidate_path)
        os.replace(candidate_path, database_path)
        replaced = True
        _atomic_write_manifest(manifest_path, remote.to_json())
        _rotate_backups(data_dir / "backups", config.catalog.backup_count, backup_path)
    except CatalogUpdateError:
        if replaced:
            _rollback_database(
                database_path=database_path,
                backup_path=backup_path,
                manifest_path=manifest_path,
                old_manifest_exists=old_manifest_exists,
                old_manifest_bytes=old_manifest_bytes,
            )
        _remove_work_dir(work_dir)
        raise
    except (OSError, ValueError) as exc:
        if replaced:
            _rollback_database(
                database_path=database_path,
                backup_path=backup_path,
                manifest_path=manifest_path,
                old_manifest_exists=old_manifest_exists,
                old_manifest_bytes=old_manifest_bytes,
            )
        _remove_work_dir(work_dir)
        raise CatalogUpdateError(f"Catalog 原子替换失败：{exc}") from exc
    _remove_work_dir(work_dir)
    warnings = list(migration.warnings)
    if replace and database_path.exists():
        warnings.append("跨 lineage 替换未迁移旧下载记录；data/firmware 文件已保留。")
    return CatalogUpdateReport(
        status="replaced" if replace else "updated",
        current_catalog_version=local.catalog_version if local else None,
        catalog_version=remote.catalog_version,
        lineage_id=remote.lineage_id,
        backup_path=backup_path,
        migrated_downloads=migration.migrated_records,
        warnings=tuple(warnings),
    )


def _check_remote_version(local: CatalogManifest | None, remote: CatalogManifest) -> None:
    if local is None:
        return
    if remote.catalog_version == local.catalog_version:
        raise CatalogUpdateError("Catalog 已是最新版本，无需更新。")
    if remote.catalog_version < local.catalog_version:
        raise CatalogUpdateError("远程 Catalog 版本低于本地版本，拒绝回滚。")


def _backup_current_state(
    *, data_dir: Path, database_path: Path, manifest_path: Path
) -> Path | None:
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"firmatlas-{stamp}-{os.getpid()}.db"
    if database_path.exists():
        shutil.copy2(database_path, backup_path)
        _fsync_file(backup_path)
    if manifest_path.exists():
        manifest_backup = backup_path.with_suffix(".manifest.json")
        shutil.copy2(manifest_path, manifest_backup)
        _fsync_file(manifest_backup)
    return backup_path if database_path.exists() else None


def _atomic_write_manifest(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    _fsync_file(temporary)
    os.replace(temporary, path)


def _rollback_database(
    *,
    database_path: Path,
    backup_path: Path | None,
    manifest_path: Path,
    old_manifest_exists: bool,
    old_manifest_bytes: bytes | None,
) -> None:
    if backup_path is not None and backup_path.exists():
        os.replace(backup_path, database_path)
    elif database_path.exists():
        database_path.unlink()
    if old_manifest_exists and old_manifest_bytes is not None:
        _atomic_write_manifest(manifest_path, old_manifest_bytes.decode("utf-8"))
    elif manifest_path.exists():
        manifest_path.unlink()


def _rotate_backups(backup_dir: Path, backup_count: int, current: Path | None) -> None:
    if current is None:
        return
    backups = sorted(backup_dir.glob("firmatlas-*.db"), key=lambda path: path.stat().st_mtime)
    for stale in backups[:-backup_count]:
        stale.unlink(missing_ok=True)
        stale.with_suffix(".manifest.json").unlink(missing_ok=True)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _remove_work_dir(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
    path.rmdir()
