"""tp-link-cn 品类粗筛 + 型号精判的契约测试。

用例取材于 2026-07-16/17、2026-07-20 对 TP-Link 资料中心
`filterConditions` 与 `search` 接口的真实实测，核心断言：

- 摄像机品类（含表面为「太阳能/4G-5G产品」的 2627/2631）下，型号含 IPC → camera；
- 2631 的非 IPC 记录里混有真 4G 蜂窝路由器（TL-TR907/903/901）→ cellular_cpe，
  不能一概当配件跳过（第二轮研判新发现 1 的修复）；
- 2627 的非 IPC 配件（TL-ZJ 支架、TL-SP 电源）→ None；
- 路由器品类 2502 → router，蜂窝细分同时看型号与产品名（产品名更可靠）；
  型号里的「5G」不再单独触发蜂窝判定（实测 TL-NR700-4C-5G 是普通企业路由）；
- 2502 里混入的工业边缘计算网关 TL-IEG → None；
- 2501 下只查询官方目标子分类：AP/网桥 → wireless_ap，无线路由器按型号细分，
  全屋 Wi-Fi 套装 → mesh_router；控制器、扩展器、工业级 AP 继续排除；
- 易展路由器 → mesh_router，但易展 AP 仍是 wireless_ap；
- 非白名单品类（交换机、门禁对讲、充电桩、2501 父分类等）→ None。
"""

import json
from pathlib import Path

import pytest

from firmatlas.adapters.tplink_cn.classification import (
    candidate_product_class_ids,
    classify,
)
from firmatlas.domain.model import ProductFamily, ProductType

FIXTURE = Path(__file__).parent / "fixtures" / "tp-link-cn" / "product_class_map.json"
WIRELESS_FIXTURE = Path(__file__).parent / "fixtures" / "tp-link-cn" / "filter_conditions_2501.json"


# --- 摄像机：品类命中 + 型号含 IPC --------------------------------------


@pytest.mark.parametrize(
    "class_id",
    ["2549", "2554", "2559", "2600", "2610"],
)
def test_camera_classes_with_ipc_model_map_to_camera(class_id: str) -> None:
    result = classify(class_id, "TL-IPC9440L-AC")
    assert result is not None
    assert result.family is ProductFamily.CAMERA
    assert result.product_type is ProductType.CAMERA
    assert result.product_class_id == class_id


@pytest.mark.parametrize(
    "model",
    ["TL-IPC642XL-F4GE", "TL-NIPC5454-GW4", "TL-NAIPC6332-GA4", "TL-AIPC6425TP-WBDC"],
)
def test_ipc_variants_all_recognized(model: str) -> None:
    # 摄像机型号有 IPC/NIPC/NAIPC/AIPC 多种前缀，共同点是含 "IPC"。
    result = classify("2600", model)
    assert result is not None
    assert result.product_type is ProductType.CAMERA


def test_solar_class_ipc_camera_is_captured() -> None:
    # 第一轮发现 2 修复：2627「太阳能产品」实为太阳能摄像机，不应漏采。
    result = classify("2627", "TL-IPC633L-A4G太阳能套装")
    assert result is not None
    assert result.family is ProductFamily.CAMERA


def test_4g5g_class_ipc_camera_is_captured() -> None:
    # 第一轮发现 2 修复：2631「4G/5G产品」多为 4G 插卡摄像机，不应漏采。
    result = classify("2631", "TL-IPC632X-A4GY")
    assert result is not None
    assert result.family is ProductFamily.CAMERA


@pytest.mark.parametrize(
    "class_id, model",
    [
        ("2627", "TL-ZJ800"),   # 支架配件
        ("2627", "TL-SP930H"),  # 供电配件
        ("2627", "TL-K234"),    # 非摄像机配件
        ("2631", "TL-SP620H"),  # 供电配件（即使出现在 2631 也不收）
    ],
)
def test_camera_class_non_ipc_accessories_are_skipped(class_id: str, model: str) -> None:
    # 品类命中但型号不含 IPC 且无蜂窝路由信号 → 判为配件，跳过（避免误入）。
    assert classify(class_id, model) is None


# --- 2631 内的真蜂窝路由器（第二轮研判新发现 1 修复）----------------------


@pytest.mark.parametrize(
    "model",
    ["TL-TR907", "TL-TR903", "TL-TR901"],
)
def test_4g5g_class_tr_routers_map_to_cellular_cpe(model: str) -> None:
    # 实测 2631 非 IPC 记录中的 TL-TR 系列是真 4G 蜂窝路由器，属采集范围，
    # 不能按「非 IPC ⇒ 配件」静默跳过。
    result = classify("2631", model)
    assert result is not None
    assert result.family is ProductFamily.ROUTER
    assert result.product_type is ProductType.CELLULAR_CPE


