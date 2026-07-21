"""Zyxel Global 适配器测试，全部回放脱敏 fixture。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
from firmatlas.adapters.zyxel_global.adapter import ZyxelGlobalAdapter
from firmatlas.domain.model import ProductType
from firmatlas.infra.http_client import FetchedText

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "zyxel-global"
AUTOCOMPLETE_PATH = "/global/en/search_api_autocomplete/product_list_by_model"
DOWNLOAD_PATH = "/global/en/support/download"


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


@dataclass
class _MockHttpFetcher:
    fail_model: str | None = None
    redirect_url: str | None = None
    page_overrides: dict[str, str] = field(default_factory=dict)

    async def get_text(self, url: str, *, headers=None) -> FetchedText:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.path == AUTOCOMPLETE_PATH:
            prefix = query["q"][0]
            entries = json.loads(_fixture("autocomplete-targets.json"))
            matching = [item for item in entries if item["value"].startswith(prefix)]
            text = json.dumps(matching)
        elif parsed.path == DOWNLOAD_PATH:
            model = query["model"][0]
            if model == self.fail_model:
                raise ConnectionError("simulated failure")
            text = self.page_overrides.get(
                model,
                {
                    "usg-flex-100h": _fixture("download-usg-flex-100h.html"),
                    "nwa50ax": _fixture("download-nwa50ax.html"),
                }[model],
            )
        else:
            raise AssertionError(f"unexpected URL: {url}")
        return FetchedText(
            url=self.redirect_url or url,
            status_code=200,
            text=text,
        )


async def _discover(fetcher: _MockHttpFetcher | None = None):
    adapter = ZyxelGlobalAdapter(fetcher or _MockHttpFetcher())
    return [event async for event in adapter.discover()]


def _products(events):
    return [event.product for event in events if isinstance(event, DiscoveredProduct)]


@pytest.mark.anyio
async def test_discover_whitelisted_products_and_complete() -> None:
    events = await _discover()
    products = _products(events)

    assert {product.model_raw for product in products} == {"USG FLEX 100H", "NWA50AX"}
    assert {product.model_raw: product.product_type for product in products} == {
        "USG FLEX 100H": ProductType.ROUTER,
        "NWA50AX": ProductType.WIRELESS_AP,
    }
    skipped = [event for event in events if isinstance(event, SkippedCandidate)]
    assert skipped == [
        SkippedCandidate(
            stage="product",
            reason_code=SkipReason.UNMAPPED_TYPE,
            detail="白名单外 Zyxel 产品共 1 项",
            source_url="https://www.zyxel.com/global/en/support/download",
            raw_hint="non_target_products",
        )
    ]
    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is True


@pytest.mark.anyio
async def test_candidate_tree_uses_unspecified_revision_and_all_versions() -> None:
    products = _products(await _discover())
    gateway = next(product for product in products if product.model_raw == "USG FLEX 100H")
    revision = gateway.hardware_revisions[0]

    assert gateway.source_key == "usg-flex-100h"
    assert gateway.source_url.endswith("?model=usg-flex-100h")
    assert revision.source_key == "__unspecified__"
    assert revision.normalized_revision == "unspecified"
    assert revision.revision_explicit is False
    assert [release.version_normalized for release in revision.releases] == [
        "1.36(ABXF.2)C0",
        "1.37(ABXF.1)C0",
        "1.38(ABXF.0)C0",
    ]
    latest = revision.releases[-1]
    assert latest.release_notes_url is not None
    assert latest.artifacts[0].source_key.startswith("url-path:usg_flex_100h/firmware/")
    assert latest.artifacts[0].media_type == "application/zip"


@pytest.mark.anyio
async def test_source_key_contract_is_stable() -> None:
    first = next(
        product for product in _products(await _discover()) if product.model_raw == "USG FLEX 100H"
    )
    second = next(
        product for product in _products(await _discover()) if product.model_raw == "USG FLEX 100H"
    )

    assert first.source_key == second.source_key
    assert first.hardware_revisions[0].releases == second.hardware_revisions[0].releases


@pytest.mark.anyio
async def test_unparsed_firmware_is_skipped_without_hiding_valid_versions() -> None:
    html = _fixture("download-usg-flex-100h.html").replace(
        "</section>",
        '<a href="https://download.zyxel.com/USG_FLEX_100H/firmware/legacy.zip">legacy</a>'
        "</section>",
    )
    events = await _discover(_MockHttpFetcher(page_overrides={"usg-flex-100h": html}))

    skipped = [event for event in events if isinstance(event, SkippedCandidate)]
    assert any(event.reason_code is SkipReason.PARSE_FAILED for event in skipped)
    gateway = next(product for product in _products(events) if product.model_raw == "USG FLEX 100H")
    assert len(gateway.hardware_revisions[0].releases) == 3
    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is True


@pytest.mark.anyio
async def test_target_detail_failure_makes_discovery_incomplete() -> None:
    events = await _discover(_MockHttpFetcher(fail_model="usg-flex-100h"))

    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is False
    assert "1 个" in (events[-1].incomplete_reason or "")


@pytest.mark.anyio
async def test_redirect_outside_source_is_incomplete() -> None:
    events = await _discover(_MockHttpFetcher(redirect_url="https://example.com/download"))

    assert events == [
        DiscoveryCompleted(
            is_complete=False,
            incomplete_reason=(
                "Zyxel 产品枚举失败: Zyxel 请求重定向到来源外: https://example.com/download"
            ),
            issues=(),
        )
    ]


def _refresh_request(product) -> ArtifactRefreshRequest:
    revision = product.hardware_revisions[0]
    release = revision.releases[-1]
    artifact = release.artifacts[0]
    return ArtifactRefreshRequest(
        product_source_key=product.source_key,
        hardware_revision_source_key=revision.source_key,
        release_source_key=release.source_key,
        artifact_source_key=artifact.source_key,
        stale_url="https://download.zyxel.com/expired.zip",
        known_filename=artifact.original_filename,
        known_size=None,
        known_checksum=None,
    )


@pytest.mark.anyio
async def test_refresh_finds_same_artifact() -> None:
    adapter = ZyxelGlobalAdapter(_MockHttpFetcher())
    product = next(
        product for product in _products(await _discover()) if product.model_raw == "USG FLEX 100H"
    )

    result = await adapter.refresh_artifact_url(_refresh_request(product))

    assert isinstance(result, ArtifactUrlRefreshed)
    assert result.download_url.endswith("USG%20FLEX%20100H_1.38(ABXF.0)C0.zip")


@pytest.mark.anyio
async def test_refresh_rejects_parent_identity_conflict() -> None:
    adapter = ZyxelGlobalAdapter(_MockHttpFetcher())
    product = next(
        product for product in _products(await _discover()) if product.model_raw == "USG FLEX 100H"
    )
    request = _refresh_request(product)
    request = ArtifactRefreshRequest(
        **{**request.__dict__, "release_source_key": "derived:v1:wrong"}
    )

    result = await adapter.refresh_artifact_url(request)

    assert isinstance(result, ArtifactRefreshFailed)
    assert result.reason_code is RefreshFailureReason.IDENTITY_CONFLICT
