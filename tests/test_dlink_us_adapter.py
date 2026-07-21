"""D-Link 美国站发现适配器测试，所有网络响应来自固定 fixture。"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from firmatlas.adapters.dlink_us.adapter import DlinkUsAdapter
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
from firmatlas.infra.http_client import FetchedText

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dlink-us"
INDEX_URL = "https://support.dlink.com/resource/PRODUCTS/"


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _responses() -> dict[str, str]:
    return {
        INDEX_URL: _fixture("adapter-products-index.html"),
        "https://support.dlink.com/resource/PRODUCTS/DCS-8302LH/": _fixture(
            "product-dcs-8302lh.html"
        ),
        "https://support.dlink.com/resource/products/DCS-8302LH/REVA/": _fixture(
            "revision-dcs-8302lh-reva.html"
        ),
        "https://support.dlink.com/resource/products/DCS-8302LH/REVA/FIRMWARE/": _fixture(
            "firmware-dcs-8302lh-reva.html"
        ),
        "https://support.dlink.com/resource/PRODUCTS/DIR-X5460/": _fixture(
            "product-dir-x5460.html"
        ),
        "https://support.dlink.com/resource/products/DIR-X5460/FIRMWARE/": _fixture(
            "firmware-dir-x5460.html"
        ),
        "https://support.dlink.com/resource/PRODUCTS/DSR-250V2/": _fixture(
            "product-dsr-250v2.html"
        ),
        "https://support.dlink.com/resource/products/DSR-250V2/REVA/": _fixture(
            "revision-dsr-250v2-reva.html"
        ),
        "https://support.dlink.com/resource/products/DSR-250V2/REVB/": _fixture(
            "firmware-dsr-250v2-revb.html"
        ),
    }


@dataclass
class _MockHttpFetcher:
    responses: dict[str, str]
    fail_url: str | None = None
    redirect_url: str | None = None

    async def get_text(self, url: str, *, headers=None) -> FetchedText:
        if self.fail_url is not None and url.casefold() == self.fail_url.casefold():
            raise ConnectionError("simulated failure")
        response_url = next(
            (candidate for candidate in self.responses if candidate.casefold() == url.casefold()),
            None,
        )
        if response_url is None:
            raise AssertionError(f"unexpected URL: {url}")
        final_url = self.redirect_url if url == INDEX_URL and self.redirect_url else url
        return FetchedText(url=final_url, status_code=200, text=self.responses[response_url])


async def _discover(fetcher: _MockHttpFetcher | None = None):
    adapter = DlinkUsAdapter(fetcher or _MockHttpFetcher(_responses()))
    return [event async for event in adapter.discover()]


def _products(events):
    return [event.product for event in events if isinstance(event, DiscoveredProduct)]


@pytest.mark.anyio
async def test_discover_only_whitelisted_products_and_complete() -> None:
    events = await _discover()
    products = _products(events)

    assert {product.model_raw for product in products} == {
        "DCS-8302LH",
        "DIR-X5460",
        "DSR-250V2",
    }
    assert {product.model_raw: product.product_type for product in products} == {
        "DCS-8302LH": ProductType.CAMERA,
        "DIR-X5460": ProductType.ROUTER,
        "DSR-250V2": ProductType.ROUTER,
    }
    skipped = [event for event in events if isinstance(event, SkippedCandidate)]
    assert skipped == [
        SkippedCandidate(
            stage="product",
            reason_code=SkipReason.UNMAPPED_TYPE,
            detail="白名单外产品或非目录条目共 2 项",
            source_url=INDEX_URL,
            raw_hint="non_target_products",
        )
    ]
    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is True


@pytest.mark.anyio
async def test_discover_hardware_revisions_and_firmware_files() -> None:
    products = _products(await _discover())
    camera = next(product for product in products if product.model_raw == "DCS-8302LH")
    router = next(product for product in products if product.model_raw == "DIR-X5460")
    gateway = next(product for product in products if product.model_raw == "DSR-250V2")

    assert [
        (revision.raw_revision, revision.normalized_revision)
        for revision in camera.hardware_revisions
    ] == [("A", "A")]
    assert [release.version_normalized for release in camera.hardware_revisions[0].releases] == [
        "1.00.05"
    ]
    assert router.hardware_revisions[0].normalized_revision == "A"
    assert len(router.hardware_revisions[0].releases) == 2
    assert {release.version_normalized for release in router.hardware_revisions[0].releases} == {
        "1.20.B01",
        None,
    }
    assert [revision.normalized_revision for revision in gateway.hardware_revisions] == ["B"]
    assert len(gateway.hardware_revisions[0].releases) == 2
    assert all(
        not release.title.lower().endswith(".pdf")
        for release in gateway.hardware_revisions[0].releases
    )


@pytest.mark.anyio
async def test_unparsed_version_is_kept_and_reported() -> None:
    events = await _discover()
    completed = events[-1]

    assert isinstance(completed, DiscoveryCompleted)
    assert completed.is_complete is True
    issue = next(issue for issue in completed.issues if issue.code == "version_unparsed")
    assert "DIR-X5460_A1_FW120B01.rar" in issue.detail


@pytest.mark.anyio
async def test_source_key_contract_is_stable() -> None:
    first = next(
        product for product in _products(await _discover()) if product.model_raw == "DCS-8302LH"
    )
    second = next(
        product for product in _products(await _discover()) if product.model_raw == "DCS-8302LH"
    )
    revision = first.hardware_revisions[0]
    release = revision.releases[0]
    artifact = release.artifacts[0]

    assert first.source_key == second.source_key
    assert first.source_key == "url-path:resource/products/dcs-8302lh"
    assert revision.source_key == "rev:a"
    assert release.source_key == (
        "derived:v1:2407c79b31bd0219b536937abbe7a19b186ac878ec77b4555133dc86b8769482"
    )
    assert artifact.source_key == (
        "url-path:resource/products/dcs-8302lh/reva/firmware/dcs-8302lh_reva_firmware_v1.00.05.bin"
    )


@pytest.mark.anyio
async def test_product_directory_failure_makes_discovery_incomplete() -> None:
    failed_url = "https://support.dlink.com/resource/PRODUCTS/DIR-X5460/"
    events = await _discover(_MockHttpFetcher(_responses(), fail_url=failed_url))

    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is False
    assert "1 个" in (events[-1].incomplete_reason or "")


@pytest.mark.anyio
async def test_index_redirect_outside_source_is_incomplete() -> None:
    events = await _discover(
        _MockHttpFetcher(
            _responses(),
            redirect_url="https://legacy.us.dlink.com/resource/PRODUCTS/",
        )
    )

    assert events == [
        DiscoveryCompleted(
            is_complete=False,
            incomplete_reason=(
                "D-Link 产品目录重定向到来源外: https://legacy.us.dlink.com/resource/PRODUCTS/"
            ),
            issues=(),
        )
    ]


def _refresh_request(**overrides) -> ArtifactRefreshRequest:
    fields = {
        "product_source_key": "url-path:resource/products/dcs-8302lh",
        "hardware_revision_source_key": "rev:a",
        "release_source_key": "",
        "artifact_source_key": (
            "url-path:resource/products/dcs-8302lh/reva/firmware/"
            "dcs-8302lh_reva_firmware_v1.00.05.bin"
        ),
        "stale_url": "https://support.dlink.com/expired.bin",
        "known_filename": "DCS-8302LH_REVA_FIRMWARE_v1.00.05.bin",
        "known_size": None,
        "known_checksum": None,
    }
    fields.update(overrides)
    return ArtifactRefreshRequest(**fields)


@pytest.mark.anyio
async def test_refresh_finds_same_artifact() -> None:
    adapter = DlinkUsAdapter(_MockHttpFetcher(_responses()))
    product = next(
        product for product in _products(await _discover()) if product.model_raw == "DCS-8302LH"
    )
    release_key = product.hardware_revisions[0].releases[0].source_key

    result = await adapter.refresh_artifact_url(_refresh_request(release_source_key=release_key))

    assert result == ArtifactUrlRefreshed(
        download_url=(
            "https://support.dlink.com/resource/products/DCS-8302LH/REVA/FIRMWARE/"
            "DCS-8302LH_REVA_FIRMWARE_v1.00.05.bin"
        ),
        url_expires_at=None,
    )


@pytest.mark.anyio
async def test_refresh_rejects_parent_identity_conflict() -> None:
    adapter = DlinkUsAdapter(_MockHttpFetcher(_responses()))

    result = await adapter.refresh_artifact_url(
        _refresh_request(release_source_key="derived:v1:wrong")
    )

    assert isinstance(result, ArtifactRefreshFailed)
    assert result.reason_code is RefreshFailureReason.IDENTITY_CONFLICT


@pytest.mark.anyio
async def test_refresh_reports_missing_artifact() -> None:
    adapter = DlinkUsAdapter(_MockHttpFetcher(_responses()))

    result = await adapter.refresh_artifact_url(
        _refresh_request(
            release_source_key="derived:v1:missing",
            artifact_source_key="url-path:resource/products/dcs-8302lh/missing.bin",
        )
    )

    assert isinstance(result, ArtifactRefreshFailed)
    assert result.reason_code is RefreshFailureReason.NOT_FOUND
