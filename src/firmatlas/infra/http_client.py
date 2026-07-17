"""HTTP 访问组件（接口设计 §5）。

提供带超时/重试/退避策略的 httpx.AsyncClient 封装，注入适配器。
适配器只通过 HttpFetcher 发请求，不得自建 HTTP 客户端。

实现细节：
- 总超时 30s，连接超时 10s
- 5xx / 网络错误重试 3 次，指数退避（1s→2s→4s）
- 4xx 不重试
- 重试耗尽后抛 FetchError；适配器可选择捕获后降级为 SkippedCandidate
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from firmatlas.domain.errors import FirmAtlasError

# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchedText:
    url: str  # 最终 URL（重定向后）
    status_code: int
    text: str


@dataclass(frozen=True)
class FetchedJson:
    url: str
    status_code: int
    data: Any  # 已解析 JSON


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class FetchError(FirmAtlasError):
    """HTTP 请求在重试耗尽后仍失败的致命错误。

    适配器层可捕获 FetchError 并转为 SkippedCandidate（对单条记录），
    或让其向上传播（视为来源级故障 → 用例将 run 置 partial/failed）。
    """

    def __init__(self, url: str, status_code: int | None, detail: str) -> None:
        self.url = url
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{url}: [{status_code or 'N/A'}] {detail}")


# ---------------------------------------------------------------------------
# HttpFetcher 实现
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0  # 秒：1, 2, 4


class HttpFetcher:
    """HTTP 访问组件，封装 httpx.AsyncClient。

    应用层在启动时创建长期 AsyncClient 并注入适配器。
    同一 client 在整个采集任务中复用（Keep-Alive）。
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    # -- POST JSON（tp-link-cn search API 的核心调用） ------------------

    async def post_json(
        self,
        url: str,
        body: Any,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FetchedJson:
        """发送 JSON POST 请求，返回已解析的 JSON 响应体。"""
        merged_headers: dict[str, str] = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
        }
        if headers:
            merged_headers.update(headers)

        return await self._retry(
            lambda: self._client.post(url, json=body, headers=merged_headers),
            url,
        )

    # -- GET -----------------------------------------------------------

    async def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FetchedText:
        """发送 GET 请求，返回文本响应体。"""
        return await self._retry(
            lambda: self._client.get(url, headers=dict(headers) if headers else None),
            url,
            as_json=False,
        )

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FetchedJson:
        """发送 GET 请求，返回已解析的 JSON 响应体。"""
        return await self._retry(
            lambda: self._client.get(url, headers=dict(headers) if headers else None),
            url,
        )

    # -- 重试逻辑 -------------------------------------------------------

    async def _retry(
        self,
        factory: Any,
        url: str,
        as_json: bool = True,
    ) -> Any:
        last_error: FetchError | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await factory()
            except httpx.TimeoutException:
                last_error = FetchError(url, None, "timeout")
            except Exception as exc:
                last_error = FetchError(url, None, f"connection error: {exc}")
            else:
                if response.status_code < 400:
                    # 成功
                    if as_json:
                        return FetchedJson(
                            url=str(response.url),
                            status_code=response.status_code,
                            data=response.json(),
                        )
                    else:
                        return FetchedText(
                            url=str(response.url),
                            status_code=response.status_code,
                            text=response.text,
                        )

                if 400 <= response.status_code < 500:
                    # 4xx 不重试
                    raise FetchError(
                        url, response.status_code,
                        f"client error: {response.text[:200]}",
                    )

                # 5xx 可重试
                last_error = FetchError(
                    url, response.status_code,
                    f"server error: {response.text[:200]}",
                )

            if attempt < _MAX_RETRIES:
                delay = _RETRY_BACKOFF_BASE * (2 ** attempt)
                await asyncio.sleep(delay)

        # 重试耗尽
        assert last_error is not None
        raise last_error


def make_http_client() -> httpx.AsyncClient:
    """创建带有项目默认超时策略的 AsyncClient。

    调用方负责生命周期管理（async with client: ...）。
    """
    return httpx.AsyncClient(
        timeout=_DEFAULT_TIMEOUT,
        headers={"User-Agent": "FirmAtlas/0.1"},
        follow_redirects=True,
    )
