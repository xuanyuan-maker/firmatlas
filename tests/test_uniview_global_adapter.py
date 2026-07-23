"""宇视科技 Uniview 全球站适配器测试，全部使用 fixture 数据。"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from datetime import date

import pytest

from firmatlas.adapters.events import (
    DiscoveredProduct,
    DiscoveryCompleted,
)
from firmatlas.adapters.uniview_global.adapter import (
    UniviewGlobalAdapter,
    _extract_version_from_filename,
    _parse_date,
    _product_source_key,
    _safe_key_part,
)
from firmatlas.domain.model import ArtifactType, ProductFamily, ProductType
from firmatlas.infra.http_client import FetchError, FetchedText

# ---------------------------------------------------------------------------
# Fixture 路径
# ---------------------------------------------------------------------------

_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "uniview-global"

_CATEGORY_URLS = {
    "Network Cameras": "https://global.uniview.com/us/Support/Download_Center/Firmware/Network_Cameras/",
    "PTZ Cameras": "https://global.uniview.com/us/Support/Download_Center/Firmware/PTZ_Cameras/",
    "Thermal Cameras": "https://global.uniview.com/us/Support/Download_Center/Firmware/Thermal_Cameras/",
}


def _load_fixture(filename: str) -> str:
    return (_FIXTURE_DIR / filename).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Mock HttpFetcher
# ---------------------------------------------------------------------------


@dataclass
class _MockHttpFetcher:
    """回放 fixture HTML 的 HttpFetcher 替代。"""

    fail_urls: set[str] = field(default_factory=set)
    error_status_urls: dict[str, int] = field(default_factory=dict)
    body_overrides: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def get_text(self, url: str) -> FetchedText:
        self.calls.append(url)

        if url in self.fail_urls:
            raise FetchError(url=url, status_code=503, detail="simulated failure")

        if url in self.error_status_urls:
            status = self.error_status_urls[url]
            return FetchedText(url=url, status_code=status, text="")

        if url in self.body_overrides:
            return FetchedText(url=url, status_code=200, text=self.body_overrides[url])

        for cat_name, cat_url in _CATEGORY_URLS.items():
            if url == cat_url:
                filename = cat_name.lower().replace(" ", "_") + ".html"
                html = _load_fixture(filename)
                return FetchedText(url=url, status_code=200, text=html)

        raise AssertionError(f"Unexpected URL: {url}")


async def _discover(fetcher: _MockHttpFetcher | None = None):
    adapter = UniviewGlobalAdapter(fetcher or _MockHttpFetcher())
    return [event async for event in adapter.discover()]


def _products(events):
    return [event.product for event in events if isinstance(event, DiscoveredProduct)]


# =============================================================================
# 单元测试：纯函数
# =============================================================================


class TestExtractVersionFromFilename:
    def test_standard_format(self) -> None:
        assert (
            _extract_version_from_filename("GIPC-B6218.7.5.251212 20260120.zip")
            == "B6218.7.5.251212"
        )

    def test_no_gipc_prefix(self) -> None:
        assert (
            _extract_version_from_filename("NVR_S300_R24-B5211.37.61.23091520240605.zip")
            == "NVR_S300_R24-B5211.37.61.23091520240605"
        )

    def test_no_version_match(self) -> None:
        assert (
            _extract_version_from_filename("unknown.zip")
            == "unknown"
        )


class TestParseDate:
    def test_valid_date(self) -> None:
        assert _parse_date("2026-01-20") == date(2026, 1, 20)

    def test_empty(self) -> None:
        assert _parse_date("") is None

    def test_invalid(self) -> None:
        assert _parse_date("not-a-date") is None


class TestProductSourceKey:
    def test_stability(self) -> None:
        key = _product_source_key("IPC3628SR-ADF28KM-WP")
        assert key == "uniview:IPC3628SR-ADF28KM-WP"
        assert key == _product_source_key("IPC3628SR-ADF28KM-WP")

    def test_normalization(self) -> None:
        assert _product_source_key("ipc3628sr-adf28km-wp") == "uniview:ipc3628sr-adf28km-wp"


class TestSafeKeyPart:
    def test_spaces_replaced(self) -> None:
        assert _safe_key_part("GIPC-B6218.7.5.251212 20260120.zip") == "GIPC-B6218.7.5.251212_20260120.zip"

    def test_parens_removed(self) -> None:
        assert _safe_key_part("ADF28(40)K-WP") == "ADF2840K-WP"


# =============================================================================
# 集成测试：适配器 discover()
# =============================================================================


@pytest.mark.anyio
async def test_discover_yields_products() -> None:
    """应产出 Network Cameras (3) + PTZ Cameras (3) + Thermal (0) = 6 个产品。"""
    events = await _discover()
    products = _products(events)
    assert len(products) == 6


@pytest.mark.anyio
async def test_all_products_are_camera_type() -> None:
    """所有产品应为 camera 类型。"""
    products = _products(await _discover())
    for p in products:
        assert p.product_family == ProductFamily.CAMERA
        assert p.product_type == ProductType.CAMERA


@pytest.mark.anyio
async def test_shared_firmware_multiple_products() -> None:
    """同一固件 zip 被多个产品共用时，每个产品有独立的 release。"""
    products = _products(await _discover())
    nc_products = [p for p in products if p.source_category == "Network Cameras"]
    assert len(nc_products) == 3

    # 所有 Network Cameras 应共享同一个固件 zip
    all_filenames: set[str | None] = set()
    for p in nc_products:
        for hw in p.hardware_revisions:
            for r in hw.releases:
                for artifact in r.artifacts:
                    all_filenames.add(artifact.original_filename)
    assert len(all_filenames) == 1  # 只有一个固件文件
    assert "GIPC-B6218.7.5.251212 20260120.zip" in all_filenames


@pytest.mark.anyio
async def test_different_categories_have_different_firmware() -> None:
    """不同分类的产品固件应来自不同的文件。"""
    products = _products(await _discover())
    nc = [p for p in products if p.source_category == "Network Cameras"][0]
    ptz = [p for p in products if p.source_category == "PTZ Cameras"][0]

    nc_filename = nc.hardware_revisions[0].releases[0].artifacts[0].original_filename
    ptz_filename = ptz.hardware_revisions[0].releases[0].artifacts[0].original_filename
    assert nc_filename != ptz_filename


@pytest.mark.anyio
async def test_candidate_tree_structure() -> None:
    """验证 ProductCandidate 树各层字段完整性。"""
    products = _products(await _discover())
    first = products[0]

    # Product 层
    assert first.source_key.startswith("uniview:")
    assert first.display_name
    assert first.model_raw
    assert first.model_normalized == first.model_raw.upper()
    assert first.product_family == ProductFamily.CAMERA
    assert first.product_type == ProductType.CAMERA
    assert first.source_category in _CATEGORY_URLS
    assert first.source_url.startswith("https://")

    # Hardware 层
    revision = first.hardware_revisions[0]
    assert revision.source_key == "__unspecified__"
    assert revision.normalized_revision == "unspecified"
    assert revision.revision_explicit is False

    # Release 层
    release = revision.releases[0]
    assert release.version_raw
    assert release.title
    assert release.source_url.startswith("https://")

    # Artifact 层
    artifact = release.artifacts[0]
    assert artifact.artifact_type == ArtifactType.FIRMWARE
    assert artifact.original_filename
    assert artifact.download_url.startswith("https://")
    assert artifact.media_type == "application/zip"


@pytest.mark.anyio
async def test_date_parsed_correctly() -> None:
    """日期字段应正确解析为 date 对象。"""
    products = _products(await _discover())
    # 所有测试 fixture 的日期都是有效的 YYYY-MM-DD
    for p in products:
        for hw in p.hardware_revisions:
            for r in hw.releases:
                assert r.release_date is not None
                assert isinstance(r.release_date, date)


@pytest.mark.anyio
async def test_source_key_stability() -> None:
    """source_key 在多次运行中保持稳定。"""
    first_run = _products(await _discover())
    second_run = _products(await _discover())

    first_keys = {p.source_key for p in first_run}
    second_keys = {p.source_key for p in second_run}
    assert first_keys == second_keys

    # 选一个产品验证各级 source_key
    p1 = first_run[0]
    p2 = second_run[0]
    assert p1.source_key == p2.source_key
    assert p1.hardware_revisions[0].source_key == p2.hardware_revisions[0].source_key
    assert p1.hardware_revisions[0].releases[0].source_key == p2.hardware_revisions[0].releases[0].source_key
    r1 = p1.hardware_revisions[0].releases[0]
    r2 = p2.hardware_revisions[0].releases[0]
    assert r1.artifacts[0].source_key == r2.artifacts[0].source_key


@pytest.mark.anyio
async def test_discovery_completed_last_event() -> None:
    """DiscoveryCompleted 必须是最后一个事件。"""
    events = await _discover()
    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is True


@pytest.mark.anyio
async def test_empty_category_no_products() -> None:
    """Thermal Cameras 分类没有产品时不产出 DiscoveredProduct。"""
    events = await _discover()
    products = _products(events)
    thermal = [p for p in products if p.source_category == "Thermal Cameras"]
    assert len(thermal) == 0


@pytest.mark.anyio
async def test_single_category_failure_not_catastrophic() -> None:
    """单个分类获取失败不应中断整体流程。"""
    fetcher = _MockHttpFetcher(
        fail_urls={_CATEGORY_URLS["PTZ Cameras"]}
    )
    events = await _discover(fetcher)

    products = _products(events)
    assert len(products) == 3  # 只有 Network Cameras

    completion = events[-1]
    assert isinstance(completion, DiscoveryCompleted)
    assert completion.is_complete is False
    assert any("PTZ" in i.detail for i in completion.issues)


@pytest.mark.anyio
async def test_all_categories_failure_is_catastrophic() -> None:
    """所有分类失败时只产出 DiscoveryCompleted(is_complete=False)。"""
    fetcher = _MockHttpFetcher(
        fail_urls=set(_CATEGORY_URLS.values())
    )
    events = await _discover(fetcher)

    assert len(events) == 1
    assert isinstance(events[0], DiscoveryCompleted)
    assert events[0].is_complete is False
    assert "所有分类页面均获取失败" in (events[0].incomplete_reason or "")


@pytest.mark.anyio
async def test_http_404_not_catastrophic() -> None:
    """HTTP 非 200 响应不抛异常，记录 issue 并继续处理其他分类。"""
    fetcher = _MockHttpFetcher(
        error_status_urls={_CATEGORY_URLS["PTZ Cameras"]: 404}
    )
    events = await _discover(fetcher)

    products = _products(events)
    assert len(products) == 3  # 只有 Network Cameras 的数据

    completion = events[-1]
    assert isinstance(completion, DiscoveryCompleted)
    assert completion.is_complete is False
    assert any("404" in i.detail for i in completion.issues)
