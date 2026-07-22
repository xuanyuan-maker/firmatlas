"""小米路由器 MiWiFi 适配器测试，全部使用 fixture 数据。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from firmatlas.adapters.events import (
    DiscoveredProduct,
    DiscoveryCompleted,
)
from firmatlas.adapters.miwifi_cn.adapter import (
    MiwifiCnAdapter,
    _classify,
    _parse_download_list,
    _parse_jsonp,
    _type_codes_for_entry,
)
from firmatlas.domain.model import ProductType
from firmatlas.infra.http_client import FetchError, FetchedText

_INDEX_URL = "https://www1.miwifi.com/statics/json/index.json"
_API_BASE = "https://api.miwifi.com/upgrade/log/latest"
_PAGE_URL = "https://www1.miwifi.com/miwifi_download.html"

# ----------  模拟 downloadList  ----------

_SAMPLE_DOWNLOAD_LIST = """
var downloadList = [
    {
        "title":"Xiaomi Router BE10000 Pro",
        "name":"Xiaomi Router BE10000 Pro 稳定版",
        "model":"RP04",
        "img":"statics/img/RP04.png",
        "config_code":false,
        "config_log":true,
        "rootLink":"https://www.xiaomi.cn/post/19134127",
    },
    {
        "title":"Xiaomi全屋路由BE3600 Pro",
        "name":"Xiaomi全屋路由BE3600 Pro 稳定版",
        "model":"RN01",
        "img":"statics/img/RN01.png",
        "config_code":false,
        "config_log":true,
        "rootLink":"https://www.xiaomi.cn/post/19134127",
    },
    {
        "title":"Xiaomi Router AX9000",
        "name":"Xiaomi Router AX9000 稳定版",
        "model":"RA70",
        "img":"statics/img/RA70.png",
        "config_code":false,
        "config_log":true,
        "rootLink":"https://www.xiaomi.cn/post/19154125",
    },
    {
        "title":"Xiaomi Router AX9000",
        "name":"Xiaomi Router AX9000 开发版",
        "model":"RA70",
        "img":"statics/img/RA70.png",
        "config_code":false,
        "config_log":true,
        "rootLink":"https://www.xiaomi.cn/post/19154125",
    },
];
"""

# ----------  模拟 API 响应  ----------

_SAMPLE_RESPONSES: dict[str, dict[str, Any]] = {
    "RP04STA": {
        "code": 0,
        "data": {
            "list": [
                {
                    "realType": "RP04STA",
                    "title": "Xiaomi Router BE10000 Pro",
                    "version": "1.0.89",
                    "url": "https://cdn.cnbj1.fds.api.mi-img.com/xiaoqiang/rom/rp04/miwifi_rp04_firmware_76b5c_1.0.89.bin",
                    "time": 1738800000000,
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
                    "version": "1.0.140",
                    "url": "https://cdn.cnbj1.fds.api.mi-img.com/miwifi/miwifi_ra70_all_develop_1.0.140.bin",
                    "time": 1636646400000,
                    "contents": "<p>Dev changelog content</p>",
                }
            ]
        },
    },
}

_JSONP_PREFIX = "jQuery123456789("
_JSONP_SUFFIX = ");"

_SAMPLE_RESPONSE_TEXTS = {
    code: f"{_JSONP_PREFIX}{json.dumps(data)}{_JSONP_SUFFIX}"
    for code, data in _SAMPLE_RESPONSES.items()
}


# =============================================================================
# Mock HttpFetcher
# =============================================================================


@dataclass
class _MockHttpFetcher:
    """回放测试数据的 HttpFetcher 替代。"""

    fail_codes: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    async def get_text(self, url: str, *, headers=None) -> FetchedText:  # noqa: ARG002
        self.calls.append(url)

        if url == _INDEX_URL:
            return FetchedText(url=url, status_code=200, text=_SAMPLE_DOWNLOAD_LIST)

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


class TestParseDownloadList:
    """index.json 中 downloadList 数组解析。"""

    def test_parse_basic(self) -> None:
        entries = _parse_download_list(_SAMPLE_DOWNLOAD_LIST)
        assert len(entries) == 4
        models = {e["model"] for e in entries}
        assert models == {"RP04", "RN01", "RA70"}

    def test_parse_empty(self) -> None:
        assert _parse_download_list("var downloadList = [];") == []

    def test_parse_missing(self) -> None:
        assert _parse_download_list("var bannerList = [];") == []


class TestTypeCodesForEntry:
    """根据 downloadList 条目生成 API typeList 码。"""

    def test_stable_only(self) -> None:
        codes = _type_codes_for_entry(
            {"model": "RP04", "name": "Xiaomi Router BE10000 Pro 稳定版"}
        )
        assert codes == ["RP04STA"]

    def test_dev_variant(self) -> None:
        codes = _type_codes_for_entry(
            {"model": "RA70", "name": "Xiaomi Router AX9000 开发版"}
        )
        assert codes == ["RA70STA", "RA70DEV"]

    def test_empty_model(self) -> None:
        assert _type_codes_for_entry({"model": "", "name": "test"}) == []


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
        _, ptype = _classify("Xiaomi Router BE10000 Pro")
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
    """discover() 应产出 3 个 model 对应的 3 个 Product。"""
    events = await _discover()
    products = _products(events)

    # RA70STA 和 RA70DEV 应合并为同一个 Product
    assert len(products) == 3
    assert sorted(p.model_normalized for p in products) == ["RA70", "RN01", "RP04"]


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
    assert release.release_date is not None

    # Artifact 层
    artifact = release.artifacts[0]
    assert artifact.artifact_type.value == "firmware"
    assert artifact.original_filename == "miwifi_rp04_firmware_76b5c_1.0.89.bin"
    assert artifact.download_url.startswith("https://cdn.cnbj1.fds.api.mi-img.com")
    assert artifact.media_type == "application/octet-stream"


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

    products = [e.product for e in events if isinstance(e, DiscoveredProduct)]
    assert len(products) >= 2  # RN01, RA70 仍正常产出

    completion = events[-1]
    assert isinstance(completion, DiscoveryCompleted)
    assert completion.is_complete is False
    assert any("RP04STA" in i.detail for i in completion.issues)


@pytest.mark.anyio
async def test_index_json_fetch_failure_is_catastrophic() -> None:
    """index.json 获取失败时直接产出 incomplete DiscoveryCompleted。"""

    @dataclass
    class _FailingHttp:
        async def get_text(self, url: str, *, headers=None) -> FetchedText:  # noqa: ARG002
            raise FetchError(url=url, status_code=503, detail="service unavailable")

    events = [event async for event in MiwifiCnAdapter(_FailingHttp()).discover()]
    assert len(events) == 1
    assert isinstance(events[0], DiscoveryCompleted)
    assert events[0].is_complete is False
    assert "503" in (events[0].incomplete_reason or "")
