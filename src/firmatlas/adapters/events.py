"""适配器发现事件类型（接口设计 §4.1）。

discover() 逐个产出这些事件，用例边消费边入库。
DiscoveryCompleted 必须是最后一个事件且只出现一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from firmatlas.domain.candidates import ProductCandidate


class SkipReason(StrEnum):
    """跳过原因的稳定枚举。"""

    UNMAPPED_TYPE = "unmapped_type"        # classify() 返回 None，产品类型不在采集范围
    PARSE_FAILED = "parse_failed"           # 标题解析失败
    MISSING_IDENTITY = "missing_identity"   # 缺少生成 source_key 的必要信息


@dataclass(frozen=True)
class DiscoveredProduct:
    """适配器发现的一个完整产品子树，可直接经 Repository 入库。"""

    product: ProductCandidate


@dataclass(frozen=True)
class SkippedCandidate:
    """一条来源记录因为不在采集范围/解析失败等原因被跳过。

    用例聚合这些记录写入 CrawlRun.issues 和完整性报告（AC-08）。
    """

    stage: str                     # "product" / "artifact"（tp-link-cn 在 artifact 级跳过）
    reason_code: SkipReason
    detail: str                    # 人类可读说明
    source_url: str | None
    raw_hint: str | None           # 定位用原始片段（如 API 记录 id）


@dataclass(frozen=True)
class DiscoveryCompleted:
    """适配器确认已覆盖来源当前公开的全部目标范围。

    is_complete=False 表示适配器提前终止或遇到不确定的情况，
    用例不得触发消失对账（AC-16）。
    """

    is_complete: bool
    incomplete_reason: str | None
    issues: tuple[AdapterIssueSummary, ...]


@dataclass(frozen=True)
class AdapterIssueSummary:
    """非致命问题的摘要（不同于 domain.model.AdapterIssue，不要求 source_url）。"""

    code: str
    detail: str
    source_url: str | None = None


# 联合类型（Python 3.12 语法）
type DiscoveryEvent = DiscoveredProduct | SkippedCandidate | DiscoveryCompleted
