"""Tenda 全球站适配器测试，全部使用 fixture 数据。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from firmatlas.adapters.events import (
    DiscoveredProduct,
    DiscoveryCompleted,
    SkippedCandidate,
)
from firmatlas.adapters.tenda_global.adapter import (
    TendaGlobalAdapter,
    _deduplicate_firmware,
    _filename_from_url,
    _product_source_key,
    _revision_source_key,
)
from firmatlas.domain.candidates import (
    UNSPECIFIED_REVISION,
    UNSPECIFIED_REVISION_SOURCE_KEY,
)
from firmatlas.domain.model import ProductFamily, ProductType
from firmatlas.infra.http_client import FetchedJson

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tenda-global"

_SITE_ID = 206917
_TREE_URL = f"https://www.tendacn.com/prod/api/pro/product/tree?siteId={_SITE_ID}"
_PRODUCT_BASE = "https://www.tendacn.com/prod/api/pro/product/list"
_DOWNLOAD_BASE = "https://www.tendacn.com/prod/api/data/center/list"


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _make_download_url(product_id: int) -> str:
    return (
        f"{_DOWNLOAD_BASE}?siteId={_SITE_ID}"
        f"&linkProductOrClass={product_id}"
        f"&pageSize=100&format=zip"
    )


# =============================================================================
# Mock HttpFetcher
# =============================================================================


@dataclass
class _MockHttpFetcher:
    """回放 fixture 数据的 HttpFetcher 替代。

    通过 fail_product_ids 控制特定产品的固件 API 失败。
    """

    fail_product_ids: set[int] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    async def get_json(self, url: str, *, headers=None) -> FetchedJson:  # noqa: ARG002
        self.calls.append(url)

        if url == _TREE_URL:
            return FetchedJson(url=url, status_code=200, data=_load("product-tree.json"))

        # Product list by category
        if url.startswith(_PRODUCT_BASE):
            if "categoryId=68" in url:
                return FetchedJson(url=url, status_code=200, data=_load("product-list-68.json"))
            elif "categoryId=34" in url:
                return FetchedJson(url=url, status_code=200, data=_load("product-list-34.json"))
            elif "categoryId=31" in url:
                return FetchedJson(url=url, status_code=200, data=_load("product-list-31.json"))
            # Unknown category: return empty
            return FetchedJson(url=url, status_code=200, data=_load("product-list-empty.json"))

        # Firmware downloads
        if url.startswith(_DOWNLOAD_BASE):
            if "linkProductOrClass=757594406256709" in url:
                return FetchedJson(
                    url=url, status_code=200, data=_load("firmware-be12pro.json")
                )
            elif "linkProductOrClass=941" in url:
                return FetchedJson(
                    url=url, status_code=200, data=_load("firmware-re6lpro.json")
                )
            elif "linkProductOrClass=683497529253957" in url:
                return FetchedJson(url=url, status_code=200, data=_load("firmware-i36.json"))
            elif "linkProductOrClass=791" in url:
                # i29 - no firmware yet
                return FetchedJson(url=url, status_code=200, data=_load("firmware-empty.json"))
            elif "linkProductOrClass=807705463984197" in url:
                # CS6G - no firmware yet (new product)
                return FetchedJson(url=url, status_code=200, data=_load("firmware-empty.json"))

        raise AssertionError(f"Unexpected URL: {url}")


@dataclass
class _FailingTreeHttpFetcher:
    """产品树 API 失败。"""

    async def get_json(self, url: str, *, headers=None) -> FetchedJson:  # noqa: ARG002
        raise ConnectionError("simulated tree failure")


async def _discover(fetcher=None):
    adapter = TendaGlobalAdapter(fetcher or _MockHttpFetcher())
    return [event async for event in adapter.discover()]


def _products(events):
    return [event.product for event in events if isinstance(event, DiscoveredProduct)]


def _skipped(events):
    return [event for event in events if isinstance(event, SkippedCandidate)]


# =============================================================================
# 单元测试：纯函数
# =============================================================================


class TestFilenameFromUrl:
    """URL 文件名提取。"""

    def test_basic_url(self) -> None:
        assert _filename_from_url(
            "https://static.tenda.com.cn/document/2026/05/28/abc/US_BE12ProV1.0mt_V16.03.66.23_TD01.zip"
        ) == "US_BE12ProV1.0mt_V16.03.66.23_TD01.zip"

    def test_url_with_query(self) -> None:
        assert _filename_from_url("https://example.com/firmware.zip?token=123") == "firmware.zip"

    def test_empty_url(self) -> None:
        assert _filename_from_url("") is None

    def test_no_filename(self) -> None:
        assert _filename_from_url("https://example.com/") is None


class TestSourceKey:
    """source_key 生成规则。"""

    def test_product_key(self) -> None:
        assert _product_source_key(757594406256709) == "tenda:757594406256709"

    def test_revision_key_with_version(self) -> None:
        assert _revision_source_key(123, "V3.0") == "tenda:123:hw:V3.0"

    def test_revision_key_empty_version(self) -> None:
        assert _revision_source_key(123, "") == UNSPECIFIED_REVISION_SOURCE_KEY

    def test_revision_key_whitespace_version(self) -> None:
        assert _revision_source_key(123, "  ") == UNSPECIFIED_REVISION_SOURCE_KEY


class TestDeduplicateFirmware:
    """固件版本去重。"""

    def test_keep_latest_by_update_time(self) -> None:
        records = [
            {"id": 1, "version": "V16.03.66.18", "updateTime": "2026-02-02"},
            {"id": 2, "version": "V16.03.66.23", "updateTime": "2026-05-28"},
        ]
        result = _deduplicate_firmware(records)
        assert len(result) == 2

    def test_dedup_same_version(self) -> None:
        records = [
            {"id": 1, "version": "V1.0.0", "updateTime": "2025-01-01"},
            {"id": 2, "version": "V1.0.0", "updateTime": "2025-06-01"},
        ]
        result = _deduplicate_firmware(records)
        assert len(result) == 1
        assert result[0]["id"] == 2

    def test_empty_version_uses_id(self) -> None:
        records = [
            {"id": 1, "version": "", "updateTime": ""},
            {"id": 2, "version": "", "updateTime": ""},
        ]
        result = _deduplicate_firmware(records)
        assert len(result) == 2


# =============================================================================
# 集成测试：适配器 discover()
# =============================================================================


@pytest.mark.anyio
async def test_discover_yields_all_target_products() -> None:
    """应产出所有目标分类下有固件的产品。"""
    events = await _discover()
    products = _products(events)

    models = {p.model_raw for p in products}
    assert models == {"BE12 Pro", "RE6L Pro", "i36"}


@pytest.mark.anyio
async def test_router_product_structure() -> None:
    """BE12 Pro 应有完整的 Candidate 树。"""
    products = _products(await _discover())
    p = next(p for p in products if p.model_raw == "BE12 Pro")

    assert p.source_key == "tenda:757594406256709"
    assert p.display_name == "BE12 Pro"
    assert p.product_family == ProductFamily.ROUTER
    assert p.product_type == ProductType.ROUTER
    assert p.source_category == "Wi-Fi 7 Routers"

    revision = p.hardware_revisions[0]
    assert revision.source_key == UNSPECIFIED_REVISION_SOURCE_KEY
    assert revision.normalized_revision == UNSPECIFIED_REVISION
    assert revision.revision_explicit is False

    # BE12 Pro has 3 firmware versions
    releases = revision.releases
    assert len(releases) == 3

    # Latest firmware first
    versions = {r.version_raw for r in releases}
    assert "V16.03.66.23" in versions
    assert "V16.03.66.21" in versions
    assert "V16.03.66.18" in versions

    # Check artifact
    artifact = releases[0].artifacts[0]
    assert artifact.artifact_type.value == "firmware"
    assert artifact.media_type == "application/zip"
    assert artifact.original_filename == "US_BE12ProV1.0mt_V16.03.66.23_TD01.zip"
    assert artifact.advertised_size == 34765295
    assert artifact.download_url.startswith("https://static.tenda.com.cn/")


@pytest.mark.anyio
async def test_ap_product_classified_as_wireless_ap() -> None:
    """Ceiling AP 应分类为 wireless_ap。"""
    products = _products(await _discover())
    ap = next(p for p in products if p.model_raw == "i36")

    assert ap.product_family == ProductFamily.ROUTER
    assert ap.product_type == ProductType.WIRELESS_AP


@pytest.mark.anyio
async def test_product_without_firmware_is_skipped() -> None:
    """无固件的产品应被跳过（SkippedCandidate）。"""
    events = await _discover()
    skipped = _skipped(events)

    assert len(skipped) == 2  # i29 and CS6G have no firmware
    skipped_models_raw_hints = {s.raw_hint for s in skipped}
    assert "791" in skipped_models_raw_hints  # i29
    assert "807705463984197" in skipped_models_raw_hints  # CS6G


@pytest.mark.anyio
async def test_source_key_stability() -> None:
    """同一产品在多次运行中 source_key 保持不变。"""
    first = _products(await _discover())
    second = _products(await _discover())

    def collect_keys(products):
        keys = set()
        for p in products:
            keys.add(p.source_key)
            for hw in p.hardware_revisions:
                keys.add(hw.source_key)
                for rel in hw.releases:
                    keys.add(rel.source_key)
                    for art in rel.artifacts:
                        keys.add(art.source_key)
        return keys

    assert collect_keys(first) == collect_keys(second)


@pytest.mark.anyio
async def test_discovery_completed_last_event() -> None:
    """DiscoveryCompleted 必须是最后一个事件。"""
    events = await _discover()
    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is True


@pytest.mark.anyio
async def test_tree_fetch_failure_is_catastrophic() -> None:
    """产品树 API 获取失败时直接产出 incomplete DiscoveryCompleted。"""
    events = [event async for event in TendaGlobalAdapter(_FailingTreeHttpFetcher()).discover()]

    assert len(events) == 1
    assert isinstance(events[0], DiscoveryCompleted)
    assert events[0].is_complete is False
    assert "simulated tree failure" in (events[0].incomplete_reason or "")
