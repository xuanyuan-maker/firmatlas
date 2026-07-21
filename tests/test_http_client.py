"""HTTP 客户端超时与重试配置测试。"""

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
