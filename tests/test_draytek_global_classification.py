"""draytek_global classification 单元测试。"""

from firmatlas.adapters.draytek_global.classification import classify
from firmatlas.domain.model import ProductType


class TestClassify:
    """FTP 目录名分类测试。"""

    # -- Router --------------------------------

    def test_router_prefix(self) -> None:
        result = classify("Vigor2767")
        assert result is not None
        assert result.product_type == ProductType.ROUTER
        assert result.source_category == "Router"

    def test_router_vigor_c_series(self) -> None:
        """Vigor C 系列（Cable 路由器）。"""
        result = classify("Vigor C410")
        assert result is not None
        assert result.product_type == ProductType.ROUTER

    def test_router_vigor_n_series(self) -> None:
        """Vigor N 系列路由器。"""
        result = classify("Vigor N61")
        assert result is not None
        assert result.product_type == ProductType.ROUTER

    def test_router_numeric_suffix(self) -> None:
        result = classify("Vigor130")
        assert result is not None
        assert result.product_type == ProductType.ROUTER

    def test_router_with_trailing_slash(self) -> None:
        """目录名可能带有尾部斜杠，解析前应 strip。"""
        result = classify("  Vigor2767/  ")
        assert result is not None
        assert result.product_type == ProductType.ROUTER

    # -- Wireless AP --------------------------

    def test_ap_prefix(self) -> None:
        result = classify("VigorAP 905")
        assert result is not None
        assert result.product_type == ProductType.WIRELESS_AP
        assert result.source_category == "AP"

    def test_ap_no_space(self) -> None:
        """少数 AP 目录名不带空格（如 VigorAP902）。"""
        result = classify("VigorAP902")
        assert result is not None
        assert result.product_type == ProductType.WIRELESS_AP

    # -- Cellular CPE -------------------------

    def test_cellular_lte_series(self) -> None:
        result = classify("Vigor2620 LTE Series")
        assert result is not None
        assert result.product_type == ProductType.CELLULAR_CPE

    def test_cellular_lte_dedicated(self) -> None:
        result = classify("VigorLTE 200")
        assert result is not None
        assert result.product_type == ProductType.CELLULAR_CPE

    def test_cellular_5g(self) -> None:
        result = classify("Vigor2927L-5G Series")
        assert result is not None
        assert result.product_type == ProductType.CELLULAR_CPE

    # -- Excluded -----------------------------

    def test_excluded_switch(self) -> None:
        assert classify("VigorSwitch G1080") is None

    def test_excluded_connect(self) -> None:
        assert classify("VigorConnect") is None

    def test_excluded_poe(self) -> None:
        assert classify("VigorPoE 600") is None

    def test_excluded_nic(self) -> None:
        assert classify("VigorNIC 132") is None

    def test_excluded_phone(self) -> None:
        assert classify("VigorPhone 350") is None

    def test_excluded_bx(self) -> None:
        assert classify("VigorBX 2000") is None

    def test_excluded_acs(self) -> None:
        assert classify("VigorACS 3") is None

    # -- Non-Vigor ----------------------------

    def test_non_vigor_utility(self) -> None:
        assert classify("Utility") is None

    def test_non_vigor_acs(self) -> None:
        assert classify("ACS 3") is None

    def test_non_vigor_databook(self) -> None:
        assert classify("Databook") is None

    def test_non_vigor_accessories(self) -> None:
        assert classify("Accessories") is None
