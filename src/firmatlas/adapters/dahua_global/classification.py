"""大华固件分类：所有选定类别的产品均为摄像机。

本模块只做枚举定义和结果包装，不涉及解析逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass

from firmatlas.domain.model import ProductFamily, ProductType

# 采集的摄像机分类（child_menu_id → 分类名）
CAMERA_CATEGORY_IDS = frozenset({1, 586, 4472, 4572, 14197, 18199})

_CATEGORY_NAMES: dict[int, str] = {
    1: "Network Cameras",
    586: "Thermal Cameras",
    4472: "Intelligent Traffic",
    4572: "PTZ Cameras",
    14197: "Explosion-Proof",
    18199: "PT Cameras",
}


@dataclass(frozen=True)
class Classification:
    source_category: str
    family: ProductFamily
    product_type: ProductType


def classify(category_id: int) -> Classification | None:
    name = _CATEGORY_NAMES.get(category_id)
    if name is None:
        return None
    return Classification(
        source_category=name,
        family=ProductFamily.CAMERA,
        product_type=ProductType.CAMERA,
    )
