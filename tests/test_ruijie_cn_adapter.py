"""ruijie-cn 适配器工具函数测试。"""

from __future__ import annotations

import base64
from pathlib import Path

from firmatlas.adapters.ruijie_cn.adapter import (
    _classify_product_type,
    _extract_model,
    _extract_version_name,
    _normalize_version,
    _parse_date,
    _parse_md5_base64,
    _url_slug,
    _ROW_HREF_RE,
    _PRODUCT_LINK_RE,
)
from firmatlas.domain.model import ProductType

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ruijie-cn"


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


class TestRowHrefRegex:
    def test_extract_vid(self) -> None:
        html = """<tr onclick="rowHref('291266', 'RGOS 11.0(5)B9P30')">"""
        matches = _ROW_HREF_RE.findall(html)
        assert matches == ["291266"]

    def test_double_quotes(self) -> None:
        html = '''<tr onclick="rowHref('290063', 'Version 1.0')">'''
        matches = _ROW_HREF_RE.findall(html)
        assert matches == ["290063"]

    def test_multiple_vids(self) -> None:
        html = """
        <tr onclick="rowHref('111', 'v1')">
        <tr onclick="rowHref('222', 'v2')">
        <tr onclick="rowHref('333', 'v3')">
        """
        matches = _ROW_HREF_RE.findall(html)
        assert matches == ["111", "222", "333"]

    def test_whitespace_variation(self) -> None:
        html = """<tr onclick="rowHref ( '12345' , 'name' )">"""
        matches = _ROW_HREF_RE.findall(html)
        assert matches == ["12345"]


class TestExtractVersionName:
    def test_after_row_href(self) -> None:
        # pos 指向 VID 引号之后的位置
        prefix = "rowHref('291266'"
        html = f"{prefix}, 'RGOS 11.0(5)B9P30') other content"
        result = _extract_version_name(html, len(prefix), "291266")
        assert result == "RGOS 11.0(5)B9P30"

    def test_fallback_to_vid(self) -> None:
        """无法解析版本名称时回退到 VID。"""
        result = _extract_version_name("  no_version_here  ", 0, "12345")
        assert result == "12345"


class TestBuildReleaseFromDetail:
    """测试从 API 响应构建 FirmwareReleaseCandidate。"""

    def _make_detail_response(
        self, vid: int, file_id: int, filename: str = "fw.bin", size: int = 1000000
    ) -> dict:
        """构造模拟的版本详情 API 响应。"""
        return {
            "code": 200,
            "message": "success",
            "data": {
                "pageTitle": f"RG-RSR20-X1 {vid}",
                "publishDate": "2025-06-15",
                "versionStageStr": "正式版本",
                "baseVersion": "",
                "softFileList": [
                    {
                        "id": file_id,
                        "filename": filename,
                        "size": str(size),
                        "md5": base64.b64encode(b"mockmd5hash1234").decode(),
                        "softType": "版本",
                        "updateTime": "2025-06-15",
                        "downTotal": 100,
                    }
                ],
            },
        }
