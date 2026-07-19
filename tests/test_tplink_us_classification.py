"""tp-link-us 分类模块契约测试（阶段 5）。

纯逻辑测试，不触网、不碰数据库。
"""

from __future__ import annotations

import pytest

from firmatlas.adapters.tplink_us.classification import (
    classify,
    target_menu_names,
)
from firmatlas.domain.model import ProductFamily, ProductType


@pytest.mark.parametrize(
    "menu_name,family,product_type",
    [
        ("WiFi Routers", ProductFamily.ROUTER, ProductType.ROUTER),
        ("Wired Routers", ProductFamily.ROUTER, ProductType.ROUTER),
        ("VPN Router", ProductFamily.ROUTER, ProductType.ROUTER),
        ("Load Balance Routers", ProductFamily.ROUTER, ProductType.ROUTER),
        ("Smart Home Router", ProductFamily.ROUTER, ProductType.ROUTER),
        ("Whole-Home Mesh", ProductFamily.ROUTER, ProductType.MESH_ROUTER),
        ("Standalone Wireless APs", ProductFamily.ROUTER, ProductType.WIRELESS_AP),
        ("Access Points", ProductFamily.ROUTER, ProductType.WIRELESS_AP),
        ("CPE", ProductFamily.ROUTER, ProductType.WIRELESS_AP),
        ("5G/4G Routers", ProductFamily.ROUTER, ProductType.CELLULAR_CPE),
        ("Smart Cameras", ProductFamily.CAMERA, ProductType.CAMERA),
        ("Cameras", ProductFamily.CAMERA, ProductType.CAMERA),
    ],
)
def test_target_categories(menu_name, family, product_type):
    """目标分类映射到正确的 family/type，且保留原始 menu_name。"""
    result = classify(menu_name)
    assert result is not None
    assert result.family is family
    assert result.product_type is product_type
    assert result.source_category == menu_name


@pytest.mark.parametrize(
    "menu_name",
    [
        "4G Wi-Fi Gateways",   # Omada 商用蜂窝网关，排除
        "Wired Gateways",      # 商用有线网关，排除
        "DSL Gateway",         # 调制解调网关，排除
        "Cable Modems & Routers",  # 有线调制解调器，排除
        "Video Recorders",     # NVR 录像机，非摄像头
        "Security Camera Systems",  # 摄像头+NVR 套装，排除
        "All Unmanaged Switches",   # 交换机，非目标
        "Smart Plugs",         # 智能插座，非目标
        "Range Extenders",     # 信号扩展器，非目标
        "",                    # 空串
        "Unknown Category",    # 未知分类
    ],
)
def test_non_target_categories_return_none(menu_name):
    """非目标分类（网关/modem/NVR/套装/交换机/配件/未知）返回 None。"""
    assert classify(menu_name) is None


def test_classify_strips_whitespace():
    """menu_name 前后空白不影响判定。"""
    result = classify("  WiFi Routers  ")
    assert result is not None
    assert result.product_type is ProductType.ROUTER


def test_target_menu_names_covers_all_mapped():
    """target_menu_names 返回所有目标分类，供适配器筛选。"""
    names = target_menu_names()
    assert "WiFi Routers" in names
    assert "Smart Cameras" in names
    assert "CPE" in names
    # 非目标不在内
    assert "Video Recorders" not in names
    assert "4G Wi-Fi Gateways" not in names
