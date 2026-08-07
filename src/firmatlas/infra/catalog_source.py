"""Catalog manifest 的 file/http/https 来源读取。"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from firmatlas.app.catalog_manifest import CatalogManifest
from firmatlas.domain.errors import CatalogManifestError, CatalogSourceError

DEFAULT_MANIFEST_MAX_BYTES = 1024 * 1024


def fetch_manifest(
    manifest_url: str,
    *,
    allow_insecure_http: bool = False,
    max_bytes: int = DEFAULT_MANIFEST_MAX_BYTES,
    timeout: float = 30.0,
) -> CatalogManifest:
    """读取并严格解析 manifest；不把响应写入数据目录。"""
    _validate_source_url(manifest_url, allow_insecure_http)
    try:
        payload = _read_url(manifest_url, max_bytes=max_bytes, timeout=timeout)
    except CatalogSourceError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise CatalogSourceError(f"无法读取 Catalog manifest：{manifest_url}：{exc}") from exc
    try:
        return CatalogManifest.from_json(payload.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise CatalogSourceError("Catalog manifest 不是有效 UTF-8 文本。") from exc
    except CatalogManifestError as exc:
        raise CatalogSourceError(f"Catalog manifest 校验失败：{exc}") from exc


def resolve_database_url(manifest_url: str, database_url: str) -> str:
    """将 manifest 中的数据库相对 URL 解析为最终 URL。"""
    _validate_source_url(manifest_url, allow_insecure_http=True)
    if not database_url or database_url.strip() != database_url:
        raise CatalogSourceError("manifest.database.url 必须是非空且无首尾空白的 URL。")
    resolved = urljoin(manifest_url, database_url)
    _validate_source_url(resolved, allow_insecure_http=True)
    return resolved


def read_local_manifest(data_dir: Path) -> CatalogManifest | None:
    path = data_dir / "catalog-manifest.json"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogSourceError(f"无法读取本地 Catalog manifest：{path}：{exc}") from exc
    try:
        return CatalogManifest.from_json(text)
    except CatalogManifestError as exc:
        raise CatalogSourceError(f"本地 Catalog manifest 校验失败：{exc}") from exc


def _read_url(url: str, *, max_bytes: int, timeout: float) -> bytes:
    if max_bytes <= 0:
        raise CatalogSourceError("manifest 最大读取大小必须大于 0。")
    try:
        with open_catalog_url(url, allow_insecure_http=True, timeout=timeout) as response:
            return _read_limited(response, max_bytes)
    except urllib.error.HTTPError as exc:
        raise CatalogSourceError(f"读取 Catalog manifest 返回 HTTP {exc.code}。") from exc
    except urllib.error.URLError as exc:
        raise CatalogSourceError(f"Catalog manifest 网络请求失败：{exc.reason}") from exc


@contextmanager
def open_catalog_url(url: str, *, allow_insecure_http: bool, timeout: float) -> Iterator:
    """打开受协议限制的 Catalog 文件或 HTTP 来源。"""
    _validate_source_url(url, allow_insecure_http)
    parsed = urlsplit(url)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        try:
            with path.open("rb") as handle:
                yield handle
        except OSError as exc:
            raise CatalogSourceError(f"无法读取本地 Catalog 文件 {path}：{exc}") from exc
        return

    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            yield response
    except urllib.error.HTTPError as exc:
        raise CatalogSourceError(f"读取 Catalog 来源返回 HTTP {exc.code}。") from exc
    except urllib.error.URLError as exc:
        raise CatalogSourceError(f"Catalog 来源网络请求失败：{exc.reason}") from exc


def _read_limited(handle, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := handle.read(min(64 * 1024, max_bytes - total + 1)):
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise CatalogSourceError(f"Catalog manifest 超过大小限制 {max_bytes} bytes。")
    return b"".join(chunks)


def _validate_source_url(url: str, allow_insecure_http: bool) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"file", "http", "https"}:
        raise CatalogSourceError("Catalog URL 只允许使用 file、http 或 https 协议。")
    if parsed.scheme in {"http", "https"} and not parsed.netloc:
        raise CatalogSourceError("Catalog HTTP URL 缺少主机名。")
    if parsed.scheme == "file" and parsed.netloc not in {"", "localhost"}:
        raise CatalogSourceError("file:// URL 不允许指定远程主机。")
    if parsed.scheme == "http" and not allow_insecure_http:
        raise CatalogSourceError("使用 http:// Catalog 来源必须显式允许不安全 HTTP。")
