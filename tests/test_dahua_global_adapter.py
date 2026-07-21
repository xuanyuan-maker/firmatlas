"""大华国际站适配器测试（基于脱敏 fixture）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from firmatlas.adapters.dahua_global.adapter import DahuaGlobalAdapter
from firmatlas.adapters.events import (
    ArtifactRefreshFailed,
    ArtifactRefreshRequest,
    ArtifactUrlRefreshed,
    DiscoveredProduct,
    DiscoveryCompleted,
    RefreshFailureReason,
    SkippedCandidate,
    SkipReason,
)
from firmatlas.infra.http_client import FetchedJson

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dahua-global"
_LIST_PATH = "/api/en/downloadCenter/firmware/list"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@dataclass
class _MockHttpFetcher:
    """回放 fixture 的虚拟 HttpFetcher，不发送真实网络请求。"""

    redirect_url: str | None = None
    page_overrides: dict[str, Any] | None = None
    fail_category_ids: set[int] | None = None
    calls: list[str] = None

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    async def get_json(self, url: str, *, headers=None) -> FetchedJson:
        parsed = urlsplit(url)
        self.calls.append(url)
        query = parse_qs(parsed.query)

        page = int(query.get("page", ["1"])[0])
        category_id = int(query.get("child_menu_id", ["0"])[0])

        if self.fail_category_ids and category_id in self.fail_category_ids:
            raise ConnectionError("simulated failure")

        if self.page_overrides and (category_id, page) in self.page_overrides:
            data = self.page_overrides[(category_id, page)]
        elif category_id == 1:
            data = _fixture("network_cameras_page1.json")
        else:
            data = _fixture("empty_response.json")

        return FetchedJson(
            url=self.redirect_url or url,
            status_code=200,
            data=data,
        )


def _mk_http(**kwargs) -> _MockHttpFetcher:
    return _MockHttpFetcher(**kwargs)


async def _discover(**http_kwargs):
    adapter = DahuaGlobalAdapter(_mk_http(**http_kwargs))
    return [event async for event in adapter.discover()]


def _products(events):
    return [event.product for event in events if isinstance(event, DiscoveredProduct)]


def _skipped(events):
    return [event for event in events if isinstance(event, SkippedCandidate)]


# ---------------------------------------------------------------------------
# discover 测试
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_discover_network_cameras_produces_products() -> None:
    """Network Cameras 分类能正确解析为产品树。"""
    events = await _discover()
    products = _products(events)

    assert len(products) > 0

    product_names = {p.model_raw for p in products}
    assert "IPC-HFW8449J-Z-SC" in product_names
    assert "IPC-HDBW2449F-AS-E2-IL" in product_names

    for p in products:
        assert p.product_family is not None
        assert p.product_type is not None
        assert p.source_key.startswith("pid:")
        assert len(p.hardware_revisions) == 1
        hw = p.hardware_revisions[0]
        assert hw.source_key == "__unspecified__"
        assert hw.normalized_revision == "unspecified"
        assert hw.revision_explicit is False
        assert len(hw.releases) > 0
        for rel in hw.releases:
            assert rel.source_key.startswith("fid:")
            assert len(rel.artifacts) == 1
            art = rel.artifacts[0]
            assert art.source_key.startswith("fid:")
            assert art.download_url.startswith("https://materialfile.dahuasecurity.com/")

    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is True


@pytest.mark.anyio
async def test_source_keys_are_stable() -> None:
    """同一 fixture 返回的 source_key 不应变化。"""
    first_events = await _discover()
    second_events = await _discover()

    first_products = {p.source_key: p for p in _products(first_events)}
    second_products = {p.source_key: p for p in _products(second_events)}

    assert set(first_products) == set(second_products)

    for key in first_products:
        p1 = first_products[key]
        p2 = second_products[key]
        assert p1.hardware_revisions[0].releases == p2.hardware_revisions[0].releases


@pytest.mark.anyio
async def test_product_with_multiple_firmware_entries() -> None:
    """同一产品（pid:154375）出现在多个固件条目中时，应有多个 release。"""
    events = await _discover()
    products = {p.source_key: p for p in _products(events)}

    ipc_hdw21249t = products.get("pid:154375")
    assert ipc_hdw21249t is not None
    hw = ipc_hdw21249t.hardware_revisions[0]
    assert len(hw.releases) >= 2
    versions = {r.version_raw for r in hw.releases}
    assert "V3.120.0000000.38.R.260623" in versions


@pytest.mark.anyio
async def test_release_has_release_notes() -> None:
    """带 firmware_note 的固件应有 release_notes_url。"""
    events = await _discover()
    products = {p.source_key: p for p in _products(events)}

    ipc_hfw8449j = products.get("pid:154301")
    assert ipc_hfw8449j is not None
    hw = ipc_hfw8449j.hardware_revisions[0]
    assert len(hw.releases) == 1
    rel = hw.releases[0]
    assert rel.release_notes_url is not None
    assert "release_notes.pdf" in rel.release_notes_url


@pytest.mark.anyio
async def test_empty_category_is_handled() -> None:
    """空分类（非摄像机分类）返回时 adaptor 正常处理。"""
    fetcher = _mk_http()
    adapter = DahuaGlobalAdapter(fetcher)
    events = [event async for event in adapter.discover()]

    products = _products(events)
    assert len(products) > 0
    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is True


# ---------------------------------------------------------------------------
# refresh_artifact_url 测试
# ---------------------------------------------------------------------------


def _make_refresh_request(
    product_source_key: str, release_source_key: str, artifact_source_key: str
) -> ArtifactRefreshRequest:
    return ArtifactRefreshRequest(
        product_source_key=product_source_key,
        hardware_revision_source_key="__unspecified__",
        release_source_key=release_source_key,
        artifact_source_key=artifact_source_key,
        stale_url="https://materialfile.dahuasecurity.com/uploads/cpq/old/expired.zip",
        known_filename=None,
        known_size=None,
        known_checksum=None,
    )


@pytest.mark.anyio
async def test_refresh_finds_matching_artifact() -> None:
    """按 firmware_id 刷新找到同一个 artifact。"""
    events = await _discover()
    products = {p.source_key: p for p in _products(events)}

    hfw8449j = products["pid:154301"]
    release = hfw8449j.hardware_revisions[0].releases[0]
    artifact = release.artifacts[0]

    adapter = DahuaGlobalAdapter(_mk_http())
    result = await adapter.refresh_artifact_url(
        _make_refresh_request(
            product_source_key=hfw8449j.source_key,
            release_source_key=release.source_key,
            artifact_source_key=artifact.source_key,
        )
    )

    assert isinstance(result, ArtifactUrlRefreshed)
    assert "260708.zip" in result.download_url


@pytest.mark.anyio
async def test_refresh_not_found_for_missing_firmware() -> None:
    """不存在的 firmware_id 返回 NOT_FOUND。"""
    adapter = DahuaGlobalAdapter(_mk_http())
    result = await adapter.refresh_artifact_url(
        _make_refresh_request(
            product_source_key="pid:154301",
            release_source_key="fid:99999",
            artifact_source_key="fid:99999",
        )
    )

    assert isinstance(result, ArtifactRefreshFailed)
    assert result.reason_code == RefreshFailureReason.NOT_FOUND


@pytest.mark.anyio
async def test_refresh_rejects_hardware_revision_conflict() -> None:
    """非 unspecified 的硬版本请求应被拒绝。"""
    adapter = DahuaGlobalAdapter(_mk_http())
    result = await adapter.refresh_artifact_url(
        ArtifactRefreshRequest(
            product_source_key="pid:154301",
            hardware_revision_source_key="hw:rev1",
            release_source_key="fid:58914",
            artifact_source_key="fid:58914",
            stale_url="https://materialfile.dahuasecurity.com/old.zip",
            known_filename=None,
            known_size=None,
            known_checksum=None,
        )
    )

    assert isinstance(result, ArtifactRefreshFailed)
    assert result.reason_code == RefreshFailureReason.IDENTITY_CONFLICT


@pytest.mark.anyio
async def test_refresh_rejects_release_key_mismatch() -> None:
    """release_source_key 不匹配时返回 IDENTITY_CONFLICT。"""
    adapter = DahuaGlobalAdapter(_mk_http())
    result = await adapter.refresh_artifact_url(
        _make_refresh_request(
            product_source_key="pid:154301",
            release_source_key="fid:00000",
            artifact_source_key="fid:58914",
        )
    )

    assert isinstance(result, ArtifactRefreshFailed)
    assert result.reason_code == RefreshFailureReason.IDENTITY_CONFLICT


# ---------------------------------------------------------------------------
# 分类与跳过测试
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_nvr_in_category_is_skipped() -> None:
    """分类中包含 NVR 产品名时应跳过。"""
    nvr_fixture = {
        "code": "200",
        "message": "OK",
        "data": {
            "page": 1,
            "size": 10,
            "total": 1,
            "list": [
                {
                    "firmware_id": "99901",
                    "firmware_name": "DH_NVR4x-4KS3-L_MultiLang_V5.001.0000000.0.R.260623",
                    "firmware_url": "https://materialfile.dahuasecurity.com/nvr_firmware.zip",
                    "firmware_updates": "",
                    "firmware_note": "",
                    "firmware_file_size": "55.23MB",
                    "post_date": "2026-07-01",
                    "md5": "aaaabbbbccccddddeeeeffff00009999",
                    "hash": "aaaabbbbccccddddeeeeffff000099990000111122223333444455556666",
                    "original_name": "NVR_firmware",
                    "product": [
                        {
                            "product_id": "155397",
                            "product_name": "NVR4216-16P-4KS3-L",
                            "product_keywords": "https://www.dahuasecurity.com/products/nvr",
                            "menu_id": "0",
                            "edition": "S0",
                        }
                    ],
                }
            ],
        },
    }

    fetcher = _mk_http(
        page_overrides={(1, 1): nvr_fixture},
    )
    adapter = DahuaGlobalAdapter(fetcher)
    events = [event async for event in adapter.discover()]

    products = _products(events)
    assert len(products) == 0
    skipped = _skipped(events)
    nvr_skipped = [s for s in skipped if "录像机" in (s.detail or "")]
    assert len(nvr_skipped) >= 1
    assert all(s.reason_code == SkipReason.UNMAPPED_TYPE for s in nvr_skipped)


@pytest.mark.anyio
async def test_api_error_is_incomplete() -> None:
    """API 请求失败时 DiscoveryCompleted.is_complete 为 False。"""
    fetcher = _mk_http(fail_category_ids={1, 586, 4472, 4572, 14197, 18199})
    adapter = DahuaGlobalAdapter(fetcher)
    events = [event async for event in adapter.discover()]

    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is False
    assert len(events[-1].issues) > 0


@pytest.mark.anyio
async def test_version_extraction_from_firmware_name() -> None:
    """固件名中的版本号正确提取。"""
    events = await _discover()
    products = {p.source_key: p for p in _products(events)}

    hfw8449j = products["pid:154301"]
    release = hfw8449j.hardware_revisions[0].releases[0]
    assert release.version_raw == "V3.146.0000000.32.R.260708"
    assert release.version_normalized == "v3.146.0000000.32.r.260708"

    hdbw2449 = products["pid:146777"]
    release = hdbw2449.hardware_revisions[0].releases[0]
    assert release.version_raw == "V3.146.0000000.32.R.260708"
