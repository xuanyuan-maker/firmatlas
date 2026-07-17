"""tp-link-cn 适配器测试（基于 fixture，AC-31）。

所有测试使用 tests/fixtures/tp-link-cn/ 下的脱敏 API 响应，
不访问真实网站。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from firmatlas.adapters.events import (
    DiscoveredProduct,
    DiscoveryCompleted,
    SkippedCandidate,
    SkipReason,
)
from firmatlas.adapters.tplink_cn.adapter import TplinkCnAdapter
from firmatlas.infra.http_client import FetchedJson

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tp-link-cn"


# -- Mock HttpFetcher -------------------------------------------------------


@dataclass
class _MockResponse:
    status_code: int = 200
    data: Any = None


class _MockHttpFetcher:
    """回放 fixture 的虚拟 HttpFetcher，不发送真实网络请求。"""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, Any]] = []

    async def post_json(self, url: str, body: Any, *, headers=None) -> FetchedJson:
        self.calls.append((url, body))
        # 用 body 中的 productClassIds 和 pageIndex 作为 key
        cid = (body.get("productClassIds") or ["unknown"])[0]
        key = f"{cid}_p{body.get('pageIndex', 1)}"
        data = self._responses.get(key, {"result": {"total": 0, "collection": []}})
        return FetchedJson(url=url, status_code=200, data=data)


def _load_fixture(name: str) -> dict:
    path = FIXTURE_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


# -- 测试：完整 discover 流程 ------------------------------------------------


@pytest.mark.anyio
async def test_discover_router_class_2502() -> None:
    """品类 2502 的第一页记录能正确解析分组为产品树。"""
    fixture = _load_fixture("search_2502.json")

    # 构造 mock：对每个粗筛品类，第一页返回 fixture 数据，
    # 其余页返回空（只测单页）。
    responses: dict[str, Any] = {}
    from firmatlas.adapters.tplink_cn.classification import candidate_product_class_ids

    for cid in candidate_product_class_ids():
        if cid == "2502":
            responses[f"{cid}_p1"] = fixture
        else:
            # 非 2502 品类返回空结果（跳过）
            responses[f"{cid}_p1"] = {"result": {"total": 0, "collection": []}}

    mock_http = _MockHttpFetcher(responses)
    adapter = TplinkCnAdapter(mock_http)

    events = [e async for e in adapter.discover()]

    # 应该有产品事件 + 跳过事件 + 完成事件
    products = [e for e in events if isinstance(e, DiscoveredProduct)]
    completed = [e for e in events if isinstance(e, DiscoveryCompleted)]

    assert len(completed) == 1
    assert completed[0].is_complete is True

    # 2502 fixture 中有 5 条记录，应该都是路由器
    # 验证每个产品的基本结构
    for p_event in products:
        p = p_event.product
        assert p.product_family is not None
        assert p.product_type is not None
        assert p.source_key  # source_key 不能为空
        assert p.model_normalized
        assert len(p.hardware_revisions) > 0
        for hw in p.hardware_revisions:
            assert hw.source_key
            assert hw.normalized_revision
            assert hw.revision_explicit is True
            for rel in hw.releases:
                assert rel.source_key
                assert rel.version_raw
                assert len(rel.artifacts) > 0
                for art in rel.artifacts:
                    assert art.source_key
                    assert art.download_url.startswith("https://")


@pytest.mark.anyio
async def test_discover_camera_class_2549() -> None:
    """品类 2549 的摄像机记录正确分类。"""
    fixture = _load_fixture("search_2549.json")

    responses: dict[str, Any] = {}
    from firmatlas.adapters.tplink_cn.classification import candidate_product_class_ids

    for cid in candidate_product_class_ids():
        if cid == "2549":
            responses[f"{cid}_p1"] = fixture
        else:
            responses[f"{cid}_p1"] = {"result": {"total": 0, "collection": []}}

    mock_http = _MockHttpFetcher(responses)
    adapter = TplinkCnAdapter(mock_http)

    events = [e async for e in adapter.discover()]

    products = [e for e in events if isinstance(e, DiscoveredProduct)]

    # 所有 2549 下的产品应该都是 CAMERA
    for p_event in products:
        assert p_event.product.product_family.value == "camera", (
            f"Expected camera, got {p_event.product.product_family} "
            f"for {p_event.product.model_raw}"
        )


# -- 测试：source_key 稳定性 ------------------------------------------------


@pytest.mark.anyio
async def test_source_keys_are_stable() -> None:
    """同一 fixture 两次 discover 产出的 source_key 一致。"""
    fixture = _load_fixture("search_2502.json")

    from firmatlas.adapters.tplink_cn.classification import candidate_product_class_ids

    async def collect_keys() -> dict[str, set[str]]:
        responses: dict[str, Any] = {}
        for cid in candidate_product_class_ids():
            responses[f"{cid}_p1"] = fixture if cid == "2502" else {
                "result": {"total": 0, "collection": []}
            }
        adapter = TplinkCnAdapter(_MockHttpFetcher(responses))
        keys: dict[str, set[str]] = {
            "product": set(),
            "hw": set(),
            "release": set(),
            "artifact": set(),
        }
        async for e in adapter.discover():
            if isinstance(e, DiscoveredProduct):
                p = e.product
                keys["product"].add(p.source_key)
                for hw in p.hardware_revisions:
                    keys["hw"].add(hw.source_key)
                    for rel in hw.releases:
                        keys["release"].add(rel.source_key)
                        for art in rel.artifacts:
                            keys["artifact"].add(art.source_key)
        return keys

    keys1 = await collect_keys()
    keys2 = await collect_keys()

    # source_key 必须稳定：两次运行结果一致
    assert keys1 == keys2


# -- 测试：SkippedCandidate 类型正确 -----------------------------------------


def _make_search_response(
    records: list[dict],
    total: int | None = None,
) -> dict:
    return {"result": {"total": total or len(records), "collection": records}}


@pytest.mark.anyio
async def test_non_target_products_are_skipped() -> None:
    """classify() 返回 None 的记录被产出为 SkippedCandidate（AC-08）。"""
    # 使用含"易展"的型号：classify() 发现易展即返回 None（本轮宁缺勿错策略）
    mock_data = _make_search_response(
        [
            {
                "id": 99999901,
                "category": "SOFTWARE",
                "title": "TL-XVR5400G-5G易展版 V1.0升级软件20260101_1.0.0",
                "url": "https://media.tp-link.com.cn/software/test.zip",
                "format": "zip",
                "softwareType": "UPGRADE_SOFT",
                "materialCenterState": "SHOW",
                "docSize": 1000,
                "modifiedTime": 1700000000000,
            }
        ]
    )

    responses: dict[str, Any] = {}
    from firmatlas.adapters.tplink_cn.classification import candidate_product_class_ids

    for cid in candidate_product_class_ids():
        if cid == "2502":
            responses[f"{cid}_p1"] = mock_data
        else:
            responses[f"{cid}_p1"] = {"result": {"total": 0, "collection": []}}

    adapter = TplinkCnAdapter(_MockHttpFetcher(responses))
    events = [e async for e in adapter.discover()]

    skipped = [e for e in events if isinstance(e, SkippedCandidate)]
    products = [e for e in events if isinstance(e, DiscoveredProduct)]

    # 易展记录应被跳过
    assert len(skipped) >= 1
    assert any(s.reason_code == SkipReason.UNMAPPED_TYPE for s in skipped)

    # 没有产品被产出（唯一记录是易展→跳过）
    assert len(products) == 0


@pytest.mark.anyio
async def test_unparseable_title_is_skipped() -> None:
    """无法解析的 title 产出 PARSE_FAILED 跳过事件。"""
    mock_data = _make_search_response(
        [
            {
                "id": 999999,
                "category": "SOFTWARE",
                "title": "这不是一个合法的标题格式",
                "url": "https://media.tp-link.com.cn/software/test.zip",
                "format": "zip",
                "softwareType": "UPGRADE_SOFT",
                "materialCenterState": "SHOW",
                "docSize": 100,
                "modifiedTime": 1700000000000,
            }
        ]
    )

    responses: dict[str, Any] = {}
    from firmatlas.adapters.tplink_cn.classification import candidate_product_class_ids

    for cid in candidate_product_class_ids():
        responses[f"{cid}_p1"] = mock_data if cid == "2502" else {
            "result": {"total": 0, "collection": []}
        }

    adapter = TplinkCnAdapter(_MockHttpFetcher(responses))
    events = [e async for e in adapter.discover()]

    skipped = [e for e in events if isinstance(e, SkippedCandidate)]
    assert any(s.reason_code == SkipReason.PARSE_FAILED for s in skipped)


# -- 测试：docSize 单位转换 -------------------------------------------------


@pytest.mark.anyio
async def test_doc_size_converted_to_bytes() -> None:
    """docSize（KB）正确转为字节。"""
    mock_data = _make_search_response(
        [
            {
                "id": 1784097617201826,
                "category": "SOFTWARE",
                "title": "TL-R5009PE-AC V1.0升级软件20260108_1.0.30",
                "url": "https://media.tp-link.com.cn/software/test.zip",
                "format": "zip",
                "softwareType": "UPGRADE_SOFT",
                "materialCenterState": "SHOW",
                "docSize": 15074,  # KB
                "modifiedTime": 1784097620182,
            }
        ]
    )

    responses: dict[str, Any] = {}
    from firmatlas.adapters.tplink_cn.classification import candidate_product_class_ids

    for cid in candidate_product_class_ids():
        responses[f"{cid}_p1"] = mock_data if cid == "2502" else {
            "result": {"total": 0, "collection": []}
        }

    adapter = TplinkCnAdapter(_MockHttpFetcher(responses))
    events = [e async for e in adapter.discover()]

    products = [e for e in events if isinstance(e, DiscoveredProduct)]
    assert len(products) == 1
    art = products[0].product.hardware_revisions[0].releases[0].artifacts[0]
    assert art.advertised_size == 15074 * 1024
