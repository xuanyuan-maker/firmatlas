"""目录查询接口（接口设计 §6.4）：list / show 的只读跨表查询。

与 Repository 的区别：Repository 面向写入与单实体查询、必须在 UnitOfWork
事务内使用；这里是面向 CLI 展示的扁平 DTO（跨表 join 结果），只读、
每次调用独立连接、不参与事务。

表格与 JSON 输出共用同一 DTO，序列化在 CLI 展示层完成（AC-22）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from firmatlas.domain.model import (
    ArtifactType,
    DownloadStatus,
    OfficialChecksum,
    ProductFamily,
    ProductType,
    VerificationStatus,
    VisibilityStatus,
)

#: JSON 输出中的 schema_version（AC-23）。输出结构不兼容变化时递增。
OUTPUT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CatalogFilter:
    """list 的组合筛选条件，全部可选（AC-21）。

    枚举条件为精确匹配；model/hardware/version/series 为不区分大小写的
    包含匹配（对用户输入更宽容）；vendor/source 精确、region 忽略大小写。
    """

    vendor: str | None = None
    source: str | None = None
    region: str | None = None
    family: ProductFamily | None = None
    type: ProductType | None = None
    series: str | None = None
    model: str | None = None
    hardware: str | None = None
    version: str | None = None
    visibility: VisibilityStatus | None = None
    download_status: DownloadStatus | None = None
    verification_status: VerificationStatus | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True)
class FirmwareListRow:
    """list 输出的一行：一条固件发布及其所属链的关键字段。"""

    release_id: str
    source_key: str
    vendor_key: str
    region_code: str
    model: str
    product_type: ProductType
    series: str | None
    hardware: str
    version: str
    version_normalized: str | None
    release_date: date | None
    visibility: VisibilityStatus
    artifact_count: int
    last_seen_at: datetime


@dataclass(frozen=True)
class CatalogPage:
    rows: list[FirmwareListRow]
    total: int  # 满足筛选条件的总行数（不受 limit/offset 影响）


@dataclass(frozen=True)
class ArtifactDetail:
    """show 输出中的单个 Artifact 及其最近一次下载状态。"""

    artifact_id: str
    artifact_type: ArtifactType
    original_filename: str | None
    download_url: str
    advertised_size: int | None
    media_type: str | None
    official_checksum: OfficialChecksum | None
    visibility: VisibilityStatus
    last_download_status: DownloadStatus | None
    last_verification_status: VerificationStatus | None


@dataclass(frozen=True)
class ReleaseDetail:
    """show 输出：发布 + 所属链 + Artifact 列表。"""

    release_id: str
    source_key: str
    vendor_key: str
    region_code: str
    display_name: str
    model: str
    product_type: ProductType
    series: str | None
    hardware: str
    hardware_raw: str | None
    version: str
    version_normalized: str | None
    release_date: date | None
    title: str | None
    release_notes: str | None
    source_url: str
    visibility: VisibilityStatus
    first_seen_at: datetime
    last_seen_at: datetime
    disappeared_at: datetime | None
    artifacts: tuple[ArtifactDetail, ...]


class CatalogQueryService(Protocol):
    def list_firmware(self, f: CatalogFilter) -> CatalogPage: ...

    def show_release(self, release_id: str) -> ReleaseDetail | None:
        """按 ID 查发布详情；不存在时返回 None（CLI 转为明确提示）。"""
        ...

    def find_release_ids_by_prefix(self, prefix: str, *, limit: int = 5) -> list[str]:
        """按 ID 前缀查找发布（list 输出短 ID，show 支持前缀输入）。"""
        ...

    def find_artifact_ids_by_prefix(self, prefix: str, *, limit: int = 5) -> list[str]:
        """按 ID 前缀查找 Artifact（download 支持 show 输出的完整/前缀 ID）。"""
        ...