# --- 路由器：品类 2502 -----------------------------------------------------


@pytest.mark.parametrize(
    "model",
    ["TL-R5009PE-AC", "TL-ER6229GPE-AC", "TL-WVR1300G"],
)
def test_router_default_is_router(model: str) -> None:
    result = classify("2502", model)
    assert result is not None
    assert result.family is ProductFamily.ROUTER
    assert result.product_type is ProductType.ROUTER


def test_router_cellular_by_model_4g() -> None:
    result = classify("2502", "TL-NR1200W-4G-SD")
    assert result is not None
    assert result.product_type is ProductType.CELLULAR_CPE


def test_router_cellular_by_product_name() -> None:
    # 实测 TL-TR960G-EH：型号无 4G/5G/LTE 字样，但产品名「4G无线路由器」，
    # 产品名的蜂窝语义比型号可靠（第二轮研判新发现 3）。
    result = classify("2502", "TL-TR960G-EH", "4G无线路由器")
    assert result is not None
    assert result.product_type is ProductType.CELLULAR_CPE


def test_model_5g_alone_no_longer_triggers_cellular() -> None:
    # 实测 TL-NR700-4C-5G 产品名「高性能全千兆企业路由器」——型号里的 5G
    # 不是蜂窝语义，单看型号 5G 不再触发 cellular_cpe（第一轮的误判修复）。
    result = classify("2502", "TL-NR700-4C-5G", "高性能全千兆企业路由器")
    assert result is not None
    assert result.product_type is ProductType.ROUTER


def test_name_5g_with_wifi_context_is_not_cellular() -> None:
    # 产品名含 5G 但处于 Wi-Fi 语境（AX 速率）→ 指 5GHz 频段，不是蜂窝。
    result = classify("2502", "TL-XVR5400G", "企业级5G/AX5400 Wi-Fi 6 无线路由器")
    assert result is not None
    assert result.product_type is ProductType.ROUTER


def test_name_2dot5g_port_speed_is_not_cellular() -> None:
    # 「2.5G」是网口速率，不得触发蜂窝判定。
    result = classify("2502", "TL-R5408", "2.5G VPN路由器")
    assert result is not None
    assert result.product_type is ProductType.ROUTER


def test_industrial_edge_gateway_is_rejected() -> None:
    # 实测 2502 混有 TL-IEG5402-5G（工业级边缘计算网关），README 范围外。
    assert classify("2502", "TL-IEG5402-5G", "工业级边缘计算网关") is None


# --- 无线网络子分类：AP / 网桥 / 无线路由器 / Mesh --------------------------


@pytest.mark.parametrize("class_id", ["2505", "2763"])
def test_wireless_ap_and_bridge_map_to_wireless_ap(class_id: str) -> None:
    model = "TL-XAP3002GI-PoE" if class_id == "2505" else "TL-CPE500G"
    result = classify(class_id, model)
    assert result is not None
    assert result.family is ProductFamily.ROUTER
    assert result.product_type is ProductType.WIRELESS_AP


def test_easymesh_ap_stays_wireless_ap() -> None:
    # 「易展」是 AP 的组网能力，不能把接入点改判成家庭 Mesh 路由器。
    result = classify("2505", "TL-XAP3000GI-PoE易展版")
    assert result is not None
    assert result.product_type is ProductType.WIRELESS_AP


@pytest.mark.parametrize(
    "class_id, model",
    [
        ("2505", "TL-N7AP7200DT工业级"),
        ("2763", "TL-N7CPE7200DT工业级"),
    ],
)
def test_industrial_wireless_devices_are_skipped(class_id: str, model: str) -> None:
    assert classify(class_id, model) is None


def test_wireless_router_defaults_to_router() -> None:
    result = classify("2507", "TL-XDR3010")
    assert result is not None
    assert result.product_type is ProductType.ROUTER


@pytest.mark.parametrize("model", ["TL-WA832RE", "TL-WDA6332RE"])
def test_range_extenders_mixed_into_router_class_are_skipped(model: str) -> None:
    assert classify("2507", model) is None


def test_war_router_is_not_mistaken_for_range_extender() -> None:
    # 数字边界保证 TL-WAR 系列不会被 TL-WA 扩展器规则误伤。
    result = classify("2507", "TL-WAR1208L")
    assert result is not None
    assert result.product_type is ProductType.ROUTER


