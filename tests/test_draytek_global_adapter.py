"""DrayTek 全球站适配器测试，所有网络响应来自固定 fixture。"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from firmatlas.adapters.draytek_global.adapter import DraytekGlobalAdapter
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
from firmatlas.domain.model import ProductType

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "draytek-global"
ROOT_URL = "https://fw.draytek.com.tw/"


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _responses() -> dict[str, str]:
    """构建 mock HTTP 响应映射：URL → 响应文本。"""
    return {
        ROOT_URL: _fixture("ftp-root.html"),
        # Vigor2767 产品
        "https://fw.draytek.com.tw/Vigor2767/Firmware/": _fixture(
            "firmware-vigor2767.html"
        ),
        "https://fw.draytek.com.tw/Vigor2767/Firmware/latest.txt": _fixture(
            "latest-vigor2767.txt"
        ),
        "https://fw.draytek.com.tw/Vigor2767/Firmware/v5.4.0/": _fixture(
            "version-vigor2767-v5.4.0.html"
        ),
        "https://fw.draytek.com.tw/Vigor2767/Firmware/v5.4.0/FIRMWARE.DIGESTS": _fixture(
            "digests-vigor2767.txt"
        ),
        # Vigor2962 产品 (多 channel)
        "https://fw.draytek.com.tw/Vigor2962/Firmware/": _fixture(
            "firmware-vigor2962.html"
        ),
        "https://fw.draytek.com.tw/Vigor2962/Firmware/latest.txt": _fixture(
            "latest-vigor2962.txt"
        ),
        "https://fw.draytek.com.tw/Vigor2962/Firmware/latest_stable.txt": _fixture(
            "latest-stable-vigor2962.txt"
        ),
        "https://fw.draytek.com.tw/Vigor2962/Firmware/v4.4.6.1/": _fixture(
            "version-vigor2962-v4.4.6.1.html"
        ),
        "https://fw.draytek.com.tw/Vigor2962/Firmware/v4.4.6.1/FIRMWARE.DIGESTS": _fixture(
            "digests-vigor2962.txt"
        ),
        "https://fw.draytek.com.tw/Vigor2962/Firmware/v4.4.5.3/": _fixture(
            "version-vigor2962-v4.4.5.3.html"
        ),
        "https://fw.draytek.com.tw/Vigor2962/Firmware/v4.4.5.3/FIRMWARE.DIGESTS": _fixture(
            "digests-vigor2962-stable.txt"
        ),
    }


@dataclass
class _MockHttpFetcher:
    """Mock HttpFetcher，从预定义字典返回响应。"""

    responses: dict[str, str]
    fail_url: str | None = None

    async def get_text(self, url: str, *, headers=None):
        from firmatlas.infra.http_client import FetchError, FetchedText

        if self.fail_url is not None and url.casefold() == self.fail_url.casefold():
            raise FetchError(url=url, status_code=None, detail="simulated failure")

        # 查找时同时匹配 URL 和 URL+"/" 两种形式
        for key, value in self.responses.items():
            if key.rstrip("/") == url.rstrip("/"):
                return FetchedText(url=url, status_code=200, text=value)

        raise AssertionError(f"unexpected URL: {url!r}")


async def _discover(fetcher: _MockHttpFetcher | None = None):
    adapter = DraytekGlobalAdapter(fetcher or _MockHttpFetcher(_responses()))
    return [event async for event in adapter.discover()]


def _products(events):
    return [event.product for event in events if isinstance(event, DiscoveredProduct)]


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_discover_only_target_products() -> None:
    """只发现 Vigor 系列 Router，跳过 Switch 和非 Vigor 目录。"""
    events = await _discover()
    products = _products(events)

    names = {product.model_raw for product in products}
    assert "Vigor2767" in names
    assert "Vigor2962" in names
    assert "VigorSwitch G1080" not in names
    assert "ACS 3" not in names
    assert "Utility" not in names

    # 验证分类
    type_map = {product.model_raw: product.product_type for product in products}
    assert type_map["Vigor2767"] == ProductType.ROUTER


@pytest.mark.anyio
async def test_discover_reports_skipped_non_targets() -> None:
    """非目标产品应产出 SkippedCandidate。"""
    events = await _discover()
    skipped = [event for event in events if isinstance(event, SkippedCandidate)]

    assert len(skipped) == 1
    assert skipped[0].stage == "product"
    assert skipped[0].reason_code == SkipReason.UNMAPPED_TYPE
    assert skipped[0].source_url == ROOT_URL


@pytest.mark.anyio
async def test_discover_firmware_release_and_artifact() -> None:
    """验证 Vigor2767 的 FirmwareRelease 和 FirmwareArtifact 字段。"""
    products = _products(await _discover())
    vigor2767 = next(p for p in products if p.model_raw == "Vigor2767")

    # 硬件版本
    assert len(vigor2767.hardware_revisions) == 1
    hw = vigor2767.hardware_revisions[0]
    assert hw.normalized_revision == "unspecified"
    assert hw.revision_explicit is False

    # 固件发布
    assert len(hw.releases) == 1
    release = hw.releases[0]
    assert release.version_raw == "5.4.0"
    assert release.version_normalized == "5.4.0"
    assert "Vigor2767 Firmware 5.4.0" in (release.title or "")

    # 固件 Artifact
    assert len(release.artifacts) == 1
    artifact = release.artifacts[0]
    assert artifact.artifact_type.value == "firmware"
    assert artifact.original_filename == "Vigor2767_v5.4.0.zip"
    assert "Vigor2767/Firmware/v5.4.0/Vigor2767_v5.4.0.zip" in artifact.download_url
    assert artifact.media_type == "application/zip"

    # 官方校验和
    assert artifact.official_checksum is not None
    assert artifact.official_checksum.algorithm == "sha1"
    assert len(artifact.official_checksum.value) == 40


@pytest.mark.anyio
async def test_discover_multi_channel_product() -> None:
    """Vigor2962 有 latest.txt 和 latest_stable.txt，应产出两个 Release。"""
    products = _products(await _discover())
    vigor2962 = next(p for p in products if p.model_raw == "Vigor2962")

    hw = vigor2962.hardware_revisions[0]
    assert len(hw.releases) == 2

    versions = {release.version_normalized for release in hw.releases}
    assert versions == {"4.4.6.1", "4.4.5.3"}


@pytest.mark.anyio
async def test_discover_release_notes_url() -> None:
    """版本目录中有 PDF 时，应作为 release_notes_url。"""
    products = _products(await _discover())
    vigor2767 = next(p for p in products if p.model_raw == "Vigor2767")

    release = vigor2767.hardware_revisions[0].releases[0]
    assert release.release_notes_url is not None
    assert "release-note" in release.release_notes_url.lower()
    assert release.release_notes_url.endswith(".pdf")


@pytest.mark.anyio
async def test_discover_complete_on_success() -> None:
    """所有产品成功处理时，DiscoveryCompleted.is_complete 应为 True。"""
    events = await _discover()
    completed = events[-1]
    assert isinstance(completed, DiscoveryCompleted)
    assert completed.is_complete is True
    assert completed.incomplete_reason is None


@pytest.mark.anyio
async def test_discover_incomplete_on_failure() -> None:
    """产品目录访问失败时，is_complete 应为 False。"""
    fetcher = _MockHttpFetcher(
        _responses(),
        fail_url="https://fw.draytek.com.tw/Vigor2767/Firmware/",
    )
    events = await _discover(fetcher)
    completed = events[-1]
    assert isinstance(completed, DiscoveryCompleted)
    assert completed.is_complete is False
    assert "1 个" in (completed.incomplete_reason or "")


@pytest.mark.anyio
async def test_discover_root_fetch_error_produces_incomplete() -> None:
    """FTP 根目录访问失败时，直接产出不完整的 DiscoveryCompleted。"""
    fetcher = _MockHttpFetcher(
        _responses(),
        fail_url=ROOT_URL,
    )
    events = await _discover(fetcher)
    assert len(events) == 1
    assert isinstance(events[0], DiscoveryCompleted)
    assert events[0].is_complete is False


@pytest.mark.anyio
async def test_product_source_url_is_constructed() -> None:
    """产品 source_url 应从 FTP 目录名推导。"""
    products = _products(await _discover())
    vigor2767 = next(p for p in products if p.model_raw == "Vigor2767")
    assert vigor2767.source_url == "https://www.draytek.com/products/vigor2767/"


@pytest.mark.anyio
async def test_source_key_contract_is_stable() -> None:
    """两次运行同一 fixture 应产出相同的 source_key（大小写不敏感）。"""
    first = next(
        p for p in _products(await _discover()) if p.model_raw == "Vigor2767"
    )
    second = next(
        p for p in _products(await _discover()) if p.model_raw == "Vigor2767"
    )

    assert first.source_key == second.source_key
    assert first.source_key == "draytek-ftp:vigor2767"

    hw1 = first.hardware_revisions[0]
    hw2 = second.hardware_revisions[0]
    assert hw1.source_key == hw2.source_key

    rel1 = hw1.releases[0]
    rel2 = hw2.releases[0]
    assert rel1.source_key == rel2.source_key
    assert rel1.source_key == "draytek-ftp:vigor2767/fw/5.4.0"

    art1 = rel1.artifacts[0]
    art2 = rel2.artifacts[0]
    assert art1.source_key == art2.source_key
    assert art1.source_key == (
        "draytek-ftp:Vigor2767/Firmware/v5.4.0/Vigor2767_v5.4.0.zip"
    )


# ---------------------------------------------------------------------------
# refresh_artifact_url 测试
# ---------------------------------------------------------------------------


def _refresh_request(**overrides) -> ArtifactRefreshRequest:
    fields = {
        "product_source_key": "draytek-ftp:vigor2767",
        "hardware_revision_source_key": "__unspecified__",
        "release_source_key": "draytek-ftp:vigor2767/fw/5.4.0",
        "artifact_source_key": (
            "draytek-ftp:Vigor2767/Firmware/v5.4.0/Vigor2767_v5.4.0.zip"
        ),
        "stale_url": "https://fw.draytek.com.tw/Vigor2767/Firmware/v5.4.0/Vigor2767_v5.4.0.zip",
        "known_filename": "Vigor2767_v5.4.0.zip",
        "known_size": None,
        "known_checksum": None,
    }
    fields.update(overrides)
    return ArtifactRefreshRequest(**fields)


@pytest.mark.anyio
async def test_refresh_finds_same_artifact() -> None:
    """刷新应返回当前的下载 URL。"""
    adapter = DraytekGlobalAdapter(_MockHttpFetcher(_responses()))
    result = await adapter.refresh_artifact_url(_refresh_request())

    assert isinstance(result, ArtifactUrlRefreshed)
    assert result.download_url == (
        "https://fw.draytek.com.tw/Vigor2767/Firmware/v5.4.0/"
        "Vigor2767_v5.4.0.zip"
    )


@pytest.mark.anyio
async def test_refresh_rejects_identity_mismatch() -> None:
    """product_source_key 不匹配时应失败。"""
    adapter = DraytekGlobalAdapter(_MockHttpFetcher(_responses()))
    result = await adapter.refresh_artifact_url(
        _refresh_request(product_source_key="draytek-ftp:someotherproduct")
    )

    assert isinstance(result, ArtifactRefreshFailed)
    assert result.reason_code == RefreshFailureReason.IDENTITY_CONFLICT


@pytest.mark.anyio
async def test_refresh_rejects_unparseable_artifact_key() -> None:
    """无法解析的 artifact_source_key 应拒绝。"""
    adapter = DraytekGlobalAdapter(_MockHttpFetcher(_responses()))
    result = await adapter.refresh_artifact_url(
        _refresh_request(artifact_source_key="garbage-key")
    )

    assert isinstance(result, ArtifactRefreshFailed)
    assert result.reason_code == RefreshFailureReason.IDENTITY_CONFLICT
