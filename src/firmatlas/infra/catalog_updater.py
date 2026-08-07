"""Catalog 候选数据库的流式下载、解压与哈希校验。"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from firmatlas.domain.errors import CatalogUpdateError
from firmatlas.infra.catalog_source import CatalogSourceError, open_catalog_url

DEFAULT_MAX_COMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class DownloadedDatabase:
    compressed_path: Path
    database_path: Path
    compressed_size: int
    uncompressed_size: int
    compressed_sha256: str
    database_sha256: str


@dataclass(frozen=True)
class DownloadMigration:
    migrated_records: int
    warnings: tuple[str, ...]


_DOWNLOAD_COLUMNS = (
    "id",
    "artifact_id",
    "status",
    "verification_status",
    "requested_at",
    "started_at",
    "finished_at",
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


def download_and_extract_database(
    *,
    url: str,
    compressed_path: Path,
    database_path: Path,
    expected_compressed_sha256: str,
    expected_database_sha256: str,
    allow_insecure_http: bool = False,
    timeout: float = 60.0,
    max_retries: int = 3,
    retry_backoff_base: float = 1.0,
    max_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> DownloadedDatabase:
    """流式下载 gzip 数据库并在落盘前后验证大小和 SHA-256。"""
    if max_compressed_bytes <= 0 or max_uncompressed_bytes <= 0:
        raise CatalogUpdateError("Catalog 数据库大小限制必须大于 0。")
    compressed_path.parent.mkdir(parents=True, exist_ok=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        compressed_size, compressed_sha256 = _download_compressed(
            url,
            compressed_path,
            allow_insecure_http=allow_insecure_http,
            timeout=timeout,
            max_bytes=max_compressed_bytes,
            max_retries=max_retries,
            retry_backoff_base=retry_backoff_base,
        )
        if compressed_sha256 != expected_compressed_sha256:
            raise CatalogUpdateError("Catalog 数据库压缩包 SHA-256 不匹配，已删除候选文件。")
        uncompressed_size, database_sha256 = _extract_database(
            compressed_path, database_path, max_bytes=max_uncompressed_bytes
        )
        if database_sha256 != expected_database_sha256:
            raise CatalogUpdateError("Catalog 数据库 SHA-256 不匹配，已删除候选文件。")
    except CatalogUpdateError:
        _unlink_if_exists(compressed_path)
        _unlink_if_exists(database_path)
        raise
    except (OSError, EOFError, gzip.BadGzipFile, CatalogSourceError) as exc:
        _unlink_if_exists(compressed_path)
        _unlink_if_exists(database_path)
        raise CatalogUpdateError(f"下载或解压 Catalog 数据库失败：{exc}") from exc
    return DownloadedDatabase(
        compressed_path=compressed_path,
        database_path=database_path,
        compressed_size=compressed_size,
        uncompressed_size=uncompressed_size,
        compressed_sha256=compressed_sha256,
        database_sha256=database_sha256,
    )


def _download_compressed(
    url: str,
    path: Path,
    *,
    allow_insecure_http: bool,
    timeout: float,
    max_bytes: int,
    max_retries: int,
    retry_backoff_base: float,
) -> tuple[int, str]:
    if max_retries < 0 or retry_backoff_base < 0:
        raise CatalogUpdateError("Catalog 下载重试参数不能小于 0。")
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            digest = hashlib.sha256()
            total = 0
            with open_catalog_url(
                url, allow_insecure_http=allow_insecure_http, timeout=timeout
            ) as source:
                content_length = getattr(source, "headers", {}).get("Content-Length")
                if content_length is not None and int(content_length) > max_bytes:
                    raise CatalogUpdateError("Catalog 压缩包 Content-Length 超过大小限制。")
                with path.open("wb") as target:
                    while chunk := source.read(_CHUNK_SIZE):
                        total += len(chunk)
                        if total > max_bytes:
                            raise CatalogUpdateError("Catalog 压缩包超过大小限制。")
                        digest.update(chunk)
                        target.write(chunk)
            return total, digest.hexdigest()
        except CatalogUpdateError:
            raise
        except (OSError, CatalogSourceError) as exc:
            last_error = exc
            if attempt < max_retries:
                time.sleep(retry_backoff_base * (2**attempt))
    assert last_error is not None
    raise last_error


def _extract_database(
    compressed_path: Path, database_path: Path, *, max_bytes: int
) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with gzip.open(compressed_path, "rb") as source, database_path.open("wb") as target:
        while chunk := source.read(_CHUNK_SIZE):
            total += len(chunk)
            if total > max_bytes:
                raise CatalogUpdateError("Catalog 解压后数据库超过大小限制。")
            digest.update(chunk)
            target.write(chunk)
    return total, digest.hexdigest()


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def migrate_download_records(
    *, candidate_path: Path, old_path: Path | None, data_dir: Path
) -> DownloadMigration:
    """在候选库中迁移旧库下载记录，并验证 Artifact 身份。"""
    if old_path is None or not old_path.exists():
        return DownloadMigration(migrated_records=0, warnings=())
    columns = ", ".join(_DOWNLOAD_COLUMNS)
    try:
        with sqlite3.connect(candidate_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("ATTACH DATABASE ? AS old_db", (str(old_path),))
            try:
                missing = connection.execute(
                    """
                    SELECT d.artifact_id
                    FROM old_db.download_records AS d
                    LEFT JOIN main.firmware_artifacts AS a ON a.id = d.artifact_id
                    WHERE a.id IS NULL
                    LIMIT 1
                    """
                ).fetchone()
                if missing is not None:
                    raise CatalogUpdateError(
                        f"旧下载记录引用的 Artifact {missing[0]} 不存在于候选目录，已中止更新。"
                    )
                count = connection.execute(
                    "SELECT COUNT(*) FROM old_db.download_records"
                ).fetchone()[0]
                if count:
                    connection.execute(
                        f"INSERT INTO main.download_records ({columns}) "
                        f"SELECT {columns} FROM old_db.download_records"
                    )
                    connection.commit()
                warnings = _completed_download_warnings(connection, data_dir)
            finally:
                connection.execute("DETACH DATABASE old_db")
    except CatalogUpdateError:
        raise
    except sqlite3.Error as exc:
        raise CatalogUpdateError(f"迁移旧下载记录失败：{exc}") from exc
    return DownloadMigration(migrated_records=count, warnings=warnings)


def _completed_download_warnings(connection: sqlite3.Connection, data_dir: Path) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT id, final_relative_path
        FROM old_db.download_records
        WHERE status = 'completed'
        """
    ).fetchall()
    warnings: list[str] = []
    root = data_dir.resolve()
    for record_id, relative_path in rows:
        if not relative_path:
            warnings.append(f"下载记录 {record_id} 缺少 final_relative_path。")
            continue
        final_path = (data_dir / relative_path).resolve()
        if root not in final_path.parents:
            warnings.append(f"下载记录 {record_id} 的 final_relative_path 超出数据目录。")
        elif not final_path.is_file():
            warnings.append(f"下载记录 {record_id} 的固件文件不存在：{relative_path}。")
    return tuple(warnings)


def verify_updated_database(database_path: Path, expected_downloads: int) -> None:
    """验证迁移后的候选库仍完整且下载记录数量正确。"""
    try:
        with sqlite3.connect(database_path) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise CatalogUpdateError(f"更新后 SQLite integrity_check 失败：{integrity}")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise CatalogUpdateError("更新后 SQLite foreign_key_check 发现错误。")
            actual = connection.execute("SELECT COUNT(*) FROM download_records").fetchone()[0]
            if actual != expected_downloads:
                raise CatalogUpdateError(
                    f"更新后下载记录数量 {actual} 与预期 {expected_downloads} 不一致。"
                )
    except CatalogUpdateError:
        raise
    except sqlite3.Error as exc:
        raise CatalogUpdateError(f"无法验证更新后的候选数据库：{exc}") from exc