def test_wireless_router_cellular_signal_maps_to_cellular_cpe() -> None:
    result = classify("2507", "TL-NR1200W-4G-SD")
    assert result is not None
    assert result.product_type is ProductType.CELLULAR_CPE


def test_easymesh_wireless_router_maps_to_mesh_router() -> None:
    result = classify("2507", "TL-XDR6088易展Turbo版")
    assert result is not None
    assert result.product_type is ProductType.MESH_ROUTER


def test_whole_home_wifi_class_maps_to_mesh_router() -> None:
    result = classify("2508", "TL-XDR1850易展版")
    assert result is not None
    assert result.product_type is ProductType.MESH_ROUTER


# --- 2502 内的易展路由器 ----------------------------------------------------


def test_easymesh_in_model_maps_to_mesh_router() -> None:
    result = classify("2502", "TL-XVR5400G-5G易展版")
    assert result is not None
    assert result.product_type is ProductType.MESH_ROUTER


def test_easymesh_in_product_name_maps_to_mesh_router() -> None:
    result = classify("2502", "TL-R5408M", "2.5G易展VPN路由器")
    assert result is not None
    assert result.product_type is ProductType.MESH_ROUTER


def test_known_mesh_model_maps_without_product_name() -> None:
    # search API 的固件标题只有型号；精确白名单保证适配器也能识别该型号。
    result = classify("2502", "TL-R5408M")
    assert result is not None
    assert result.product_type is ProductType.MESH_ROUTER


# --- 非白名单品类：一律跳过 -----------------------------------------------


@pytest.mark.parametrize(
    "class_id, label",
    [
        ("2501", "无线网络父分类"),  # 只查询经过验证的子分类，不直接收混杂父分类
        ("2506", "无线控制器"),
        ("2509", "无线网卡"),
        ("2510", "天线及配件"),
        ("2503", "交换机"),
        ("2504", "全光网络"),
        ("2527", "网络安全"),
        ("2612", "NVR"),
        ("2620", "解码和显示"),
        ("2642", "工业交换机"),
        ("2684", "门禁对讲"),
        ("2686", "充电桩"),
        ("2698", "服务器"),
    ],
)
def test_non_whitelisted_classes_return_none(class_id: str, label: str) -> None:
    assert classify(class_id, "TL-IPC任意") is None, f"{label}({class_id}) 不应被本轮采集"


def test_wireless_parent_and_controller_are_not_accepted() -> None:
    # 父分类仍不直接接收；控制器子分类也不进入候选集合。
    assert classify("2501", "TL-XAP3002GI-PoE") is None
    assert classify("2506", "TL-AC1000") is None


def test_switch_and_doorbell_and_charger_rejected() -> None:
    assert classify("2503", "TL-SG2210P工业级") is None
    assert classify("2684", "TL-DP7") is None
    assert classify("2686", "TL-EVC-7kW-C4G套装") is None


def test_unknown_class_id_returns_none() -> None:
    assert classify("999999", "TL-IPC9440L") is None


def test_class_id_is_stripped() -> None:
    result = classify("  2502  ", "TL-R5009PE-AC")
    assert result is not None
    assert result.product_class_id == "2502"


# --- 粗筛品类 id 集合 ------------------------------------------------------


def test_candidate_ids_cover_all_target_classes() -> None:
    ids = candidate_product_class_ids()
    for cam in ("2549", "2554", "2559", "2600", "2610", "2627", "2631"):
        assert cam in ids
    assert "2502" in ids
    for wireless in ("2505", "2763", "2507", "2508"):
        assert wireless in ids


def test_candidate_ids_exclude_parent_and_non_target_wireless_classes() -> None:
    ids = candidate_product_class_ids()
    assert "2503" not in ids  # 交换机
    assert "2684" not in ids  # 门禁对讲
    assert "2686" not in ids  # 充电桩
    assert "2501" not in ids  # 混杂的无线网络父分类
    assert "2506" not in ids  # 无线控制器
    assert "2509" not in ids  # 无线网卡
    assert "2510" not in ids  # 天线及配件


def test_candidate_ids_exist_in_fixture_class_map() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    known_ids = {
        child["id"]
        for top in data["topProductClassList"]
        for child in top["childrens"]
    }
    wireless = json.loads(WIRELESS_FIXTURE.read_text(encoding="utf-8"))
    known_ids.update(str(child["classId"]) for child in wireless["subClasses"])
    for class_id in candidate_product_class_ids():
        assert class_id in known_ids, f"粗筛品类 id {class_id} 不在真实品类树中"
