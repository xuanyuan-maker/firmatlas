"""Catalog 来源读取与相对 URL 测试。"""

from datetime import UTC, datetime

import pytest

from firmatlas.app.catalog_manifest import CatalogCounts, CatalogDatabase, CatalogManifest
from firmatlas.domain.errors import CatalogSourceError
from firmatlas.infra import catalog_source
from firmatlas.infra.catalog_source import fetch_manifest, read_local_manifest, resolve_database_url


def manifest_json() -> str:
    return CatalogManifest(
        format_version=1,
        lineage_id="11111111-1111-4111-8111-111111111111",
        catalog_version="2026.08.07.1",
        created_at=datetime(2026, 8, 7, tzinfo=UTC),
        schema_version=1,
        minimum_firmatlas_version="1.0.0",
        database=CatalogDatabase(
            url="firmatlas.db.gz",
            compression="gzip",
            compressed_size=1,
            uncompressed_size=2,
            compressed_sha256="a" * 64,
            database_sha256="b" * 64,
        ),
        counts=CatalogCounts(sources=0, products=0, releases=0, artifacts=0, downloads=0),
        sources=(),
    ).to_json()


def test_fetch_manifest_from_file_url(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(manifest_json(), encoding="utf-8")

    manifest = fetch_manifest(path.as_uri())

    assert manifest.catalog_version == "2026.08.07.1"


@pytest.mark.parametrize(
    "url", ["http://catalog.example/manifest.json", "https://catalog.example/manifest.json"]
)
def test_fetch_manifest_from_http_sources_when_allowed(monkeypatch, url):
    class FakeResponse:
        def __init__(self):
            self._payload = manifest_json().encode("utf-8")
            self._offset = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

        def read(self, size=-1):
            if self._offset >= len(self._payload):
                return b""
            chunk = self._payload[self._offset : self._offset + size]
            self._offset += len(chunk)
            return chunk

    monkeypatch.setattr(
        catalog_source.urllib.request, "urlopen", lambda request, timeout: FakeResponse()
    )

    manifest = fetch_manifest(url, allow_insecure_http=True)

    assert manifest.catalog_version == "2026.08.07.1"


def test_read_local_manifest(tmp_path):
    (tmp_path / "catalog-manifest.json").write_text(manifest_json(), encoding="utf-8")

    manifest = read_local_manifest(tmp_path)

    assert manifest is not None
    assert manifest.lineage_id == "11111111-1111-4111-8111-111111111111"


def test_read_local_manifest_returns_none_when_missing(tmp_path):
    assert read_local_manifest(tmp_path) is None


def test_resolve_database_url_from_file_manifest(tmp_path):
    manifest_url = (tmp_path / "manifest.json").as_uri()

    assert (
        resolve_database_url(manifest_url, "firmatlas.db.gz")
        == (tmp_path / "firmatlas.db.gz").as_uri()
    )


def test_http_requires_explicit_allowance():
    with pytest.raises(CatalogSourceError, match="不安全 HTTP"):
        fetch_manifest("http://127.0.0.1/manifest.json")


def test_rejects_remote_file_host():
    with pytest.raises(CatalogSourceError, match="远程主机"):
        fetch_manifest("file://example.com/manifest.json")


def test_rejects_oversized_manifest(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(manifest_json(), encoding="utf-8")

    with pytest.raises(CatalogSourceError, match="大小限制"):
        fetch_manifest(path.as_uri(), max_bytes=10)


def test_rejects_manifest_with_invalid_schema(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"format_version": 99}', encoding="utf-8")

    with pytest.raises(CatalogSourceError, match="校验失败"):
        fetch_manifest(path.as_uri())
