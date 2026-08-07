"""Catalog 快照的 SQLite 一致性备份、校验、统计和压缩。"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from firmatlas.domain.errors import CatalogExportError
from firmatlas.infra.schema import SCHEMA_VERSION

_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class CatalogSourceStats:
    source_key: str
    last_success_at: str | None
    last_status: str
    products: int
    releases: int
    artifacts: int


@dataclass(frozen=True)
class CatalogSnapshotStats:
    sources: int
    products: int
    releases: int
    artifacts: int
    downloads: int
    source_stats: tuple[CatalogSourceStats, ...]


@dataclass(frozen=True)
class CompressedDatabase:
    path: Path
    compressed_size: int
    uncompressed_size: int
    compressed_sha256: str
    database_sha256: str


def backup_database(source_path: Path, target_path: Path) -> None:
    """通过 SQLite Backup API 生成一致性副本。"""
    if not source_path.is_file():
        raise CatalogExportError(f"规范数据库 {source_path} 不存在。")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
            source.backup(target)
    except sqlite3.Error as exc:
        raise CatalogExportError(f"无法创建 SQLite 一致性副本：{exc}") from exc


def inspect_clean_database(database_path: Path) -> CatalogSnapshotStats:
    """校验纯净数据库约束，并返回 manifest 所需统计。"""
    try:
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            _check_integrity(connection)
            _check_schema_version(connection)
            downloads = _count(connection, "download_records")
            if downloads != 0:
                raise CatalogExportError(
                    f"规范数据库包含 {downloads} 条 download_records，不能导出纯净快照。"
                )
            running = connection.execute(
                "SELECT COUNT(*) FROM crawl_runs WHERE status = 'running'"
            ).fetchone()[0]
            if running != 0:
                raise CatalogExportError(
                    f"规范数据库包含 {running} 个 running CrawlRun，不能导出快照。"
                )
            return _collect_stats(connection)
    except CatalogExportError:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise CatalogExportError(f"无法读取或校验候选数据库：{exc}") from exc


def compress_database(database_path: Path, compressed_path: Path) -> CompressedDatabase:
    """使用固定 mtime 的 gzip 压缩数据库，并计算压缩前后哈希。"""
    database_hash = hashlib.sha256()
    compressed_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        uncompressed_size = 0
        with (
            database_path.open("rb") as source,
            compressed_path.open("wb") as raw_target,
            gzip.GzipFile(fileobj=raw_target, mode="wb", mtime=0) as target,
        ):
            while chunk := source.read(_CHUNK_SIZE):
                database_hash.update(chunk)
                uncompressed_size += len(chunk)
                target.write(chunk)
        compressed_hash = _sha256_file(compressed_path)
    except (OSError, gzip.BadGzipFile) as exc:
        raise CatalogExportError(f"压缩候选数据库失败：{exc}") from exc
    return CompressedDatabase(
        path=compressed_path,
        compressed_size=compressed_path.stat().st_size,
        uncompressed_size=uncompressed_size,
        compressed_sha256=compressed_hash,
        database_sha256=database_hash.hexdigest(),
    )


def write_sha256_file(path: Path, digest: str, filename: str) -> None:
    try:
        path.write_text(f"{digest}  {filename}\n", encoding="ascii")
    except OSError as exc:
        raise CatalogExportError(f"无法写入 SHA-256 校验文件：{exc}") from exc


def _check_integrity(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise CatalogExportError(f"SQLite integrity_check 失败：{integrity}")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise CatalogExportError("SQLite foreign_key_check 发现外键错误。")


def _check_schema_version(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise CatalogExportError(
            f"候选数据库 schema_version={version}，当前程序要求 {SCHEMA_VERSION}。"
        )


def _collect_stats(connection: sqlite3.Connection) -> CatalogSnapshotStats:
    source_rows = connection.execute(
        "SELECT id, source_key FROM firmware_sources ORDER BY source_key"
    ).fetchall()
    source_stats = tuple(
        _source_stats(connection, source_id=row["id"], source_key=row["source_key"])
        for row in source_rows
    )
    return CatalogSnapshotStats(
        sources=len(source_rows),
        products=_count(connection, "products"),
        releases=_count(connection, "firmware_releases"),
        artifacts=_count(connection, "firmware_artifacts"),
        downloads=0,
        source_stats=source_stats,
    )


def _source_stats(
    connection: sqlite3.Connection, *, source_id: str, source_key: str
) -> CatalogSourceStats:
    last_run = connection.execute(
        """
        SELECT status
        FROM crawl_runs
        WHERE source_id = ?
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (source_id,),
    ).fetchone()
    last_success = connection.execute(
        """
        SELECT MAX(finished_at)
        FROM crawl_runs
        WHERE source_id = ? AND status = 'completed' AND is_complete = 1
        """,
        (source_id,),
    ).fetchone()[0]
    products = connection.execute(
        "SELECT COUNT(*) FROM products WHERE source_id = ?", (source_id,)
    ).fetchone()[0]
    releases = connection.execute(
        """
        SELECT COUNT(*)
        FROM firmware_releases AS releases
        JOIN hardware_revisions AS revisions
          ON revisions.id = releases.hardware_revision_id
        JOIN products ON products.id = revisions.product_id
        WHERE products.source_id = ?
        """,
        (source_id,),
    ).fetchone()[0]
    artifacts = connection.execute(
        """
        SELECT COUNT(*)
        FROM firmware_artifacts AS artifacts
        JOIN firmware_releases AS releases ON releases.id = artifacts.release_id
        JOIN hardware_revisions AS revisions
          ON revisions.id = releases.hardware_revision_id
        JOIN products ON products.id = revisions.product_id
        WHERE products.source_id = ?
        """,
        (source_id,),
    ).fetchone()[0]
    return CatalogSourceStats(
        source_key=source_key,
        last_success_at=last_success,
        last_status=last_run["status"] if last_run is not None else "never",
        products=products,
        releases=releases,
        artifacts=artifacts,
    )


def _count(connection: sqlite3.Connection, table: str) -> int:
    # table 名称只来自本模块固定常量，不能由外部输入。
    return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()
