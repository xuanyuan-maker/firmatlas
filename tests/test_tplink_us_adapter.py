"""tp-link-us 适配器测试（基于 fixture，AC-31）。

用回放 fixture 的 mock HttpFetcher，不访问真实网站。
覆盖：目标类识别、多/单硬件版本、无下载链接跳过、站外重定向排除、
source_key 契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from firmatlas.adapters.events import (
    DiscoveredProduct,
    DiscoveryCompleted,
    SkippedCandidate,
)
from firmatlas.adapters.tplink_us.adapter import TplinkUsAdapter
from firmatlas.domain.model import ProductFamily, ProductType
from firmatlas.infra.http_client import FetchedText

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tp-link-us"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# -- Mock HttpFetcher -------------------------------------------------------


@dataclass
class _Route:
    """一条 URL → 响应的路由。text 为 None 表示抛异常（模拟站外重定向失败）。"""

    text: str | None
    final_url: str | None = None      # 重定向后的最终 URL（默认等于请求 URL）
    raise_error: bool = False


class _MockHttpFetcher:
    """按 URL 回放 fixture 的虚拟 HttpFetcher。"""

    def __init__(self, routes: dict[str, _Route]) -> None:
        self._routes = routes
        self.calls: list[str] = []

    async def get_text(self, url: str, *, headers=None) -> FetchedText:
        self.calls.append(url)
        route = self._routes.get(url)
        if route is None:
            # 未注册的 URL：返回空页（无固件表）
            return FetchedText(url=url, status_code=200, text="<html></html>")
        if route.raise_error:
            raise ConnectionError(f"simulated fetch failure for {url}")
        return FetchedText(
            url=route.final_url or url,
            status_code=200,
            text=route.text or "",
        )


# -- 路由表 fixture ----------------------------------------------------------

_INDEX = "https://www.tp-link.com/us/support/download/"


def _build_routes() -> dict[str, _Route]:
    """构造与 index.html 内 productTree 一致的路由表。"""
    base = "https://www.tp-link.com/us/support/download/"
    return {
        _INDEX: _Route(text=_load("index.html")),
        # 单硬件版本路由器：固件在主页
        f"{base}archer-be670/": _Route(text=_load("download_archer-be670.html")),
        # 多硬件版本 mesh：主页列 version-list，子页有固件
        f"{base}deco-x55/": _Route(text=_load("download_deco-x55.html")),
        f"{base}deco-x55/v3/": _Route(text=_load("download_deco-x55_v3.html")),
        # deco-x55 的 v2/v1 子页返回无固件页（测试空子页容忍）
        f"{base}deco-x55/v2/": _Route(text="<html></html>"),
        f"{base}deco-x55/v1/": _Route(text="<html></html>"),
        # 摄像头：有固件条目但无下载链接
        f"{base}tapo-c100/": _Route(text=_load("download_tapo-c100.html")),
        # AP / 蜂窝路由：无固件页（简化，测试不产出但不报错）
        f"{base}ac500/": _Route(text="<html></html>"),
        f"{base}tl-mr3220/": _Route(text="<html></html>"),
        # Omada 商用摄像头：模拟重定向到独立站（抓取抛异常）
        f"{base}omada-8mp-bullet/": _Route(text=None, raise_error=True),
    }


async def _run_discover(routes=None):
    adapter = TplinkUsAdapter(_MockHttpFetcher(routes or _build_routes()))
    return [e async for e in adapter.discover()]


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_discover_completes() -> None:
    """discover 以 DiscoveryCompleted(is_complete=True) 结束。"""
    events = await _run_discover()
    completed = [e for e in events if isinstance(e, DiscoveryCompleted)]
    assert len(completed) == 1
    assert completed[0].is_complete is True
    # 完成事件必须是最后一个
    assert isinstance(events[-1], DiscoveryCompleted)


@pytest.mark.anyio
async def test_discover_target_products() -> None:
    """目标类型号产出正确的产品树（router / mesh / camera）。"""
    events = await _run_discover()
    products = {
        p.product.source_key: p.product
        for p in events
        if isinstance(p, DiscoveredProduct)
    }

    # Archer BE670（单硬件版本路由器）
    assert "archer-be670" in products
    be670 = products["archer-be670"]
    assert be670.product_family is ProductFamily.ROUTER
    assert be670.product_type is ProductType.ROUTER
    assert be670.source_category == "WiFi Routers"
    assert len(be670.hardware_revisions) == 1
    hw = be670.hardware_revisions[0]
    assert hw.releases[0].artifacts[0].download_url.startswith(
        "https://static.tp-link.com/upload/firmware/"
    )

    # Deco X55（多硬件版本 mesh）
    assert "deco-x55" in products
    deco = products["deco-x55"]
    assert deco.product_type is ProductType.MESH_ROUTER
    # 只有 v3 有固件，v2/v1 空 → 仅 1 个硬件版本入树
    assert len(deco.hardware_revisions) == 1
    assert deco.hardware_revisions[0].normalized_revision == "3"


@pytest.mark.anyio
async def test_non_target_excluded() -> None:
    """非目标型号（交换机）不产出产品。"""
    events = await _run_discover()
    products = [p.product.model_raw for p in events if isinstance(p, DiscoveredProduct)]
    assert "TL-SG1210P" not in products


@pytest.mark.anyio
async def test_camera_without_download_skipped() -> None:
    """摄像头有固件条目但无下载链接 → SkippedCandidate，不产出产品。"""
    events = await _run_discover()
    products = [p.product.source_key for p in events if isinstance(p, DiscoveredProduct)]
    assert "tapo-c100" not in products
    skipped = [e for e in events if isinstance(e, SkippedCandidate)]
    assert any(s.raw_hint == "tapo-c100" for s in skipped)


@pytest.mark.anyio
async def test_offsite_redirect_excluded() -> None:
    """Omada 商用摄像头抓取失败（模拟站外重定向）→ 记 issue，不产出产品。"""
    events = await _run_discover()
    products = [p.product.source_key for p in events if isinstance(p, DiscoveredProduct)]
    assert "omada-8mp-bullet" not in products
    completed = next(e for e in events if isinstance(e, DiscoveryCompleted))
    codes = {i.code for i in completed.issues}
    assert "model_fetch_failed" in codes


@pytest.mark.anyio
async def test_source_key_contract() -> None:
    """source_key 生成契约：稳定 URL 路径派生，可复现。"""
    events = await _run_discover()
    be670 = next(
        p.product for p in events
        if isinstance(p, DiscoveredProduct) and p.product.source_key == "archer-be670"
    )
    hw = be670.hardware_revisions[0]
    # 硬件版本 source_key = {slug}/v{hw}
    assert hw.source_key == "archer-be670/v1.6"
    rel = hw.releases[0]
    # 发布 source_key = {slug}/v{hw}/fw{version}
    assert rel.source_key.startswith("archer-be670/v1.6/fw")
    art = rel.artifacts[0]
    # Artifact source_key 含 slug + 硬件版本 + 文件名
    assert art.source_key.startswith("archer-be670/v1.6/")
    assert art.original_filename == "Archer BE670(US)_V1.6_20251203.zip"


@pytest.mark.anyio
async def test_size_parsed_to_bytes() -> None:
    """文件大小文本 "18.66 MB" 解析为近似字节数。"""
    events = await _run_discover()
    be670 = next(
        p.product for p in events
        if isinstance(p, DiscoveredProduct) and p.product.source_key == "archer-be670"
    )
    art = be670.hardware_revisions[0].releases[0].artifacts[0]
    # 18.66 MB = 18.66 * 1024^2 ≈ 19565117
    assert art.advertised_size == int(18.66 * 1024**2)
