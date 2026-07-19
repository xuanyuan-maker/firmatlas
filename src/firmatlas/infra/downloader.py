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
_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, read=60.0, connect=10.0)
# on_progress 回调的最小间隔（字节），避免过于频繁的磁盘写入
_PROGRESS_THRESHOLD_BYTES = 256 * 1024  # 256 KiB


class Downloader:
    """流式下载器：HTTPX 流式 GET → 临时文件 + 边下边算 SHA-256。

    用法：实例化时传入 AsyncClient（复用长连接），调用方负责 client 生命周期。
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def download(
        self,
        *,
        url: str,
        dest: Path,
        expected_size: int | None = None,
        on_progress: Callable[[int], None] | None = None,
        referer: str | None = None,
        size_tolerance: int = 0,
    ) -> DownloadOutcome:
        """流式下载到 dest（必须是 data/tmp/downloads/ 下的临时路径）。

        SHA-256 在接收过程中累计算，不需要下载完成后重读文件。
        on_progress 在接收过程中按 ~256 KiB 节流回调（累计字节数）。
        referer 非空时随请求发送 Referer 头（部分厂商下载服务器
        校验 Referer，缺失即 403，如 service.tp-link.com.cn）。
        size_tolerance 是 expected_size 的允许偏差（字节）：实际大小与
        预期相差不超过它即视为通过。默认 0（精确比对）；当来源大小为
        KB 粒度近似值（如 tp-link-cn docSize）时由调用方放宽到 1024。

        调用方负责：
        - 确保 dest 的父目录存在
        - 下载成功后将文件从 tmp 移动到最终路径（ArtifactStore.promote）
        - 下载失败时清理临时文件
        """
        sha256 = hashlib.sha256()
        bytes_received = 0
        last_notified = 0

        headers = {
            "User-Agent": "FirmAtlas/0.1",
        }
        if referer is not None:
            headers["Referer"] = referer

        try:
            async with self._client.stream(
                "GET", url, headers=headers, timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True,
            ) as response:
                # 响应级错误：直接返回 DownloadFailed
                if response.status_code >= 400:
                    return _http_error(response.status_code, bytes_received)

                # 打开目标文件准备写入
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        f.write(chunk)
                        sha256.update(chunk)
                        bytes_received += len(chunk)

                        if on_progress is not None and (bytes_received - last_notified) >= (
                            _PROGRESS_THRESHOLD_BYTES
                        ):
                            on_progress(bytes_received)
                            last_notified = bytes_received

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

        # 大小校验（expected_size 不为 None 时才比较；size_tolerance 允许偏差）
        if expected_size is not None and abs(bytes_received - expected_size) > size_tolerance:
            return DownloadFailed(
                error_code=DownloadErrorCode.SIZE_MISMATCH,
                http_status=None,
                detail=f"大小不符：预期 {expected_size} B，实际 {bytes_received} B",
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
