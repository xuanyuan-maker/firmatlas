"""Omada Worldwide 适配器测试，全部回放脱敏 fixture。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

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
from firmatlas.adapters.omada_global.adapter import OmadaGlobalAdapter, _size_to_bytes
from firmatlas.domain.model import ProductType
from firmatlas.infra.http_client import FetchedJson

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "omada-global"
MENU_API = "https://support.omadanetworks.com/api/v1/menu/tourist/findProductMenuByTree"
MODEL_API = "https://support.omadanetworks.com/api/v1/resource/tourist/findFirmwareModelByTypeId"
FIRMWARE_API = "https://support.omadanetworks.com/api/v1/resource/tourist/findFirmwareByModel"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@dataclass
class _MockHttpFetcher:
    fail_model: str | None = None
    redirect_url: str | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)

    async def post_json(self, url: str, body: Any, *, headers=None) -> FetchedJson:
        self.calls.append((url, body))
        final_url = self.redirect_url or url
        if url == MENU_API:
            data = _load("product-menu.json")
        elif url == MODEL_API:
            data = _load("model-list.json")
        elif url == FIRMWARE_API:
            model_name = body["modelName"]
            if model_name == self.fail_model:
                raise ConnectionError("simulated failure")
            data = _load("firmware-samples.json")
            data["result"] = [
                item for item in data["result"] if item["title"].startswith(f"{model_name}(")
            ]
        else:
            raise AssertionError(f"unexpected URL: {url}")
        return FetchedJson(url=final_url, status_code=200, data=data)


async def _discover(fetcher: _MockHttpFetcher | None = None):
    adapter = OmadaGlobalAdapter(fetcher or _MockHttpFetcher())
    return [event async for event in adapter.discover()]


def _products(events):
    return [event.product for event in events if isinstance(event, DiscoveredProduct)]


@pytest.mark.anyio
async def test_discover_whitelisted_products_and_complete() -> None:
    events = await _discover()
    products = _products(events)

    assert {product.model_raw for product in products} == {"ER605", "EAP225"}
    assert {product.model_raw: product.product_type for product in products} == {
        "ER605": ProductType.ROUTER,
        "EAP225": ProductType.WIRELESS_AP,
    }
    assert [event for event in events if isinstance(event, SkippedCandidate)] == [
        SkippedCandidate(
            stage="product",
            reason_code=SkipReason.UNMAPPED_TYPE,
            detail="白名单外 Omada 产品共 1 项",
            source_url="https://support.omadanetworks.com/en/download/firmware/",
            raw_hint="non_target_products",
        )
    ]
    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is True


@pytest.mark.anyio
async def test_candidate_tree_preserves_region_revision_and_metadata() -> None:
    products = _products(await _discover())
    router = next(product for product in products if product.model_raw == "ER605")
    revision = router.hardware_revisions[0]
    release = revision.releases[0]
    artifact = release.artifacts[0]

    assert router.source_key == "model-id:1402"
    assert router.source_url.endswith("/er605/")
    assert revision.source_key == "model-id:1402/rev:un-v2.20"
    assert revision.raw_revision == "UN-V2.20"
    assert revision.revision_explicit is True
    assert release.version_raw == "2.4.4 Build 20260630"
    assert release.version_normalized == "2.4.4 Build 20260630"
    assert release.release_date is not None
    assert release.release_date.isoformat() == "2026-07-16"
    assert release.release_notes == (
        "Minimum firmware version applies.\n\nImproved system stability."
    )
    assert artifact.original_filename == "ER605_V2.20_2.4.4.zip"
    assert artifact.advertised_size == 26_828_800
    assert artifact.media_type == "application/zip"


@pytest.mark.parametrize(
    ("size_text", "expected_bytes", "actual_bytes"),
    [
        ("26.20 MB", 26_828_800, 26_826_668),
        ("41.16 MB", 42_147_840, 42_147_091),
        ("62.04 MB", 63_528_960, 63_526_571),
        ("38.33 MB", 39_249_920, 39_250_239),
        ("11.15 MB", 11_417_600, 11_422_171),
        ("20.47 MB", 20_961_280, 20_962_193),
        ("20.36 MB", 20_848_640, 20_853_447),
        ("21.99 MB", 22_517_760, 22_515_164),
    ],
)
def test_omada_size_uses_1000_kibibytes_per_reported_mb(
    size_text: str,
    expected_bytes: int,
    actual_bytes: int,
) -> None:
    converted = _size_to_bytes(size_text)

    assert converted == expected_bytes
    assert converted is not None
    assert abs(converted - actual_bytes) <= 5 * 1024


@pytest.mark.anyio
async def test_source_key_contract_is_stable_and_url_independent() -> None:
    first = next(
        product for product in _products(await _discover()) if product.model_raw == "ER605"
    )
    second = next(
        product for product in _products(await _discover()) if product.model_raw == "ER605"
    )

    first_revision = first.hardware_revisions[0]
    second_revision = second.hardware_revisions[0]
    assert first.source_key == second.source_key == "model-id:1402"
    assert first_revision.source_key == second_revision.source_key
    assert first_revision.releases[0].source_key == second_revision.releases[0].source_key
    assert (
        first_revision.releases[0].artifacts[0].source_key
        == second_revision.releases[0].artifacts[0].source_key
    )
    assert "static.tp-link.com" not in first_revision.releases[0].artifacts[0].source_key


@pytest.mark.anyio
async def test_target_firmware_failure_makes_discovery_incomplete() -> None:
    events = await _discover(_MockHttpFetcher(fail_model="ER605"))

    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is False
    assert "1 个" in (events[-1].incomplete_reason or "")


@pytest.mark.anyio
async def test_api_redirect_outside_source_is_incomplete() -> None:
    events = await _discover(_MockHttpFetcher(redirect_url="https://example.com/api"))

    assert events == [
        DiscoveryCompleted(
            is_complete=False,
            incomplete_reason=(
                "请求 Omada 型号目录失败: Omada API 重定向到来源外: https://example.com/api"
            ),
            issues=(),
        )
    ]


def _refresh_request(product) -> ArtifactRefreshRequest:
    revision = product.hardware_revisions[0]
    release = revision.releases[0]
    artifact = release.artifacts[0]
    return ArtifactRefreshRequest(
        product_source_key=product.source_key,
        hardware_revision_source_key=revision.source_key,
        release_source_key=release.source_key,
        artifact_source_key=artifact.source_key,
        stale_url="https://static.tp-link.com/expired.zip",
        known_filename=artifact.original_filename,
        known_size=artifact.advertised_size,
        known_checksum=None,
    )


@pytest.mark.anyio
async def test_refresh_finds_same_artifact() -> None:
    fetcher = _MockHttpFetcher()
    adapter = OmadaGlobalAdapter(fetcher)
    product = next(
        product for product in _products(await _discover()) if product.model_raw == "ER605"
    )

    result = await adapter.refresh_artifact_url(_refresh_request(product))

    assert result == ArtifactUrlRefreshed(
        download_url="https://static.tp-link.com/example/ER605_V2.20_2.4.4.zip",
        url_expires_at=None,
    )


@pytest.mark.anyio
async def test_refresh_rejects_parent_identity_conflict() -> None:
    adapter = OmadaGlobalAdapter(_MockHttpFetcher())
    product = next(
        product for product in _products(await _discover()) if product.model_raw == "ER605"
    )
    request = _refresh_request(product)
    request = ArtifactRefreshRequest(
        **{**request.__dict__, "release_source_key": "derived:v1:wrong"}
    )

    result = await adapter.refresh_artifact_url(request)

    assert isinstance(result, ArtifactRefreshFailed)
    assert result.reason_code is RefreshFailureReason.IDENTITY_CONFLICT


@pytest.mark.anyio
async def test_refresh_reports_unknown_product_identity() -> None:
    adapter = OmadaGlobalAdapter(_MockHttpFetcher())
    product = next(
        product for product in _products(await _discover()) if product.model_raw == "ER605"
    )
    request = _refresh_request(product)
    request = ArtifactRefreshRequest(**{**request.__dict__, "product_source_key": "ER605"})

    result = await adapter.refresh_artifact_url(request)

    assert isinstance(result, ArtifactRefreshFailed)
    assert result.reason_code is RefreshFailureReason.IDENTITY_CONFLICT
