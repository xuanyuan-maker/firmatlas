"""小米路由器 MiWiFi 适配器测试，全部使用 fixture 数据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from firmatlas.adapters.events import (
    DiscoveredProduct,
    DiscoveryCompleted,
)
from firmatlas.adapters.miwifi_cn.adapter import (
    MiwifiCnAdapter,
    _classify,
    _extract_type_codes,
    _model_from_type_code,
    _parse_jsonp,
)
from firmatlas.domain.model import ProductType
from firmatlas.infra.http_client import FetchError, FetchedText

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "miwifi-cn"

_PAGE_URL = "https://www1.miwifi.com/miwifi_download.html"
_API_BASE = "https://api.miwifi.com/upgrade/log/latest"

# 模拟 API 响应（仅 data.list[0] 字段，不含外部包装）
_SAMPLE_RESPONSES: dict[str, dict[str, Any]] = {
    "RP04STA": {
        "code": 0,
        "data": {
            "list": [
                {
                    "realType": "RP04STA",
                    "title": "Xiaomi Router BE10000 Pro",
                    "type": "Xiaomi Router BE10000 Pro (Stable)",
                    "version": "1.0.89",
                    "url": "https://cdn.cnbj1.fds.api.mi-img.com/xiaoqiang/rom/rp04/miwifi_rp04_firmware_76b5c_1.0.89.bin",
                    "time": 1738800000000,  # 2026-02-06
                    "contents": "<p>Changelog content</p>",
                }
            ]
        },
    },
    "RN01STA": {
        "code": 0,
        "data": {
            "list": [
                {
                    "realType": "RN01STA",
                    "title": "Xiaomi全屋路由BE3600 Pro",
                    "type": "Xiaomi全屋路由BE3600 Pro（稳定版）",
                    "version": "1.0.74",
                    "url": "https://cdn.cnbj1.fds.api.mi-img.com/xiaoqiang/rom/rn01/miwifi_rn01_firmware_6fbc2_1.0.74.bin",
                    "time": 1738108800000,
                    "contents": "<p>Changelog content</p>",
                }
            ]
        },
    },
    "RA70STA": {
        "code": 0,
        "data": {
            "list": [
                {
                    "realType": "RA70STA",
                    "title": "Xiaomi Router AX9000",
                    "type": "Xiaomi Router AX9000 (Stable)",
                    "version": "1.0.168",
                    "url": "https://cdn.cnbj1.fds.api.mi-img.com/xiaoqiang/rom/ra70/miwifi_ra70_firmware_cc424_1.0.168.bin",
                    "time": 1675814400000,
                    "contents": "<p>Changelog content</p>",
                }
            ]
        },
    },
    "RA70DEV": {
        "code": 0,
        "data": {
            "list": [
                {
                    "realType": "RA70DEV",
                    "title": "Xiaomi Router AX9000",
                    "type": "Xiaomi Router AX9000 (Dev)",
                    "version": "1.0.140",
                    "url": "https://cdn.cnbj1.fds.api.mi-img.com/miwifi/miwifi_ra70_all_develop_1.0.140.bin",
                    "time": 1636646400000,
                    "contents": "<p>Dev changelog content</p>",
                }
            ]
        },
    },
}

# ----------  JSONP 包装的 API 响应文本  ----------

_JSONP_PREFIX = "jQuery123456789("
_JSONP_SUFFIX = ");"

_SAMPLE_RESPONSE_TEXTS = {
    code: f"{_JSONP_PREFIX}{__import__('json').dumps(data)}{_JSONP_SUFFIX}"
    for code, data in _SAMPLE_RESPONSES.items()
}


# =============================================================================
# Mock HttpFetcher
# =============================================================================


@dataclass
class _MockHttpFetcher:
    """回放测试数据的 HttpFetcher 替代。

    fail_codes: 对列表中的 type_code 模拟异常（测试错误处理）。
    """

    fail_codes: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    async def get_text(self, url: str, *, headers=None) -> FetchedText:
        self.calls.append(url)

        if url == _PAGE_URL:
            html = (FIXTURE_DIR / "download.html").read_text(encoding="utf-8")
            return FetchedText(url=url, status_code=200, text=html)

        # 从 URL 提取 type_code：?typeList=XXX
        if url.startswith(_API_BASE):
            type_code = url.split("typeList=")[-1]
            if type_code in self.fail_codes:
                raise FetchError(url=url, status_code=500, detail="simulated failure")
            text = _SAMPLE_RESPONSE_TEXTS.get(type_code, "")
            return FetchedText(url=url, status_code=200, text=text)

        raise AssertionError(f"Unexpected URL: {url}")


async def _discover(fetcher: _MockHttpFetcher | None = None):
    adapter = MiwifiCnAdapter(fetcher or _MockHttpFetcher())
    return [event async for event in adapter.discover()]


def _products(events):
    return [event.product for event in events if isinstance(event, DiscoveredProduct)]


# =============================================================================
# 单元测试：纯函数
# =============================================================================


class TestExtractTypeCodes:
    """HTML 解析：提取 seelog 元素中的 typeList 码。"""

    def test_extract_basic(self) -> None:
        codes = _extract_type_codes(
            '<a class="seelog" data="RP04STA" href="javascript:;">更新日志</a>'
        )
        assert codes == ["RP04STA"]

    def test_extract_multiple_and_dedup(self) -> None:
        html = (
            '<a class="seelog" data="RP04STA" href="javascript:;">A</a>'
            '<a class="seelog" data="RA70DEV" href="javascript:;">B</a>'
            '<a class="seelog" data="RP04STA" href="javascript:;">C</a>'
        )
        codes = _extract_type_codes(html)
        assert codes == ["RP04STA", "RA70DEV"]

    def test_extract_clr_seelog(self) -> None:
        """开发版使用 class="clr seelog"。"""
        codes = _extract_type_codes(
            '<a class="clr seelog" data="RA70DEV" href="javascript:;">更新日志</a>'
        )
        assert codes == ["RA70DEV"]

    def test_extract_empty(self) -> None:
        assert _extract_type_codes("<html></html>") == []


class TestModelFromTypeCode:
    """从 typeList 码提取 model 码。"""

    @pytest.mark.parametrize(
        ("type_code", "expected"),
        [
            ("RP04STA", "RP04"),
            ("RA70DEV", "RA70"),
            ("RD03V2STA", "RD03V2"),
            ("R4ACSTA-220", "R4AC"),
            ("R1CMSTA", "R1CM"),
            ("RM1800STA", "RM1800"),
            ("D01STA", "D01"),
            ("RA80V2STA", "RA80V2"),
        ],
    )
    def test_valid(self, type_code: str, expected: str) -> None:
        assert _model_from_type_code(type_code) == expected

    def test_invalid_returns_none(self) -> None:
        assert _model_from_type_code("WiFiPC") is None


class TestParseJsonp:
    """JSONP / JSON 解析兼容性。"""

    def test_pure_json(self) -> None:
        result = _parse_jsonp('{"code": 0, "data": {"list": [{"x": 1}]}}')
        assert result is not None
        assert result["code"] == 0

    def test_jsonp_wrapped(self) -> None:
        result = _parse_jsonp(
            'jQuery123456789({"code": 0, "data": {"list": [{"x": 1}]}});'
        )
        assert result is not None
        assert result["code"] == 0

    def test_invalid(self) -> None:
        assert _parse_jsonp("not json") is None


class TestClassify:
    """产品分类逻辑。"""

    def test_plain_router(self) -> None:
        family, ptype = _classify("Xiaomi Router BE10000 Pro")
        assert ptype == ProductType.ROUTER

    def test_mesh_router_chinese(self) -> None:
        _, ptype = _classify("Xiaomi 全屋路由 BE3600 Pro")
        assert ptype == ProductType.MESH_ROUTER

    def test_mesh_router_english(self) -> None:
        _, ptype = _classify("Xiaomi HomeWiFi Mesh Router")
        assert ptype == ProductType.MESH_ROUTER


# =============================================================================
# 集成测试：适配器 discover()
# =============================================================================


@pytest.mark.anyio
async def test_discover_yields_products() -> None:
    """discover() 应产出 4 个 type_code 对应的 3 个 Product。"""
    events = await _discover()
    products = _products(events)

    # RA70STA 和 RA70DEV 应合并为同一个 Product
    assert len(products) == 3
    assert [p.model_normalized for p in products] == ["RA70", "RN01", "RP04"]


@pytest.mark.anyio
async def test_mesh_classification_applied() -> None:
    """含 "全屋路由" 的产品应分类为 mesh_router。"""
    products = _products(await _discover())
    mesh = [p for p in products if "全屋" in p.display_name]
    assert len(mesh) == 1
    assert mesh[0].product_type == ProductType.MESH_ROUTER


@pytest.mark.anyio
async def test_stable_and_dev_merged_into_one_product() -> None:
    """同一 model 的稳定版和开发版应为同一 Product 的两个 Release。"""
    products = _products(await _discover())
    ax9000 = [p for p in products if p.model_normalized == "RA70"][0]

    assert len(ax9000.hardware_revisions) == 1
    releases = ax9000.hardware_revisions[0].releases
    assert len(releases) == 2  # stable + dev


@pytest.mark.anyio
async def test_release_has_variant_label() -> None:
    """Release title 应包含 "稳定版" 或 "开发版"。"""
    products = _products(await _discover())
    ax9000 = [p for p in products if p.model_normalized == "RA70"][0]
    titles = {r.title for r in ax9000.hardware_revisions[0].releases}
    assert "Xiaomi Router AX9000 稳定版" in titles
    assert "Xiaomi Router AX9000 开发版" in titles


@pytest.mark.anyio
async def test_candidate_tree_structure() -> None:
    """验证 ProductCandidate 树各层字段。"""
    products = _products(await _discover())
    rp04 = [p for p in products if p.model_normalized == "RP04"][0]

    # Product 层
    assert rp04.source_key == "miwifi:RP04"
    assert rp04.display_name == "Xiaomi Router BE10000 Pro"
    assert rp04.source_url == _PAGE_URL
    assert rp04.product_family.value == "router"
    assert rp04.product_type == ProductType.ROUTER

    # Hardware 层
    revision = rp04.hardware_revisions[0]
    assert revision.source_key == "__unspecified__"
    assert revision.normalized_revision == "unspecified"
    assert revision.revision_explicit is False

    # Release 层
    release = revision.releases[0]
    assert release.version_raw == "1.0.89"
    assert release.version_normalized == "1.0.89"
    assert release.release_date is not None
    assert release.release_notes == "<p>Changelog content</p>"

    # Artifact 层
    artifact = release.artifacts[0]
    assert artifact.artifact_type.value == "firmware"
    assert artifact.original_filename == "miwifi_rp04_firmware_76b5c_1.0.89.bin"
    assert artifact.download_url.startswith("https://cdn.cnbj1.fds.api.mi-img.com")
    assert artifact.media_type == "application/octet-stream"
    assert artifact.official_checksum is None


@pytest.mark.anyio
async def test_source_key_stability() -> None:
    """source_key 在多次运行中保持稳定。"""
    first = _products(await _discover())[0]
    second = _products(await _discover())[0]

    assert first.source_key == second.source_key
    first_rev = first.hardware_revisions[0]
    second_rev = second.hardware_revisions[0]
    assert first_rev.source_key == second_rev.source_key
    assert first_rev.releases[0].source_key == second_rev.releases[0].source_key
    assert (
        first_rev.releases[0].artifacts[0].source_key
        == second_rev.releases[0].artifacts[0].source_key
    )


@pytest.mark.anyio
async def test_discovery_completed_last_event() -> None:
    """DiscoveryCompleted 必须是最后一个事件。"""
    events = await _discover()
    assert isinstance(events[-1], DiscoveryCompleted)
    assert events[-1].is_complete is True


@pytest.mark.anyio
async def test_api_failure_makes_incomplete() -> None:
    """单个 API 失败不应中断整体流程，但应标记 incomplete。"""
    fetcher = _MockHttpFetcher(fail_codes={"RP04STA"})
    events = [event async for event in MiwifiCnAdapter(fetcher).discover()]

    # 即使一个 API 失败，其余产品仍产出
    products = [e.product for e in events if isinstance(e, DiscoveredProduct)]
    assert len(products) >= 2  # RN01, RA70 仍正常产出

    completion = events[-1]
    assert isinstance(completion, DiscoveryCompleted)
    assert completion.is_complete is False
    assert completion.issues
    assert any("RP04STA" in i.detail for i in completion.issues)


@pytest.mark.anyio
async def test_html_fetch_failure_is_catastrophic() -> None:
    """HTML 获取失败时直接产出 incomplete DiscoveryCompleted。"""

    @dataclass
    class _FailingHttp:
        async def get_text(self, url: str, *, headers=None) -> FetchedText:
            raise FetchError(url=url, status_code=503, detail="service unavailable")

    events = [event async for event in MiwifiCnAdapter(_FailingHttp()).discover()]
    assert len(events) == 1
    assert isinstance(events[0], DiscoveryCompleted)
    assert events[0].is_complete is False
    assert "503" in (events[0].incomplete_reason or "")
