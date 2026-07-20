"""HTTP 客户端超时与重试配置测试。"""

import httpx
import pytest

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
