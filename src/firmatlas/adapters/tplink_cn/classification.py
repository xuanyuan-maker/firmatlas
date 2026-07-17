"""tp-link-cn 品类粗筛 + 型号精判（阶段 3）。

## 这个模块解决什么问题

TP-Link 资料中心（resource.tp-link.com.cn）把所有品类的升级软件混在一个列表里。
README 0x02 限定 MVP 只采集路由器类（home_router/mesh_router/wireless_ap/
cellular_cpe）和摄像头（camera）。因此采集前必须过滤。

**实测（2026-07-16/17 两轮）发现纯品类白名单不成立**，因此改用两级过滤：

1. **品类粗筛**：用 search 接口的 `productClassIds` 参数（服务端过滤）圈定
   *可能*含目标设备的品类，缩小拉取范围。
2. **型号精判**：对拉到的每条记录再按型号（辅以产品名）判定 family/type，
   非目标返回 None。

为什么两级都需要（实测证据）：

- 摄像机**散落在多个品类**：除「无线/有线/球型/AI场景/鹰眼摄像机」外，
  「太阳能产品(2627)」「4G/5G产品(2631)」下也以 TL-IPC 摄像机为主。
  只按摄像机品类白名单会漏采这批目标——这种漏采不会报错，最隐蔽。
- 反过来，粗筛品类内也混有非目标，须逐条精判：
  - 2627/2631 混有配件（支架 TL-ZJ、电源 TL-SP）→ 靠「不含 IPC」跳过；
  - 2631 的非 IPC 里又混有**真 4G 蜂窝路由器**（TL-TR907/903/901，实测
    2026-07-17）→ 不能一概当配件，须按蜂窝信号收为 cellular_cpe；
  - 2502 混有工业边缘计算网关（TL-IEG 系列，README 范围外）→ 按前缀排除。

## 本模块的范围（阶段 3 切片）

已实现：**摄像机**、**有线/企业路由器（品类 2502）**、**4G 蜂窝路由（2631 内）**。

**暂未实现（已知缺口，见 PLAN.md 跟踪）**：
- `wireless_ap`：其主品类「无线网络(2501)」实测混装无线 AP、家用/企业路由、
  **无线控制器 AC（TL-AC/NAC，README 明确排除）**、硬盘录像机(TL-XVR)、
  免驱网卡(TL-XDN)等，需要一张专门且严格的型号规则表，单列一轮处理，
  避免把非目标误收进正式目录（AC-07 红线）。
- `mesh_router`：TP-Link 易展（EasyMesh）产品分散在 2501 与 2502
  （如 TL-R5408M「2.5G 易展 VPN 路由器」）。**本轮对型号或产品名含「易展」
  的记录一律返回 None 跳过**（2026-07-17 用户决策：宁缺勿错，避免先以
  home_router 错误入库、mesh 专项回补时再修存量数据）。

在这两项补齐前，MVP 五类中的 wireless_ap / mesh_router 不会被产出（AC-06 缺口）。

## 输入 / 输出

- 输入：二级品类 id（字符串，如 "2502"）+ 型号（如 "TL-R5009PE-AC"）
  + 可选产品名（如「异地组网4G路由器」，来自 search 记录的 productName）
- 输出：Classification（family + product_type + 命中的品类 id/名）
        或 None（该记录不是本轮目标，采集用例应记录跳过原因后丢弃，AC-08）

产品名的蜂窝语义比型号可靠（实测 TL-TR960G-EH 型号无 4G 字样、产品名
「4G无线路由器」），因此蜂窝判定同时看两者；不传产品名时退化为纯型号判定。

本模块是纯逻辑，不触网、不碰数据库，可独立单元测试。
品类 id 出处见 `tests/fixtures/tp-link-cn/product_class_map.json`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from firmatlas.domain.model import ProductFamily, ProductType

# ---------------------------------------------------------------------------
# 品类粗筛：哪些品类值得拉取（后续仍需型号精判）
# ---------------------------------------------------------------------------

# 摄像机可能出现的品类 id -> 该品类展示名。
# 前 5 类是「视觉安防」下的各类摄像机；2627/2631 表面是「太阳能/4G-5G产品」，
# 实测其内容以 TL-IPC 摄像机为主，故一并纳入粗筛，再靠型号精判。
_CAMERA_CANDIDATE_CLASSES: dict[str, str] = {
    "2549": "无线摄像机",
    "2554": "有线摄像机",
    "2559": "球型摄像机",
    "2600": "AI/场景摄像机",
    "2610": "鹰眼系统",
    "2627": "太阳能产品",
    "2631": "4G/5G产品",
}

# 路由器品类 id -> 展示名。实测 2502 以有线/企业路由为主（TL-R/ER/NR 等前缀），
# 整类纳入 router 家族，但须排除混入的工业边缘计算网关（TL-IEG）。
_ROUTER_CANDIDATE_CLASSES: dict[str, str] = {
    "2502": "路由器",
}

# 摄像机型号的稳定标记：型号含子串 "IPC"（覆盖 TL-IPC/NIPC/NAIPC/AIPC）。
_CAMERA_MODEL_HINT = re.compile(r"IPC", re.IGNORECASE)

# 2631「4G/5G产品」内非 IPC 的真蜂窝路由器系列（实测 TL-TR907/903/901）。
_TR_CELLULAR_ROUTER = re.compile(r"^TL-TR\d", re.IGNORECASE)

# 工业边缘计算网关（实测 2502 内混有 TL-IEG5402-5G），README 范围外。
_INDUSTRIAL_GATEWAY_PREFIX = "TL-IEG"

# 强蜂窝信号：4G / LTE（词边界；同时排除「2.4G」这类频段写法——点号前缀不算），
# 以及中文「蜂窝」「插卡」。出现在型号或产品名中均算数。
_CELLULAR_STRONG = re.compile(r"(?:^|[^0-9A-Za-z.])(?:4G|LTE)(?![0-9A-Za-z])", re.IGNORECASE)
_CELLULAR_STRONG_CN = re.compile(r"蜂窝|插卡")

# 弱蜂窝信号：5G。语义歧义（蜂窝 5G vs 5GHz Wi-Fi 频段），只在**产品名**中出现
# 且产品名没有 Wi-Fi 语境（Wi-Fi/AX 速率）时才算蜂窝；「2.5G」（网口速率）不算。
# 实测依据：TL-NR700-4C-5G 产品名「高性能全千兆企业路由器」是普通路由，
# TL-XVR5400G-5G易展版 产品名「企业级5G/AX5400 Wi-Fi 6 无线路由器」的 5G 指频段。
_NAME_5G = re.compile(r"(?:^|[^0-9A-Za-z.])5G(?![0-9A-Za-z])", re.IGNORECASE)
_WIFI_CONTEXT = re.compile(r"Wi-?Fi|AX\d", re.IGNORECASE)

# 易展（EasyMesh）标记：本轮一律跳过，mesh 专项时统一收（2026-07-17 用户决策）。
_EASYMESH_HINT = "易展"


@dataclass(frozen=True)
class Classification:
    """一次成功的品类判定结果。

    product_class_name 保留厂商原始品类名，对应 README 的 source_category
    （厂商原始分类必须保留）。
    """

    product_class_id: str
    product_class_name: str
    family: ProductFamily
    product_type: ProductType


def _is_cellular(model: str, product_name: str) -> bool:
    """蜂窝设备判定：型号/产品名的强信号，或产品名中无 Wi-Fi 语境的 5G。"""
    for text in (model, product_name):
        if _CELLULAR_STRONG.search(text) or _CELLULAR_STRONG_CN.search(text):
            return True
    if product_name and _NAME_5G.search(product_name) and not _WIFI_CONTEXT.search(product_name):
        return True
    return False


def classify(
    product_class_id: str, model: str, product_name: str = ""
) -> Classification | None:
    """把 tp-link-cn 二级品类 id + 型号（+ 可选产品名）映射到领域分类。

    返回 None 表示该记录不是本轮采集目标（非白名单品类、摄像机品类下的
    非目标配件、工业网关、易展 mesh 产品等），采集用例应记录跳过原因后
    丢弃（AC-08）。
    """
    key = product_class_id.strip()

    # 易展（mesh）产品本轮统一跳过：宁缺勿错，避免以 home_router 错误入库。
    if _EASYMESH_HINT in model or _EASYMESH_HINT in product_name:
        return None

    if key in _CAMERA_CANDIDATE_CLASSES:
        # 摄像机品类：型号含 IPC 的是真摄像机。
        if _CAMERA_MODEL_HINT.search(model):
            return Classification(
                product_class_id=key,
                product_class_name=_CAMERA_CANDIDATE_CLASSES[key],
                family=ProductFamily.CAMERA,
                product_type=ProductType.CAMERA,
            )
        # 2631「4G/5G产品」的非 IPC 记录不全是配件：实测混有 TL-TR 系列
        # 真 4G 蜂窝路由器，按系列前缀或蜂窝信号收为 cellular_cpe。
        is_tr_cellular = _TR_CELLULAR_ROUTER.match(model) or _is_cellular(model, product_name)
        if key == "2631" and is_tr_cellular:
            return Classification(
                product_class_id=key,
                product_class_name=_CAMERA_CANDIDATE_CLASSES[key],
                family=ProductFamily.ROUTER,
                product_type=ProductType.CELLULAR_CPE,
            )
        # 其余（2627 的供电/支架配件等）跳过。
        return None

    if key in _ROUTER_CANDIDATE_CLASSES:
        # 排除混入 2502 的工业边缘计算网关（README 范围外）。
        if model.upper().startswith(_INDUSTRIAL_GATEWAY_PREFIX):
            return None
        product_type = (
            ProductType.CELLULAR_CPE
            if _is_cellular(model, product_name)
            else ProductType.HOME_ROUTER
        )
        return Classification(
            product_class_id=key,
            product_class_name=_ROUTER_CANDIDATE_CLASSES[key],
            family=ProductFamily.ROUTER,
            product_type=product_type,
        )

    return None


def candidate_product_class_ids() -> tuple[str, ...]:
    """返回本轮所有粗筛品类 id，供采集用例作为 search 的 productClassIds 传入，
    从服务端就只拉取这些品类，再逐条型号精判。

    注意：这些是「值得拉取」的品类，不代表其中每条都会入库——classify()
    仍会逐条精判并跳过非目标记录。
    """
    return (
        *_CAMERA_CANDIDATE_CLASSES.keys(),
        *_ROUTER_CANDIDATE_CLASSES.keys(),
    )
