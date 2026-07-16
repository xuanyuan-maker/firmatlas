"""领域对象与枚举（接口设计 §3.3、§6）。

- 枚举值与数据库 CHECK 约束中的文本一字不差（infra/schema.py）；
- 实体 dataclass 与 7 张表逐字段对应，但类型换成 Python 原生类型：
  时间为带 UTC 时区的 datetime、日期为 date、布尔为 bool，
  与 TEXT/INTEGER 的互转由 Repository 实现负责；
- 官方校验和的 algorithm/value 两列在领域侧合并为 OfficialChecksum 一个值对象；
- 本模块不得 import 任何第三方库。
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class ProductFamily(StrEnum):
    ROUTER = "router"
    CAMERA = "camera"


class ProductType(StrEnum):
    HOME_ROUTER = "home_router"
    MESH_ROUTER = "mesh_router"
    WIRELESS_AP = "wireless_ap"
    CELLULAR_CPE = "cellular_cpe"
    CAMERA = "camera"


class ArtifactType(StrEnum):
    FIRMWARE = "firmware"
    RECOVERY = "recovery"
    OTHER_FIRMWARE = "other_firmware"


class VisibilityStatus(StrEnum):
    ACTIVE = "active"
    DISAPPEARED = "disappeared"


class DiscoveryMethod(StrEnum):
    API = "api"
    HTML = "html"
    HYBRID = "hybrid"


class CrawlRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DownloadStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class VerificationStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    NOT_AVAILABLE = "not_available"
    VERIFIED = "verified"
    MISMATCH = "mismatch"


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OfficialChecksum:
    """厂商公布的官方校验和，algorithm 如 'md5'、'sha256'。"""

    algorithm: str
    value: str


@dataclass(frozen=True)
class AdapterIssue:
    """采集过程中的非致命问题，随 CrawlRun 以 JSON 形式落库。"""

    code: str
    detail: str
    source_url: str | None = None


# ---------------------------------------------------------------------------
# 实体（Repository 输出，字段与表列一一对应）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FirmwareSource:
    id: str
    vendor_key: str
    vendor_name: str
    source_key: str
    name: str
    region_code: str
    locale: str | None
    base_url: str
    adapter_key: str
    discovery_method: DiscoveryMethod
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Product:
    id: str
    source_id: str
    source_key: str
    display_name: str
    model_raw: str
    model_normalized: str
    series: str | None
    product_family: ProductFamily
    product_type: ProductType
    source_category: str | None
    source_url: str
    first_seen_at: datetime
    last_seen_at: datetime
    last_seen_run_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class HardwareRevision:
    id: str
    product_id: str
    source_key: str
    raw_revision: str | None
    normalized_revision: str
    revision_explicit: bool
    source_url: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    last_seen_run_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class FirmwareRelease:
    id: str
    hardware_revision_id: str
    source_key: str
    version_raw: str
    version_normalized: str | None
    release_date: date | None
    title: str | None
    release_notes: str | None
    release_notes_url: str | None
    source_url: str
    visibility_status: VisibilityStatus
    first_seen_at: datetime
    last_seen_at: datetime
    disappeared_at: datetime | None
    last_seen_run_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class FirmwareArtifact:
    id: str
    release_id: str
    source_key: str
    artifact_type: ArtifactType
    original_filename: str | None
    download_url: str
    url_last_resolved_at: datetime
    url_expires_at: datetime | None
    advertised_size: int | None
    media_type: str | None
    official_checksum: OfficialChecksum | None
    visibility_status: VisibilityStatus
    first_seen_at: datetime
    last_seen_at: datetime
    disappeared_at: datetime | None
    last_seen_run_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CrawlRun:
    id: str
    source_id: str
    status: CrawlRunStatus
    is_complete: bool
    started_at: datetime
    finished_at: datetime | None
    products_seen: int
    releases_seen: int
    artifacts_seen: int
    items_added: int
    items_updated: int
    items_disappeared: int
    items_skipped: int
    error_count: int
    error_summary: str | None
    issues: tuple[AdapterIssue, ...]
    created_at: datetime


@dataclass(frozen=True)
class DownloadRecord:
    id: str
    artifact_id: str
    status: DownloadStatus
    verification_status: VerificationStatus
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    resolved_url: str | None
    url_refresh_count: int
    temporary_relative_path: str | None
    final_relative_path: str | None
    bytes_received: int
    size_bytes: int | None
    sha256: str | None
    attempt_count: int
    http_etag: str | None
    http_last_modified: str | None
    error_code: str | None
    error_message: str | None


# ---------------------------------------------------------------------------
# Repository 接口用到的输入/输出类型（接口设计 §6）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpsertResult:
    entity_id: str
    created: bool  # True=新增，False=更新（用于 items_added/updated 统计）


@dataclass(frozen=True)
class DisappearanceSummary:
    releases_disappeared: int
    artifacts_disappeared: int


@dataclass(frozen=True)
class ArtifactContext:
    """Artifact 及其完整所属链，供下载用例构造归档路径与刷新请求。"""

    source: FirmwareSource
    product: Product
    hardware_revision: HardwareRevision
    release: FirmwareRelease
    artifact: FirmwareArtifact


@dataclass(frozen=True)
class CrawlStats:
    """一次采集的聚合统计，收尾时写入 crawl_runs。"""

    products_seen: int = 0
    releases_seen: int = 0
    artifacts_seen: int = 0
    items_added: int = 0
    items_updated: int = 0
    items_disappeared: int = 0
    items_skipped: int = 0
    error_count: int = 0


@dataclass(frozen=True)
class DownloadPatch:
    """下载记录的一次状态变迁。status 必填；其余字段为 None 表示保持原值不变。"""

    status: DownloadStatus
    verification_status: VerificationStatus | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    resolved_url: str | None = None
    url_refresh_count: int | None = None
    temporary_relative_path: str | None = None
    final_relative_path: str | None = None
    bytes_received: int | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    attempt_count: int | None = None
    http_etag: str | None = None
    http_last_modified: str | None = None
    error_code: str | None = None
    error_message: str | None = None
