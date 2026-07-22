"""DrayTek 产品分类：按 FTP 目录名前缀判定产品类型。

FTP 服务器（fw.draytek.com.tw）根目录包含约 170 个产品目录，
本模块按目录名前缀将产品映射到 FirmAtlas 的 ProductFamily / ProductType，
非目标产品返回 None。
"""

from __future__ import annotations

from dataclasses import dataclass

from firmatlas.domain.model import ProductFamily, ProductType


@dataclass(frozen=True)
class Classification:
    """按目录名分类的结果。"""

    source_category: str  # "Router" / "AP" / "Cellular Router"
    family: ProductFamily
    product_type: ProductType


# 属于 Vigor 系列但不属于 Router 或 AP 的产品前缀（应跳过）。
_EXCLUDED_VIGOR_PREFIXES = (
    "VIGORSWITCH",
    "VIGORCONNECT",
    "VIGORPOE",
    "VIGORNIC",
    "VIGORPHONE",
    "VIGORPLUG",
    "VIGORTALK",
    "VIGORACCESS",
    "VIGORIPPBX",
    "VIGORBX",    # PBX 基站
    "VIGORACS",
    "VIGORFLY",   # 旅行路由器太小众，跳过
    "VIGORPRO",   # UTM 安全设备，不是纯路由器
)


def classify(dir_name: str) -> Classification | None:
    """根据 FTP 目录名判定产品分类，非目标返回 None。

    规则：
    - VigorAP* → WIRELESS_AP
    - VigorLTE* 或含 LTE/5G 的 → CELLULAR_CPE
    - 其余 Vigor* → ROUTER
    - VigorSwitch/Connect/PoE 等非目标 → None
    - 非 Vigor 开头 → None
    """
    name = dir_name.strip()
    upper = name.upper()

    # 必须 Vigor 开头
    if not upper.startswith("VIGOR"):
        return None

    # 排除非 Router/AP 的 Vigor 产品
    for prefix in _EXCLUDED_VIGOR_PREFIXES:
        if upper.startswith(prefix):
            return None

    # Access Point
    if upper.startswith("VIGORAP"):
        return Classification(
            source_category="AP",
            family=ProductFamily.ROUTER,
            product_type=ProductType.WIRELESS_AP,
        )

    # 蜂窝路由器：LTE 或 5G 在目录名中
    if "LTE" in upper or "5G" in upper:
        return Classification(
            source_category="Cellular Router",
            family=ProductFamily.ROUTER,
            product_type=ProductType.CELLULAR_CPE,
        )

    # 其余默认路由器
    return Classification(
        source_category="Router",
        family=ProductFamily.ROUTER,
        product_type=ProductType.ROUTER,
    )
