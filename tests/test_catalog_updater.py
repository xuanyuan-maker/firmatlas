"""Catalog 候选数据库流式下载与校验测试。"""

import gzip
import hashlib

import pytest

from firmatlas.domain.errors import CatalogUpdateError
from firmatlas.infra.catalog_updater import download_and_extract_database


def test_download_and_extract_file_url(tmp_path):
    source = tmp_path / "source.gz"
    payload = b"SQLite candidate database bytes"
    with gzip.open(source, "wb") as handle:
        handle.write(payload)
    compressed_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    database_hash = hashlib.sha256(payload).hexdigest()

    result = download_and_extract_database(
        url=source.as_uri(),
        compressed_path=tmp_path / "tmp" / "source.gz",
        database_path=tmp_path / "tmp" / "candidate.db",
        expected_compressed_sha256=compressed_hash,
        expected_database_sha256=database_hash,
    )

    assert result.compressed_size == source.stat().st_size
    assert result.uncompressed_size == len(payload)
    assert result.database_path.read_bytes() == payload


def test_download_rejects_compressed_hash_mismatch(tmp_path):
    source = tmp_path / "source.gz"
    with gzip.open(source, "wb") as handle:
        handle.write(b"candidate")

    with pytest.raises(CatalogUpdateError, match="压缩包 SHA-256"):
        download_and_extract_database(
            url=source.as_uri(),
            compressed_path=tmp_path / "source-copy.gz",
            database_path=tmp_path / "candidate.db",
            expected_compressed_sha256="a" * 64,
            expected_database_sha256="b" * 64,
        )
    assert not (tmp_path / "source-copy.gz").exists()
    assert not (tmp_path / "candidate.db").exists()


def test_download_rejects_truncated_gzip(tmp_path):
    source = tmp_path / "source.gz"
    with gzip.open(source, "wb") as handle:
        handle.write(b"candidate")
    compressed = source.read_bytes()[:-4]
    source.write_bytes(compressed)

    with pytest.raises(CatalogUpdateError, match="下载或解压"):
        download_and_extract_database(
            url=source.as_uri(),
            compressed_path=tmp_path / "source-copy.gz",
            database_path=tmp_path / "candidate.db",
            expected_compressed_sha256=hashlib.sha256(compressed).hexdigest(),
            expected_database_sha256="b" * 64,
        )


def test_download_enforces_decompression_limit(tmp_path):
    source = tmp_path / "source.gz"
    payload = b"candidate database"
    with gzip.open(source, "wb") as handle:
        handle.write(payload)

    with pytest.raises(CatalogUpdateError, match="解压后数据库超过"):
        download_and_extract_database(
            url=source.as_uri(),
            compressed_path=tmp_path / "source-copy.gz",
            database_path=tmp_path / "candidate.db",
            expected_compressed_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            expected_database_sha256=hashlib.sha256(payload).hexdigest(),
            max_uncompressed_bytes=4,
        )
