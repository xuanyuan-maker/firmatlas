"""Downloader 与 ArtifactStore 测试（接口设计 §8）。

Downloader 测试用临时本地 HTTP 服务器（不访问真实网站）。
ArtifactStore 测试为纯逻辑（路径构造、安全规范化、原子移动）。
"""

from __future__ import annotations

import gzip
import hashlib
import threading
from dataclasses import replace
from datetime import UTC
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path, PurePosixPath

import httpx
import pytest

from firmatlas.domain.model import (
    ArtifactContext,
    ArtifactType,
    DownloadErrorCode,
    DownloadFailed,
    DownloadSucceeded,
    FirmwareArtifact,
    FirmwareRelease,
    FirmwareSource,
    HardwareRevision,
    Product,
    ProductFamily,
    ProductType,
    VisibilityStatus,
)
from firmatlas.infra.artifact_store import ArtifactStore
from firmatlas.infra.downloader import Downloader

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """清除代理环境变量，测试不发真实请求。"""
    for v in (
        "all_proxy",
        "ALL_PROXY",
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "no_proxy",
        "NO_PROXY",
    ):
        monkeypatch.delenv(v, raising=False)


# ---------------------------------------------------------------------------
# ArtifactStore
# ---------------------------------------------------------------------------


def make_artifact_context() -> ArtifactContext:
    from datetime import datetime

    from firmatlas.domain.ids import new_id
    from firmatlas.domain.model import DiscoveryMethod

    now = datetime(2026, 7, 17, tzinfo=UTC)
    source = FirmwareSource(
        id=new_id(),
        vendor_key="tp-link",
        vendor_name="TP-Link",
        source_key="tp-link-cn",
        name="TP-Link CN",
        region_code="CN",
        locale="zh-CN",
        base_url="https://resource.tp-link.com.cn/",
        adapter_key="tplink_cn",
        discovery_method=DiscoveryMethod.API,
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    product = Product(
        id=new_id(),
        source_id=source.id,
        source_key="sar305g",
        display_name="SAR305G",
        model_raw="SAR305G",
        model_normalized="sar305g",
        series=None,
        product_family=ProductFamily.ROUTER,
        product_type=ProductType.ROUTER,
        source_category="企业VPN路由器",
        source_url="https://resource.tp-link.com.cn/",
        first_seen_at=now,
        last_seen_at=now,
        last_seen_run_id="",
        created_at=now,
        updated_at=now,
    )
    hw = HardwareRevision(
        id=new_id(),
        product_id=product.id,
        source_key="v1",
        raw_revision="V1.0",
        normalized_revision="v1.0",
        revision_explicit=True,
        source_url=None,
        first_seen_at=now,
        last_seen_at=now,
        last_seen_run_id="",
        created_at=now,
        updated_at=now,
    )
    release = FirmwareRelease(
        id=new_id(),
        hardware_revision_id=hw.id,
        source_key="sar305g_v1.0_1.0.1",
        version_raw="1.0.1",
        version_normalized="1.0.1",
        release_date=None,
        title="SAR305G V1.0 升级软件",
        release_notes=None,
        release_notes_url=None,
        source_url="https://resource.tp-link.com.cn/",
        visibility_status=VisibilityStatus.ACTIVE,
        first_seen_at=now,
        last_seen_at=now,
        disappeared_at=None,
        last_seen_run_id="",
        created_at=now,
        updated_at=now,
    )
    artifact = FirmwareArtifact(
        id="1234567890abcdef1234567890abcdef",
        release_id=release.id,
        source_key="1657242724340094",
        artifact_type=ArtifactType.FIRMWARE,
        original_filename="SAR305G_V1_1.0.1.zip",
        download_url="https://media.tp-link.com.cn/software/SAR305G_V1_1.0.1.zip",
        url_last_resolved_at=now,
        url_expires_at=None,
        advertised_size=5_242_880,
        media_type="application/zip",
        official_checksum=None,
        visibility_status=VisibilityStatus.ACTIVE,
        first_seen_at=now,
        last_seen_at=now,
        disappeared_at=None,
        last_seen_run_id="",
        created_at=now,
        updated_at=now,
    )
    return ArtifactContext(
        source=source,
        product=product,
        hardware_revision=hw,
        release=release,
        artifact=artifact,
    )


def test_build_final_relative_path_basic(tmp_path):
    store = ArtifactStore(tmp_path / "data")
    ctx = make_artifact_context()

    path = store.build_final_relative_path(ctx, "SAR305G_V1_1.0.1.zip")

    assert isinstance(path, PurePosixPath)
    # 厂商/地区/型号/硬件版本/固件版本/前缀__文件
    assert path.parts[:2] == ("tp-link", "CN")
    assert path.parts[2] == "sar305g"
    assert path.parts[3] == "v1.0"
    assert path.parts[4] == "1.0.1"
    filename = path.parts[5]
    assert filename.startswith("12345678__")
    assert "SAR305G_V1" in filename


def test_build_path_without_original_filename(tmp_path):
    store = ArtifactStore(tmp_path / "data")
    ctx = make_artifact_context()

    path = store.build_final_relative_path(ctx, None)

    assert path.name == "12345678"


def test_build_path_sanitizes_dangerous_chars(tmp_path):
    store = ArtifactStore(tmp_path / "data")

    ctx = make_artifact_context()
    bad_product = replace(ctx.product, model_normalized="../etc/passwd")
    bad_ctx = replace(ctx, product=bad_product)

    path = store.build_final_relative_path(bad_ctx, "../../bin/sh")
    for part in path.parts:
        assert ".." not in part
        assert "/" not in part


def test_build_path_truncates_long_segments(tmp_path):
    store = ArtifactStore(tmp_path / "data")
    ctx = make_artifact_context()

    long_name = "A" * 200
    path = store.build_final_relative_path(ctx, f"{long_name}.bin")

    for part in path.parts:
        assert len(part.encode("utf-8")) <= 128


def test_promote_moves_file_atomically(tmp_path):
    data_dir = tmp_path / "data"
    store = ArtifactStore(data_dir)
    ctx = make_artifact_context()

    tmp_src = data_dir / "tmp" / "downloads" / "test.bin"
    tmp_src.parent.mkdir(parents=True, exist_ok=True)
    tmp_src.write_bytes(b"hello firmware")

    rel = store.build_final_relative_path(ctx, "test.bin")
    dest = store.promote(tmp_path=tmp_src, final_relative_path=rel)

    assert dest.exists()
    assert dest.read_bytes() == b"hello firmware"
    assert not tmp_src.exists()  # 源已移走


# ---------------------------------------------------------------------------
# Downloader（用本地 HTTP 服务器）
# ---------------------------------------------------------------------------


def _serve_file(
    tmp_path: Path,
    content: bytes,
    status: int = 200,
    *,
    content_length: int | str | None = None,
    include_content_length: bool = True,
) -> tuple[int, dict]:
    """在随机端口启动一个临时 HTTP 服务器（线程），返回 (端口号, 请求头记录)。

    服务器只处理一个请求然后关闭；收到的请求头写入返回的 dict。
    """
    file_path = tmp_path / "test_firmware.bin"
    file_path.write_bytes(content)

    ready = threading.Event()
    result: dict = {"port": 0}
    seen_headers: dict = {}

    class _Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            seen_headers.update(dict(self.headers))
            body = file_path.read_bytes()
            self.send_response(status)
            if include_content_length:
                length = len(body) if content_length is None else content_length
                self.send_header("Content-Length", str(length))
            self.send_header("ETag", '"abc123"')
            self.send_header("Last-Modified", "Wed, 21 Oct 2015 07:28:00 GMT")
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass  # 静默

    def _run():
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        result["port"] = server.server_address[1]
        ready.set()
        server.handle_request()  # 处理一个请求后自动退出

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    ready.wait(timeout=2)
    return result["port"], seen_headers


class _AsyncBytesStream(httpx.AsyncByteStream):
    """让 MockTransport 提供真正的异步原始响应流。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aiter__(self):
        yield self._body


def _mock_stream_response(
    request: httpx.Request,
    body: bytes,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers=headers,
        stream=_AsyncBytesStream(body),
        request=request,
    )


@pytest.mark.anyio
async def test_download_success(tmp_path):
    content = b"x" * (256 * 1024 + 100)  # 略大于进度阈值，触发一次 on_progress
    port, seen_headers = _serve_file(tmp_path, content)
    expected_sha = hashlib.sha256(content).hexdigest()

    async with httpx.AsyncClient() as client:
        downloader = Downloader(client)
        progress_values: list[int] = []

        outcome = await downloader.download(
            url=f"http://127.0.0.1:{port}/test_firmware.bin",
            dest=tmp_path / "downloads" / "test_result.bin",
            on_progress=lambda n: progress_values.append(n),
            referer="https://resource.example.com/",
        )

    assert isinstance(outcome, DownloadSucceeded)
    assert outcome.bytes_received == len(content)
    assert outcome.sha256 == expected_sha
    assert outcome.etag == '"abc123"'
    assert outcome.last_modified == "Wed, 21 Oct 2015 07:28:00 GMT"
    assert len(progress_values) >= 1
    assert (tmp_path / "downloads" / "test_result.bin").read_bytes() == content
    # Referer 头随请求发出（部分厂商下载服务器校验 Referer）
    assert seen_headers.get("Referer") == "https://resource.example.com/"


@pytest.mark.anyio
async def test_download_without_referer_sends_no_header(tmp_path):
    """未传 referer 时不发送 Referer 头。"""
    port, seen_headers = _serve_file(tmp_path, b"data")

    async with httpx.AsyncClient() as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url=f"http://127.0.0.1:{port}/test_firmware.bin",
            dest=tmp_path / "downloads" / "noref.bin",
        )

    assert isinstance(outcome, DownloadSucceeded)
    assert "Referer" not in seen_headers


@pytest.mark.anyio
async def test_downloader_uses_effective_timeout_config(tmp_path):
    seen_timeout = {}

    async def handler(request):
        seen_timeout.update(request.extensions["timeout"])
        return _mock_stream_response(request, b"configured")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = Downloader(client, read_timeout=17, connect_timeout=4)
        outcome = await downloader.download(
            url="https://example.com/firmware.bin",
            dest=tmp_path / "downloads" / "configured.bin",
        )

    assert isinstance(outcome, DownloadSucceeded)
    assert seen_timeout["read"] == 17
    assert seen_timeout["connect"] == 4


@pytest.mark.anyio
async def test_download_http_404(tmp_path):
    port, _ = _serve_file(tmp_path, b"error", status=404)

    async with httpx.AsyncClient() as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url=f"http://127.0.0.1:{port}/test_firmware.bin",
            dest=tmp_path / "downloads" / "nope.bin",
        )

    assert isinstance(outcome, DownloadFailed)
    assert outcome.error_code is DownloadErrorCode.HTTP_404
    assert outcome.http_status == 404


@pytest.mark.anyio
async def test_download_http_403(tmp_path):
    port, _ = _serve_file(tmp_path, b"forbidden", status=403)

    async with httpx.AsyncClient() as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url=f"http://127.0.0.1:{port}/test_firmware.bin",
            dest=tmp_path / "downloads" / "forbidden.bin",
        )

    assert isinstance(outcome, DownloadFailed)
    assert outcome.error_code is DownloadErrorCode.HTTP_403


@pytest.mark.anyio
async def test_download_size_mismatch(tmp_path):
    content = b"small"

    def handler(request: httpx.Request) -> httpx.Response:
        return _mock_stream_response(
            request,
            content,
            headers={"Content-Length": "99999"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url="https://example.com/test_firmware.bin",
            dest=tmp_path / "downloads" / "result.bin",
        )

    assert isinstance(outcome, DownloadFailed)
    assert outcome.error_code is DownloadErrorCode.SIZE_MISMATCH


@pytest.mark.anyio
async def test_download_http_content_length_mismatch_is_stable_error(tmp_path):
    """HTTPX 在响应提前结束时抛协议异常，也要返回稳定的大小不符错误。"""
    content = b"truncated firmware"
    port, _ = _serve_file(tmp_path, content, content_length=len(content) + 1)

    async with httpx.AsyncClient() as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url=f"http://127.0.0.1:{port}/test_firmware.bin",
            dest=tmp_path / "downloads" / "result.bin",
        )

    assert isinstance(outcome, DownloadFailed)
    assert outcome.error_code is DownloadErrorCode.SIZE_MISMATCH


@pytest.mark.anyio
async def test_download_without_content_length_is_allowed(tmp_path):
    """没有响应长度时允许正常完成，并记录实际接收字节数。"""
    content = b"x" * 5000
    port, _ = _serve_file(tmp_path, content, include_content_length=False)

    async with httpx.AsyncClient() as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url=f"http://127.0.0.1:{port}/test_firmware.bin",
            dest=tmp_path / "downloads" / "result.bin",
        )

    assert isinstance(outcome, DownloadSucceeded)
    assert outcome.bytes_received == 5000


@pytest.mark.anyio
async def test_download_invalid_content_length_is_allowed(tmp_path):
    content = b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        return _mock_stream_response(
            request,
            content,
            headers={"Content-Length": "not-a-number"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url="https://example.com/test_firmware.bin",
            dest=tmp_path / "downloads" / "result.bin",
        )

    assert isinstance(outcome, DownloadSucceeded)
    assert outcome.bytes_received == 5000


@pytest.mark.anyio
async def test_download_content_length_is_exact(tmp_path):
    content = b"x" * 18_192
    port, _ = _serve_file(tmp_path, content, content_length=len(content))

    async with httpx.AsyncClient() as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url=f"http://127.0.0.1:{port}/test_firmware.bin",
            dest=tmp_path / "downloads" / "result.bin",
        )

    assert isinstance(outcome, DownloadSucceeded)


@pytest.mark.anyio
async def test_download_content_length_shorter_than_body_fails(tmp_path):
    content = b"x" * 18_193

    def handler(request: httpx.Request) -> httpx.Response:
        return _mock_stream_response(
            request,
            content,
            headers={"Content-Length": str(len(content) - 1)},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url="https://example.com/test_firmware.bin",
            dest=tmp_path / "downloads" / "result.bin",
        )

    assert isinstance(outcome, DownloadFailed)
    assert outcome.error_code is DownloadErrorCode.SIZE_MISMATCH


@pytest.mark.anyio
async def test_download_content_length_longer_than_body_fails(tmp_path):
    content = b"x" * 1_001

    def handler(request: httpx.Request) -> httpx.Response:
        return _mock_stream_response(
            request,
            content,
            headers={"Content-Length": str(len(content) + 1)},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url="https://example.com/test_firmware.bin",
            dest=tmp_path / "downloads" / "result.bin",
        )

    assert isinstance(outcome, DownloadFailed)
    assert outcome.error_code is DownloadErrorCode.SIZE_MISMATCH


@pytest.mark.anyio
async def test_download_content_encoding_preserves_raw_bytes(tmp_path):
    """带 Content-Encoding 时保存和统计原始响应表示，而非解码后的内容。"""
    content = b"firmware raw representation"
    compressed = gzip.compress(content)

    def handler(request: httpx.Request) -> httpx.Response:
        return _mock_stream_response(
            request,
            compressed,
            headers={
                "Content-Length": str(len(compressed)),
                "Content-Encoding": "gzip",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = Downloader(client)
        dest = tmp_path / "downloads" / "result.bin"
        outcome = await downloader.download(url="https://example.com/fw.bin", dest=dest)

    assert isinstance(outcome, DownloadSucceeded)
    assert outcome.bytes_received == len(compressed)
    assert outcome.sha256 == hashlib.sha256(compressed).hexdigest()
    assert dest.read_bytes() == compressed


@pytest.mark.anyio
async def test_download_redirect_uses_final_content_length(tmp_path):
    content = b"redirected firmware"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return _mock_stream_response(
                request,
                b"",
                status_code=302,
                headers={"Location": "/final", "Content-Length": "99999"},
            )
        return _mock_stream_response(
            request,
            content,
            headers={"Content-Length": str(len(content))},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url="https://example.com/start",
            dest=tmp_path / "downloads" / "result.bin",
        )

    assert isinstance(outcome, DownloadSucceeded)
    assert outcome.bytes_received == len(content)


@pytest.mark.anyio
async def test_download_directory_size_does_not_affect_result(tmp_path):
    """下载器不接收 advertised_size，目录声明大小偏差不影响结果。"""
    content = b"x" * 5000
    port, _ = _serve_file(tmp_path, content)

    async with httpx.AsyncClient() as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url=f"http://127.0.0.1:{port}/test_firmware.bin",
            dest=tmp_path / "downloads" / "result.bin",
        )

    assert isinstance(outcome, DownloadSucceeded)


@pytest.mark.anyio
async def test_download_timeout_classified(tmp_path):
    """下载中 httpx 抛超时 → 归类为 TIMEOUT（而非兜底 INTERRUPTED）。

    用 httpx.MockTransport 注入一个直接抛 ReadTimeout 的处理器：
    ReadTimeout 是 httpx.TimeoutException 的子类，走的是 downloader
    里 `except httpx.TimeoutException` 那条分支，与真实读超时同一段代码。
    好处是瞬间完成、不真等 60 秒读超时、不依赖真实慢服务器。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated read timeout", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url="http://example.invalid/fw.bin",
            dest=tmp_path / "downloads" / "timeout.bin",
        )

    assert isinstance(outcome, DownloadFailed)
    assert outcome.error_code is DownloadErrorCode.TIMEOUT
    assert outcome.http_status is None
    # 未收到任何字节；也不应留下临时文件（下载分支未进入写文件）
    assert outcome.bytes_received == 0
    assert not (tmp_path / "downloads" / "timeout.bin").exists()


@pytest.mark.anyio
async def test_download_connection_refused():
    """连接一个不可达端口 → CONNECTION 错误。"""
    async with httpx.AsyncClient() as client:
        downloader = Downloader(client)
        outcome = await downloader.download(
            url="http://127.0.0.1:1/test",  # 端口 1 一般无人监听
            dest=Path("/tmp/nope.bin"),
        )

    assert isinstance(outcome, DownloadFailed)
    assert outcome.error_code in (DownloadErrorCode.CONNECTION, DownloadErrorCode.INTERRUPTED)
