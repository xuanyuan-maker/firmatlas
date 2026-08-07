"""Catalog 候选数据库的流式下载、解压与哈希校验。"""

from __future__ import annotations

import gzip
import hashlib
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


def download_and_extract_database(
    *,
    url: str,
    compressed_path: Path,
    database_path: Path,
    expected_compressed_sha256: str,
    expected_database_sha256: str,
    allow_insecure_http: bool = False,
    timeout: float = 60.0,
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
) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with open_catalog_url(url, allow_insecure_http=allow_insecure_http, timeout=timeout) as source:
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
