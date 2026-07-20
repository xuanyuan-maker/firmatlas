"""tp-link-us 分类：按官方 menu_name 映射到领域分类（阶段 5）。

## 这个模块解决什么问题

US 下载站的索引页内嵌 productTree JSON，每个型号自带 `menu_name`（官方分类名，
如 "WiFi Routers"）。与 CN 站不同，US 站已经把型号归好类，因此**无需按型号名
做正则精判**——直接用 menu_name 白名单映射即可，更简单也更可靠。

## 范围（2026-07-19 用户决策）

只采集需求分析定义的五类：router / mesh_router / wireless_ap / cellular_cpe / camera。
关键边界（实测 menu_name 语义）：

- `CPE`（Pharos 户外无线网桥/基站 CPE210/WBS510 等）→ wireless_ap
  （是无线接入/桥接设备，**不是**蜂窝 CPE，勿与需求分析的 cellular_cpe 混淆）
- `5G/4G Routers`（家用蜂窝路由，如 TL-MR3220）→ cellular_cpe；
  `4G Wi-Fi Gateways`（Omada 商用网关）**排除**（商用网关，且多重定向到独立站）
- 网关/调制解调类（Wired/DSL/Cable Gateway、Cable Modems & Routers 等）**全部排除**
  （需求分析定义的五类无对应类型）
- `Video Recorders`（NVR）、`Security Camera Systems`（摄像头+NVR 套装）**排除**，
  只收纯摄像头（`Smart Cameras` / `Cameras`）

映射表未覆盖的 menu_name 一律返回 None（非目标，采集用例记录跳过原因，AC-08）。

## 输入 / 输出

- 输入：menu_name（字符串，如 "WiFi Routers"）
- 输出：Classification（family + product_type + 原始 menu_name）或 None

本模块是纯逻辑，不触网、不碰数据库，可独立单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass

from firmatlas.domain.model import ProductFamily, ProductType

# menu_name → (family, product_type)。白名单：只有表内分类才采集。
_MENU_NAME_MAP: dict[str, tuple[ProductFamily, ProductType]] = {
    # -- 路由器 --
    "WiFi Routers": (ProductFamily.ROUTER, ProductType.ROUTER),
    "Wired Routers": (ProductFamily.ROUTER, ProductType.ROUTER),
    "VPN Router": (ProductFamily.ROUTER, ProductType.ROUTER),
    "Load Balance Routers": (ProductFamily.ROUTER, ProductType.ROUTER),
    "Smart Home Router": (ProductFamily.ROUTER, ProductType.ROUTER),
    # -- Mesh --
    "Whole-Home Mesh": (ProductFamily.ROUTER, ProductType.MESH_ROUTER),
    # -- 无线 AP（含 Pharos 户外 CPE 网桥/基站）--
    "Standalone Wireless APs": (ProductFamily.ROUTER, ProductType.WIRELESS_AP),
    "Access Points": (ProductFamily.ROUTER, ProductType.WIRELESS_AP),
    "CPE": (ProductFamily.ROUTER, ProductType.WIRELESS_AP),
    # -- 家用蜂窝路由 --
    "5G/4G Routers": (ProductFamily.ROUTER, ProductType.CELLULAR_CPE),
    # -- 摄像头（纯摄像头，不含 NVR/套装）--
    "Smart Cameras": (ProductFamily.CAMERA, ProductType.CAMERA),
    "Cameras": (ProductFamily.CAMERA, ProductType.CAMERA),
}


@dataclass(frozen=True)
class Classification:
    """一次成功的分类判定结果。

    source_category 保留厂商原始 menu_name（需求分析要求保留厂商原始分类）。
    """

    source_category: str
    family: ProductFamily
    product_type: ProductType


def classify(menu_name: str) -> Classification | None:
    """把 US 站 menu_name 映射到领域分类。

    返回 None 表示该分类不是本轮采集目标（网关/modem/NVR/套装/交换机/配件等），
    采集用例应记录跳过原因后丢弃（AC-08）。
    """
    key = menu_name.strip()
    mapping = _MENU_NAME_MAP.get(key)
    if mapping is None:
        return None
    family, product_type = mapping
    return Classification(
        source_category=key,
        family=family,
        product_type=product_type,
    )


def target_menu_names() -> frozenset[str]:
    """返回所有目标 menu_name，供适配器在遍历 productTree 时快速筛选。"""
    return frozenset(_MENU_NAME_MAP)
