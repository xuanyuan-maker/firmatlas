"""Candidate 类型：适配器解析来源后输出的候选数据（接口设计 §3.2）。

与领域实体（model.py）的区别：
- 没有数据库 id（适配器不可见数据库）；
- 没有 first_seen_at / last_seen_at / visibility_status 等生命周期字段，
  这些由业务用例与 Repository 在持久化时决定；
- 父子关系用结构嵌套表达（一个产品一棵完整子树），不含外键。
"""

from dataclasses import dataclass
from datetime import date, datetime

from firmatlas.domain.model import (
    ArtifactType,
    OfficialChecksum,
    ProductFamily,
    ProductType,
)

# 来源未提供硬件版本时的占位值（接口设计 §3.2）
UNSPECIFIED_REVISION_SOURCE_KEY = "__unspecified__"
UNSPECIFIED_REVISION = "unspecified"


@dataclass(frozen=True)
class FirmwareArtifactCandidate:
    source_key: str
    artifact_type: ArtifactType
    original_filename: str | None
    download_url: str
    url_expires_at: datetime | None
    advertised_size: int | None
    media_type: str | None
    official_checksum: OfficialChecksum | None


@dataclass(frozen=True)
class FirmwareReleaseCandidate:
    source_key: str
    version_raw: str
    version_normalized: str | None
    release_date: date | None
    title: str | None
    release_notes: str | None
    release_notes_url: str | None
    source_url: str
    artifacts: tuple[FirmwareArtifactCandidate, ...]


@dataclass(frozen=True)
class HardwareRevisionCandidate:
    source_key: str
    raw_revision: str | None
    normalized_revision: str
    revision_explicit: bool
    source_url: str | None
    releases: tuple[FirmwareReleaseCandidate, ...]


@dataclass(frozen=True)
class ProductCandidate:
    source_key: str
    display_name: str
    model_raw: str
    model_normalized: str
    series: str | None
    product_family: ProductFamily
    product_type: ProductType
    source_category: str | None
    source_url: str
    hardware_revisions: tuple[HardwareRevisionCandidate, ...]
