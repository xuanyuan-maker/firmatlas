"""HTTP 客户端超时与重试配置测试。"""

import asyncio
import ssl

import httpx
import pytest

from firmatlas.infra import http_client
from firmatlas.infra.http_client import HttpFetcher, make_http_client


@pytest.fixture(autouse=True)
def no_proxy_env(monkeypatch):
    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.anyio
async def test_make_http_client_uses_effective_timeouts():
    client = make_http_client(request_timeout=45, connect_timeout=12.5)
    try:
        assert client.timeout.read == 45
        assert client.timeout.write == 45
        assert client.timeout.pool == 45
        assert client.timeout.connect == 12.5
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_make_http_client_accepts_socks_proxy_from_environment(monkeypatch):
    """安装 HTTPX SOCKS extra 后，ALL_PROXY 不应导致客户端初始化失败。"""
    monkeypatch.setenv("all_proxy", "socks5://127.0.0.1:10808")

    client = make_http_client()

    await client.aclose()


def test_make_http_client_legacy_tls_keeps_certificate_verification(monkeypatch):
    captured = {}

    class FakeContext:
        def set_ciphers(self, ciphers: str) -> None:
            captured["ciphers"] = ciphers

    context = FakeContext()
    monkeypatch.setattr(ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(
        http_client.httpx,
        "AsyncClient",
        lambda **options: captured.update(options) or object(),
    )

    make_http_client(legacy_tls=True)

    assert captured["ciphers"] == "DEFAULT:@SECLEVEL=1"
    assert captured["verify"] is context


@pytest.mark.anyio
async def test_http_fetcher_uses_effective_retry_settings():
    attempts = 0

    async def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="retry", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpFetcher(client, max_retries=1, retry_backoff_base=0)
        result = await fetcher.get_json("https://example.com/data.json")

    assert attempts == 2
    assert result.data == {"ok": True}


@pytest.mark.anyio
async def test_concurrent_requests_are_throttled_but_can_remain_in_flight():
    request_interval = 0.03
    request_started_at: list[float] = []
    in_flight = 0
    max_in_flight = 0

    async def handler(request):
        nonlocal in_flight, max_in_flight
        request_started_at.append(asyncio.get_running_loop().time())
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.09)
        in_flight -= 1
        return httpx.Response(200, json={"ok": True}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpFetcher(client, max_retries=0, request_interval=request_interval)
        await asyncio.gather(
            *(fetcher.get_json(f"https://example.com/{index}") for index in range(4))
        )

    intervals = [
        current - previous
        for previous, current in zip(request_started_at, request_started_at[1:], strict=False)
    ]
    assert all(interval >= request_interval * 0.8 for interval in intervals)
    assert max_in_flight > 1
