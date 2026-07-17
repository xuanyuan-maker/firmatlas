"""适配器发现与地址刷新事件类型（接口设计 §4.1、§4.2）。

discover() 逐个产出发现事件，用例边消费边入库。
DiscoveryCompleted 必须是最后一个事件且只出现一次。
refresh_artifact_url() 供下载用例在地址失效时调用，结果同样用数据表示。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from firmatlas.domain.candidates import ProductCandidate
from firmatlas.domain.model import OfficialChecksum


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


# ---------------------------------------------------------------------------
# 地址刷新（接口设计 §4.2，下载用例 → 适配器）
# ---------------------------------------------------------------------------


class RefreshFailureReason(StrEnum):
    """地址刷新失败原因的稳定枚举。"""

    NOT_FOUND = "not_found"                 # 来源已找不到该 Artifact（可能已下架）
    IDENTITY_CONFLICT = "identity_conflict"  # 找到的记录 source_key 与请求不一致
    SOURCE_ERROR = "source_error"            # 来源访问失败（网络/HTTP 错误）


@dataclass(frozen=True)
class ArtifactRefreshRequest:
    """下载用例根据 ArtifactContext 构造的刷新请求。

    artifact_source_key 刷新前后不得改变（AC-29）；
    known_* 字段供适配器核对身份，避免刷新到另一个文件。
    """

    product_source_key: str
    hardware_revision_source_key: str
    release_source_key: str
    artifact_source_key: str
    stale_url: str
    known_filename: str | None
    known_size: int | None
    known_checksum: OfficialChecksum | None


@dataclass(frozen=True)
class ArtifactUrlRefreshed:
    """刷新成功：拿到新的下载地址。"""

    download_url: str
    url_expires_at: datetime | None


@dataclass(frozen=True)
class ArtifactRefreshFailed:
    """刷新失败：原因用稳定枚举表示，不抛异常。"""

    reason_code: RefreshFailureReason
    detail: str


type ArtifactRefreshResult = ArtifactUrlRefreshed | ArtifactRefreshFailed
