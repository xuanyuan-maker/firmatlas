"""SQLite 表结构定义（README 0x0B）。

本模块是 7 张表的唯一定义处，只允许基础设施层（infra）import。
通用规则（README 0x0B「通用数据库规则」）：
- 主键为应用生成的不透明 TEXT ID；
- 时间为 UTC RFC 3339 文本，发布日期为 YYYY-MM-DD 文本；
- 布尔用 INTEGER 且限制为 0/1；
- 外键一律 ON DELETE RESTRICT。
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)

# 当前表结构对应的 PRAGMA user_version 值（README 0x0C）
SCHEMA_VERSION = 1

metadata = MetaData()

# 1. firmware_sources：厂商、地区和适配器来源
firmware_sources = Table(
    "firmware_sources",
    metadata,
    Column("id", Text, primary_key=True),
    Column("vendor_key", Text, nullable=False),
    Column("vendor_name", Text, nullable=False),
    Column("source_key", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("region_code", Text, nullable=False),
    Column("locale", Text, nullable=True),
    Column("base_url", Text, nullable=False),
    Column("adapter_key", Text, nullable=False),
    Column("discovery_method", Text, nullable=False),
    Column("enabled", Integer, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint(
        "discovery_method IN ('api', 'html', 'hybrid')",
        name="ck_sources_discovery_method",
    ),
    CheckConstraint("enabled IN (0, 1)", name="ck_sources_enabled"),
    # 地区代码限定为两字母（CN、US 等），防止整段地区名误入库
    CheckConstraint("length(region_code) = 2", name="ck_sources_region_code_length"),
)

# 2. products：来源下的产品
products = Table(
    "products",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "source_id", Text, ForeignKey("firmware_sources.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("source_key", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("model_raw", Text, nullable=False),
    Column("model_normalized", Text, nullable=False),
    Column("series", Text, nullable=True),
    Column("product_family", Text, nullable=False),
    Column("product_type", Text, nullable=False),
    Column("source_category", Text, nullable=True),
    Column("source_url", Text, nullable=False),
    Column("first_seen_at", Text, nullable=False),
    Column("last_seen_at", Text, nullable=False),
    Column(
        "last_seen_run_id", Text, ForeignKey("crawl_runs.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    UniqueConstraint("source_id", "source_key", name="uq_products_source_id_source_key"),
    # 分类组合约束：router 只能配四种路由器类型，camera 只能配 camera
    CheckConstraint(
        "(product_family = 'router' AND product_type IN "
        "('home_router', 'mesh_router', 'wireless_ap', 'cellular_cpe'))"
        " OR (product_family = 'camera' AND product_type = 'camera')",
        name="ck_products_family_type",
    ),
)

# 3. hardware_revisions：产品硬件版本
hardware_revisions = Table(
    "hardware_revisions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("product_id", Text, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
    Column("source_key", Text, nullable=False),
    Column("raw_revision", Text, nullable=True),
    Column("normalized_revision", Text, nullable=False),
    Column("revision_explicit", Integer, nullable=False),
    Column("source_url", Text, nullable=True),
    Column("first_seen_at", Text, nullable=False),
    Column("last_seen_at", Text, nullable=False),
    Column(
        "last_seen_run_id", Text, ForeignKey("crawl_runs.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    UniqueConstraint("product_id", "source_key", name="uq_revisions_product_id_source_key"),
    CheckConstraint("revision_explicit IN (0, 1)", name="ck_revisions_revision_explicit"),
)

# 4. firmware_releases：固件发布
firmware_releases = Table(
    "firmware_releases",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "hardware_revision_id",
        Text,
        ForeignKey("hardware_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("source_key", Text, nullable=False),
    Column("version_raw", Text, nullable=False),
    Column("version_normalized", Text, nullable=True),
    Column("release_date", Text, nullable=True),
    Column("title", Text, nullable=True),
    Column("release_notes", Text, nullable=True),
    Column("release_notes_url", Text, nullable=True),
    Column("source_url", Text, nullable=False),
    Column("visibility_status", Text, nullable=False),
    Column("first_seen_at", Text, nullable=False),
    Column("last_seen_at", Text, nullable=False),
    Column("disappeared_at", Text, nullable=True),
    Column(
        "last_seen_run_id", Text, ForeignKey("crawl_runs.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    UniqueConstraint(
        "hardware_revision_id", "source_key", name="uq_releases_revision_id_source_key"
    ),
    CheckConstraint(
        "visibility_status IN ('active', 'disappeared')",
        name="ck_releases_visibility_status",
    ),
)

# 5. firmware_artifacts：实际下载资源
firmware_artifacts = Table(
    "firmware_artifacts",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "release_id", Text, ForeignKey("firmware_releases.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("source_key", Text, nullable=False),
    Column("artifact_type", Text, nullable=False),
    Column("original_filename", Text, nullable=True),
    Column("download_url", Text, nullable=False),
    Column("url_last_resolved_at", Text, nullable=False),
    Column("url_expires_at", Text, nullable=True),
    Column("advertised_size", Integer, nullable=True),
    Column("media_type", Text, nullable=True),
    Column("official_checksum_algorithm", Text, nullable=True),
    Column("official_checksum_value", Text, nullable=True),
    Column("visibility_status", Text, nullable=False),
    Column("first_seen_at", Text, nullable=False),
    Column("last_seen_at", Text, nullable=False),
    Column("disappeared_at", Text, nullable=True),
    Column(
        "last_seen_run_id", Text, ForeignKey("crawl_runs.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    UniqueConstraint("release_id", "source_key", name="uq_artifacts_release_id_source_key"),
    CheckConstraint(
        "artifact_type IN ('firmware', 'recovery', 'other_firmware')",
        name="ck_artifacts_artifact_type",
    ),
    CheckConstraint(
        "visibility_status IN ('active', 'disappeared')",
        name="ck_artifacts_visibility_status",
    ),
)

# 6. crawl_runs：采集运行及完整性
crawl_runs = Table(
    "crawl_runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "source_id", Text, ForeignKey("firmware_sources.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("status", Text, nullable=False),
    Column("is_complete", Integer, nullable=False),
    Column("started_at", Text, nullable=False),
    Column("finished_at", Text, nullable=True),
    Column("products_seen", Integer, nullable=False, server_default=text("0")),
    Column("releases_seen", Integer, nullable=False, server_default=text("0")),
    Column("artifacts_seen", Integer, nullable=False, server_default=text("0")),
    Column("items_added", Integer, nullable=False, server_default=text("0")),
    Column("items_updated", Integer, nullable=False, server_default=text("0")),
    Column("items_disappeared", Integer, nullable=False, server_default=text("0")),
    Column("items_skipped", Integer, nullable=False, server_default=text("0")),
    Column("error_count", Integer, nullable=False, server_default=text("0")),
    Column("error_summary", Text, nullable=True),
    Column("issues_json", Text, nullable=False, server_default=text("'[]'")),
    Column("created_at", Text, nullable=False),
    CheckConstraint(
        "status IN ('running', 'completed', 'partial', 'failed', 'cancelled')",
        name="ck_runs_status",
    ),
    CheckConstraint("is_complete IN (0, 1)", name="ck_runs_is_complete"),
    # is_complete = 1 时 status 必须为 completed
    CheckConstraint(
        "is_complete = 0 OR status = 'completed'", name="ck_runs_complete_implies_completed"
    ),
    # running 时 finished_at 必须为 NULL
    CheckConstraint(
        "status != 'running' OR finished_at IS NULL", name="ck_runs_running_not_finished"
    ),
)

# 7. download_records：下载、校验、哈希和本地路径
download_records = Table(
    "download_records",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "artifact_id",
        Text,
        ForeignKey("firmware_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("status", Text, nullable=False),
    Column("verification_status", Text, nullable=False),
    Column("requested_at", Text, nullable=False),
    Column("started_at", Text, nullable=True),
    Column("finished_at", Text, nullable=True),
    Column("resolved_url", Text, nullable=True),
    Column("url_refresh_count", Integer, nullable=False, server_default=text("0")),
    Column("temporary_relative_path", Text, nullable=True),
    Column("final_relative_path", Text, nullable=True),
    Column("bytes_received", Integer, nullable=False, server_default=text("0")),
    Column("size_bytes", Integer, nullable=True),
    Column("sha256", Text, nullable=True),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("http_etag", Text, nullable=True),
    Column("http_last_modified", Text, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("error_message", Text, nullable=True),
    CheckConstraint(
        "status IN ('queued', 'downloading', 'completed', 'failed', 'cancelled', 'interrupted')",
        name="ck_downloads_status",
    ),
    CheckConstraint(
        "verification_status IN ('not_checked', 'not_available', 'verified', 'mismatch')",
        name="ck_downloads_verification_status",
    ),
    # 失效地址最多自动刷新一次
    CheckConstraint("url_refresh_count IN (0, 1)", name="ck_downloads_url_refresh_count"),
    # completed 必须具有最终路径、大小和哈希
    CheckConstraint(
        "status != 'completed' OR (final_relative_path IS NOT NULL"
        " AND size_bytes IS NOT NULL AND sha256 IS NOT NULL)",
        name="ck_downloads_completed_has_result",
    ),
    # 校验不一致（mismatch）不能对应正常完成归档
    CheckConstraint(
        "NOT (status = 'completed' AND verification_status = 'mismatch')",
        name="ck_downloads_mismatch_not_completed",
    ),
)

# 推荐索引（README 0x0B「推荐索引」）
Index("ix_products_source_family_type", products.c.source_id, products.c.product_family,
      products.c.product_type)
Index("ix_products_model_normalized", products.c.model_normalized)
Index("ix_revisions_product_normalized", hardware_revisions.c.product_id,
      hardware_revisions.c.normalized_revision)
Index("ix_releases_revision_version", firmware_releases.c.hardware_revision_id,
      firmware_releases.c.version_normalized)
Index("ix_releases_visibility_date", firmware_releases.c.visibility_status,
      firmware_releases.c.release_date)
Index("ix_artifacts_release_visibility", firmware_artifacts.c.release_id,
      firmware_artifacts.c.visibility_status)
Index("ix_runs_source_started", crawl_runs.c.source_id, crawl_runs.c.started_at)
Index("ix_downloads_artifact_requested", download_records.c.artifact_id,
      download_records.c.requested_at)
Index("ix_downloads_status_requested", download_records.c.status, download_records.c.requested_at)
Index("ix_downloads_sha256", download_records.c.sha256)

# 部分唯一索引：同一 Artifact 同时最多一个 queued/downloading 记录（AC-30 兜底）
Index(
    "uq_downloads_one_active_per_artifact",
    download_records.c.artifact_id,
    unique=True,
    sqlite_where=text("status IN ('queued', 'downloading')"),
)
