"""hikvision-global 摄像机发现适配器测试，所有网络响应来自固定 fixture。"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from firmatlas.adapters.events import (
    DiscoveredProduct,
    DiscoveryCompleted,
    SkippedCandidate,
    SkipReason,
)
from firmatlas.adapters.hikvision_global.adapter import HikvisionGlobalAdapter
from firmatlas.domain.candidates import UNSPECIFIED_REVISION_SOURCE_KEY
from firmatlas.domain.model import ProductFamily, ProductType
from firmatlas.infra.http_client import FetchedText

FIXTURE = Path(__file__).parent / "fixtures" / "hikvision-global" / "firmware_camera_samples.html"
INDEX_URL = "https://www.hikvision.com/en/support/download/firmware/"


@dataclass
class _MockHttpFetcher:
    text: str
    final_url: str = INDEX_URL
    error: Exception | None = None

    async def get_text(self, url: str, *, headers=None) -> FetchedText:
        assert url == INDEX_URL
        if self.error:
            raise self.error
        return FetchedText(url=self.final_url, status_code=200, text=self.text)


async def _discover(html: str | None = None):
    text = html if html is not None else FIXTURE.read_text(encoding="utf-8")
    return [event async for event in HikvisionGlobalAdapter(_MockHttpFetcher(text)).discover()]


def _products(events):
    return [event.product for event in events if isinstance(event, DiscoveredProduct)]


@pytest.mark.anyio
async def test_discover_camera_models_and_complete() -> None:
    events = await _discover()
    products = _products(events)
    completed = events[-1]

    assert isinstance(completed, DiscoveryCompleted)
    assert completed.is_complete is True
    assert len(products) == 5
    assert {product.model_raw for product in products} == {
        "DS-2CD1043G3-LIU(2.8mm)",
        "DS-2CD1043G3-LIU(2.8mm)(BLACK)",
        "DS-2CD1043G3-LIU(4mm)",
        "DS-2DF8425IX-AELW(T3)",
        "DS-2TD2136T-10",
    }
    assert all(product.product_family is ProductFamily.CAMERA for product in products)
    assert all(product.product_type is ProductType.CAMERA for product in products)


@pytest.mark.anyio
async def test_non_camera_category_is_aggregated_skip() -> None:
    events = await _discover()
    skipped = [event for event in events if isinstance(event, SkippedCandidate)]

    assert len(skipped) == 1
    assert skipped[0].reason_code is SkipReason.UNMAPPED_TYPE
    assert skipped[0].raw_hint == "IP-Products/Network-Video-Recorders"
    assert "跳过 1 个目录项" in skipped[0].detail


@pytest.mark.anyio
async def test_regional_artifacts_share_one_release() -> None:
    product = next(
        product
        for product in _products(await _discover())
        if product.model_raw == "DS-2DF8425IX-AELW(T3)"
    )
    revision = product.hardware_revisions[0]
    release = revision.releases[0]

    assert revision.source_key == UNSPECIFIED_REVISION_SOURCE_KEY
    assert revision.revision_explicit is False
    assert len(revision.releases) == 1
    assert release.version_normalized == "V4.30.122_201107"
    assert {artifact.source_key for artifact in release.artifacts} == {
        "1013a769-1107-4061-b83d-3093cc234323",
        "6f9a0cd6-1dd9-4da5-92a8-d34ddf652818",
    }
    assert release.release_notes_url is not None
    assert release.release_notes_url.endswith("Camera_V4.30.122_201107.pdf")


@pytest.mark.anyio
async def test_different_versions_become_separate_releases() -> None:
    product = next(
        product for product in _products(await _discover()) if product.model_raw == "DS-2TD2136T-10"
    )

    assert [release.version_normalized for release in product.hardware_revisions[0].releases] == [
        "V4.2.7_180418",
        "V5.5.8_210702",
    ]


@pytest.mark.anyio
async def test_source_key_contract_is_stable() -> None:
    first = next(
        product
        for product in _products(await _discover())
        if product.model_raw == "DS-2CD1043G3-LIU(2.8mm)"
    )
    second = next(
        product for product in _products(await _discover()) if product.model_raw == first.model_raw
    )
    release = first.hardware_revisions[0].releases[0]
    artifact = release.artifacts[0]

    assert first.source_key == second.source_key
    assert first.source_key == "2bcbebd8fa435660e52ef6ad398bdfca8a427d3eef382660b64647d2dd405b9e"
    assert release.source_key == "fw/v5.9.15_260508"
    assert artifact.source_key == "S3000721729"
    assert artifact.original_filename == "Firmware__V5.9.15_260508_S3000721729.zip"


@pytest.mark.anyio
async def test_missing_asset_url_makes_discovery_incomplete() -> None:
    html = """
    <div class="nav-item" data-main-tag="IP-Products" data-sub-tag="Network-Cameras">
      <div class="main-title">
        <a class="link" href="/en/products/IP-Products/Network-Cameras/camera-1/">Camera 1</a>
      </div>
      <div class="main-item">
        <div class="firmware-section">
          <a class="assets" data-title="Firmware_V1.0.0_250101"
             href="#download-agreement">Firmware</a>
        </div>
        <ul class="sub-list"><li class="sub-item">CAMERA-1</li></ul>
      </div>
    </div>
    """
    events = await _discover(html)
    completed = events[-1]

    assert isinstance(completed, DiscoveryCompleted)
    assert completed.is_complete is False
    assert _products(events) == []
    assert any(
        isinstance(event, SkippedCandidate) and event.reason_code is SkipReason.PARSE_FAILED
        for event in events
    )


@pytest.mark.anyio
async def test_empty_page_is_incomplete() -> None:
    events = await _discover("<html><body>No firmware list</body></html>")

    assert events == [
        DiscoveryCompleted(
            is_complete=False,
            incomplete_reason="国际站固件目录未解析到产品",
            issues=(),
        )
    ]


@pytest.mark.anyio
async def test_fetch_failure_is_incomplete() -> None:
    adapter = HikvisionGlobalAdapter(
        _MockHttpFetcher(text="", error=ConnectionError("simulated failure"))
    )
    events = [event async for event in adapter.discover()]

    assert len(events) == 1
    assert isinstance(events[0], DiscoveryCompleted)
    assert events[0].is_complete is False
    assert "请求失败" in (events[0].incomplete_reason or "")


@pytest.mark.anyio
async def test_redirect_outside_global_source_is_incomplete() -> None:
    adapter = HikvisionGlobalAdapter(
        _MockHttpFetcher(
            text=FIXTURE.read_text(encoding="utf-8"),
            final_url="https://www.hikvision.com/uk/support/download/firmware/",
        )
    )
    events = [event async for event in adapter.discover()]

    assert len(events) == 1
    assert isinstance(events[0], DiscoveryCompleted)
    assert events[0].is_complete is False
    assert "来源外" in (events[0].incomplete_reason or "")
