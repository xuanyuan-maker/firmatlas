"""ruijie-cn 适配器工具函数测试。"""

from __future__ import annotations

import base64

from firmatlas.adapters.ruijie_cn.adapter import (
    _GOODS_ITEM_RE,
    _GOODS_SPAN_RE,
    _PRODUCT_LINK_RE,
    _classify_product_type,
    _extract_model,
    _normalize_version,
    _parse_date,
    _parse_md5_base64,
    _url_slug,
)
from firmatlas.domain.model import ProductType


class TestUrlSlug:
    def test_standard_product_url(self) -> None:
        assert _url_slug("https://www.ruijie.com.cn/fw/rj-cp-rg-rsr-x1/") == "rg-rsr-x1"

    def test_without_trailing_slash(self) -> None:
        assert _url_slug("https://www.ruijie.com.cn/fw/rj-cp-77x") == "77x"

    def test_no_rj_cp_prefix(self) -> None:
        assert _url_slug("https://www.ruijie.com.cn/fw/rj-cp/rsr10o") == "rsr10o"


class TestExtractModel:
    def test_standard_format(self) -> None:
        assert _extract_model("RG-RSR20-X1系列接入路由器") == "RG-RSR20-X1"

    def test_with_suffix(self) -> None:
        assert _extract_model("RG-RSR20-X1系列接入路由器软件下载-锐捷网络") == "RG-RSR20-X1"

    def test_no_series_suffix(self) -> None:
        assert _extract_model("RG-NPE50路由器") == "RG-NPE50"

    def test_fallback(self) -> None:
        result = _extract_model("锐捷全新SD-WAN解决方案")
        assert result == "锐捷全新SD-WAN解决方案"


class TestClassifyProductType:
    def test_default_router(self) -> None:
        assert _classify_product_type("路由器X", ProductType.ROUTER) == ProductType.ROUTER

    def test_default_wireless(self) -> None:
        assert _classify_product_type("AP-XXX", ProductType.WIRELESS_AP) == ProductType.WIRELESS_AP

    def test_mobile_router_4g(self) -> None:
        assert (
            _classify_product_type("RG-RSR10-01G系列4G路由器", ProductType.ROUTER)
            == ProductType.CELLULAR_CPE
        )

    def test_mobile_router_5g(self) -> None:
        assert (
            _classify_product_type("RG-RSR860-NR系列5G路由器", ProductType.ROUTER)
            == ProductType.CELLULAR_CPE
        )

    def test_mobile_router_explicit(self) -> None:
        assert (
            _classify_product_type("RG-RSR820系列移动路由器", ProductType.ROUTER)
            == ProductType.CELLULAR_CPE
        )


class TestNormalizeVersion:
    def test_rgos_format(self) -> None:
        assert _normalize_version("RGOS 11.0(5)B9P30") == "11.0.5.b9p30"

    def test_simple_version(self) -> None:
        assert _normalize_version("1.2.3") == "1.2.3"

    def test_empty(self) -> None:
        assert _normalize_version("") is None
        assert _normalize_version("  ") is None


class TestParseDate:
    def test_iso_format(self) -> None:
        result = _parse_date("2025-01-15")
        assert result is not None
        assert result.year == 2025
        assert result.month == 1
        assert result.day == 15

    def test_slash_format(self) -> None:
        result = _parse_date("2025/01/15")
        assert result is not None
        assert result.year == 2025

    def test_invalid(self) -> None:
        assert _parse_date("not-a-date") is None
        assert _parse_date("") is None
        assert _parse_date(None) is None


class TestParseMd5Base64:
    def test_valid_md5(self) -> None:
        # Base64 of "test" is "dGVzdA==", hex is "74657374"
        encoded = base64.b64encode(b"test").decode()
        result = _parse_md5_base64(encoded)
        assert result is not None
        assert result.algorithm == "md5"
        assert result.value == "74657374"

    def test_empty(self) -> None:
        assert _parse_md5_base64("") is None
        assert _parse_md5_base64(None) is None

    def test_invalid_base64(self) -> None:
        assert _parse_md5_base64("!!!invalid!!!") is None


class TestProductLinkRegex:
    def test_extract_single_link(self) -> None:
        html = '<a href="/fw/rj-cp-rg-rsr-x1/">RG-RSR20-X1</a>'
        matches = _PRODUCT_LINK_RE.findall(html)
        assert matches == ["/fw/rj-cp-rg-rsr-x1/"]

    def test_extract_multiple_links(self) -> None:
        html = """
        <a href="/fw/rj-cp-77x/">Product A</a>
        <a href="/fw/rj-cp-820/">Product B</a>
        <a href="/fw/rj-cp-rsr10/">Product C</a>
        """
        matches = _PRODUCT_LINK_RE.findall(html)
        assert len(matches) == 3


class TestGoodsSpanRegex:
    """新版产品页 <span goodsId="..."> 提取。"""

    def test_extract_product_line_goods_id(self) -> None:
        html = '<span data-id="abc-123" goodsId="1777604717224923138">RG-EG-E系列新一代智能安全网关</span>'
        match = _GOODS_SPAN_RE.search(html)
        assert match is not None
        assert match.group(1) == "1777604717224923138"
        assert match.group(2) == "RG-EG-E系列新一代智能安全网关"

    def test_extract_simple_span(self) -> None:
        html = '<span goodsId="123456">X60 PRO</span>'
        match = _GOODS_SPAN_RE.search(html)
        assert match is not None
        assert match.group(1) == "123456"
        assert match.group(2) == "X60 PRO"

    def test_no_match(self) -> None:
        html = "<span>No goodsId here</span>"
        assert _GOODS_SPAN_RE.search(html) is None


class TestGoodsItemRegex:
    """新版产品页 <div class="item" goodsId="..."> 提取。"""

    def test_extract_single_model(self) -> None:
        html = '<div class="item" style="cursor:pointer;" goodsId="2048582900152905729"> RG-EG-E3100-P </div>'
        matches = _GOODS_ITEM_RE.findall(html)
        assert len(matches) == 1
        assert matches[0] == ("2048582900152905729", "RG-EG-E3100-P")

    def test_extract_multiple_models(self) -> None:
        html = """
        <div class="item" style="cursor:pointer;" goodsId="111"> Model-A </div>
        <div class="item" style="cursor:pointer;" goodsId="222"> Model-B </div>
        <div class="item" style="cursor:pointer;" goodsId="333"> Model-C </div>
        """
        matches = _GOODS_ITEM_RE.findall(html)
        assert len(matches) == 3
        assert matches[0] == ("111", "Model-A")
        assert matches[2] == ("333", "Model-C")

    def test_skips_hidden_item(self) -> None:
        """display:none 的 item 应该被跳过（goodsId 相同但 name 为空）。"""
        html = '<div class="item" goodsId="999" style="display:none;cursor:pointer;"> </div>'
        matches = _GOODS_ITEM_RE.findall(html)
        # 空白 name 会被 findall 捕获，但后续 .strip() 会清空
        # 正则本身不跳过，适配器会过滤空 name
        assert len(matches) == 1

    def test_no_match_on_non_item_div(self) -> None:
        html = '<div class="other" goodsId="123">Not an item</div>'
        matches = _GOODS_ITEM_RE.findall(html)
        assert len(matches) == 0
