"""固件下载器（接口设计 §8）。

流式下载到临时文件、边下边算 SHA-256、进度回调节流。
下载器只负责"把 URL 指向的远程文件原样搬到本地临时文件"，
不关心校验和比对或归档（那是下载用例的职责）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx

from firmatlas.domain.model import (
    DownloadErrorCode,
    DownloadFailed,
    DownloadOutcome,
    DownloadSucceeded,
)

# 固件下载空闲读取超时 60s，总超时不设（大文件可一直下）
_DEFAULT_READ_TIMEOUT = 60.0
_DEFAULT_CONNECT_TIMEOUT = 10.0
# on_progress 回调的最小间隔（字节），避免过于频繁的磁盘写入
_PROGRESS_THRESHOLD_BYTES = 256 * 1024  # 256 KiB


class Downloader:
    """流式下载器：HTTPX 流式 GET → 临时文件 + 边下边算 SHA-256。

    用法：实例化时传入 AsyncClient（复用长连接），调用方负责 client 生命周期。
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        read_timeout: float = _DEFAULT_READ_TIMEOUT,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self._client = client
        self._timeout = httpx.Timeout(
            read_timeout,
            read=read_timeout,
            connect=connect_timeout,
        )

    async def download(
        self,
        *,
        url: str,
        dest: Path,
        on_progress: Callable[[int], None] | None = None,
        referer: str | None = None,
    ) -> DownloadOutcome:
        """流式下载到 dest（必须是 download_dir/tmp/downloads/ 下的临时路径）。

        SHA-256 在接收过程中累计算，不需要下载完成后重读文件。
        on_progress 在接收过程中按 ~256 KiB 节流回调（累计字节数）。
        referer 非空时随请求发送 Referer 头（部分厂商下载服务器
        校验 Referer，缺失即 403，如 service.tp-link.com.cn）。

        如果最终 HTTP 响应提供合法的 Content-Length，实际接收的原始响应字节数
        必须与之完全一致；缺少或非法的 Content-Length 按未提供处理。目录中的
        advertised_size 不参与此处校验，官方 checksum 由下载用例负责。

        调用方负责：
        - 确保 dest 的父目录存在
        - 下载成功后将文件从 tmp 移动到最终路径（ArtifactStore.promote）
        - 下载失败时清理临时文件
        """
        sha256 = hashlib.sha256()
        bytes_received = 0
        last_notified = 0
        content_length: int | None = None
        response: httpx.Response | None = None

        headers = {
            "User-Agent": "FirmAtlas/0.1",
        }
        if referer is not None:
            headers["Referer"] = referer

        try:
            async with self._client.stream(
                "GET",
                url,
                headers=headers,
                timeout=self._timeout,
                follow_redirects=True,
            ) as response:
                content_length = _parse_content_length(response.headers.get("content-length"))
                # 响应级错误：直接返回 DownloadFailed
                if response.status_code >= 400:
                    return _http_error(response.status_code, bytes_received)

                # 打开目标文件准备写入
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as f:
                    # aiter_bytes() 会按 Content-Encoding 解码；下载器需要保存和统计
                    # HTTP 响应的原始字节表示，因此必须使用 aiter_raw()。
                    async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                        f.write(chunk)
                        sha256.update(chunk)
                        bytes_received += len(chunk)

                        if on_progress is not None and (bytes_received - last_notified) >= (
                            _PROGRESS_THRESHOLD_BYTES
                        ):
                            on_progress(bytes_received)
                            last_notified = bytes_received

        except httpx.RemoteProtocolError as exc:
            # HTTPX 在部分传输层会先检测到 Content-Length 与响应体不一致，
            # 再抛出协议错误，而不是让 aiter_raw() 正常结束。对调用方保持
            # 稳定的大小不符错误码。
            if content_length is not None:
                return DownloadFailed(
                    error_code=DownloadErrorCode.SIZE_MISMATCH,
                    http_status=None,
                    detail=f"响应 Content-Length 校验失败：{exc}",
                    bytes_received=bytes_received,
                )
            return DownloadFailed(
                error_code=DownloadErrorCode.INTERRUPTED,
                http_status=None,
                detail=f"下载中断：{exc}",
                bytes_received=bytes_received,
            )
        except httpx.TimeoutException:
            return DownloadFailed(
                error_code=DownloadErrorCode.TIMEOUT,
                http_status=None,
                detail=f"下载超时：{url}",
                bytes_received=bytes_received,
            )
        except httpx.ConnectError:
            return DownloadFailed(
                error_code=DownloadErrorCode.CONNECTION,
                http_status=None,
                detail=f"连接失败：{url}",
                bytes_received=bytes_received,
            )
        except Exception as exc:
            return DownloadFailed(
                error_code=DownloadErrorCode.INTERRUPTED,
                http_status=None,
                detail=f"下载中断：{exc}",
                bytes_received=bytes_received,
            )

        # 最后调用一次 on_progress（确保落库时有最终字节数）
        if on_progress is not None and bytes_received > last_notified:
            on_progress(bytes_received)

        assert response is not None
        if content_length is not None and bytes_received != content_length:
            return DownloadFailed(
                error_code=DownloadErrorCode.SIZE_MISMATCH,
                http_status=None,
                detail=(
                    f"响应 Content-Length 不符：声明 {content_length} B，实际 {bytes_received} B"
                ),
                bytes_received=bytes_received,
            )

        return DownloadSucceeded(
            bytes_received=bytes_received,
            sha256=sha256.hexdigest(),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )


def _http_error(status_code: int, bytes_received: int) -> DownloadFailed:
    if status_code == 403:
        code = DownloadErrorCode.HTTP_403
    elif status_code == 404:
        code = DownloadErrorCode.HTTP_404
    elif status_code == 410:
        code = DownloadErrorCode.HTTP_410
    elif 400 <= status_code < 500:
        code = DownloadErrorCode.HTTP_4XX
    else:
        code = DownloadErrorCode.HTTP_5XX
    return DownloadFailed(
        error_code=code,
        http_status=status_code,
        detail=f"HTTP {status_code}",
        bytes_received=bytes_received,
    )


def _parse_content_length(value: str | None) -> int | None:
    """解析响应 Content-Length；缺少、空值或非法值均视为未提供。"""
    if value is None or not value or any(char not in "0123456789" for char in value):
        return None
    return int(value)
