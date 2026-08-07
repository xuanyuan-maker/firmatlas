"""Catalog 更新前置检查与差异报告。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from firmatlas.app.catalog_manifest import CatalogCounts, CatalogManifest
from firmatlas.app.config import AppConfig
from firmatlas.domain.errors import CatalogUpdateError
from firmatlas.domain.timeutil import format_rfc3339
from firmatlas.infra.catalog_snapshot import CatalogSnapshotStats, inspect_clean_database
from firmatlas.infra.catalog_source import fetch_manifest, read_local_manifest


@dataclass(frozen=True)
class CatalogCheckReport:
    current_lineage_id: str | None
    current_catalog_version: str | None
    remote_lineage_id: str
    remote_catalog_version: str
    update_available: bool
    replace_required: bool
    current_counts: CatalogCounts | None
    remote_counts: CatalogCounts


def check_catalog_update(*, data_dir: Path, config: AppConfig) -> CatalogCheckReport:
    if config.catalog.mode != "managed":
        raise CatalogUpdateError("Standalone 模式不允许执行 Catalog 更新。")
    assert config.catalog.manifest_url is not None
    remote = fetch_manifest(
        config.catalog.manifest_url,
        allow_insecure_http=config.catalog.allow_insecure_http,
        timeout=config.http.request_timeout,
    )
    local = read_local_manifest(data_dir)
    current_version = local.catalog_version if local else None
    update_available = local is None or remote.catalog_version > local.catalog_version
    replace_required = (
        local is None
        or remote.lineage_id != local.lineage_id
        or remote.schema_version != local.schema_version
    )
    return CatalogCheckReport(
        current_lineage_id=local.lineage_id if local else None,
        current_catalog_version=current_version,
        remote_lineage_id=remote.lineage_id,
        remote_catalog_version=remote.catalog_version,
        update_available=update_available,
        replace_required=replace_required,
        current_counts=local.counts if local else None,
        remote_counts=remote.counts,
    )


def validate_candidate_database(
    *, database_path: Path, manifest: CatalogManifest
) -> CatalogSnapshotStats:
    """校验候选库结构、纯净约束、总计数和来源统计。"""
    stats = inspect_clean_database(database_path)
    if manifest.schema_version != 1:
        raise CatalogUpdateError(
            f"manifest schema_version={manifest.schema_version} 与当前程序不兼容。"
        )
    actual_counts = CatalogCounts(
        sources=stats.sources,
        products=stats.products,
        releases=stats.releases,
        artifacts=stats.artifacts,
        downloads=stats.downloads,
    )
    if actual_counts != manifest.counts:
        raise CatalogUpdateError(
            f"候选数据库计数 {actual_counts} 与 manifest 计数 {manifest.counts} 不一致。"
        )
    expected_sources = {source.source_key: source for source in manifest.sources}
    actual_sources = {source.source_key: source for source in stats.source_stats}
    if set(expected_sources) != set(actual_sources):
        raise CatalogUpdateError("候选数据库来源集合与 manifest 不一致。")
    for source_key, expected in expected_sources.items():
        actual = actual_sources[source_key]
        if (
            actual.last_success_at
            != (
                format_rfc3339(expected.last_success_at)
                if expected.last_success_at is not None
                else None
            )
            or actual.last_status != expected.last_status
            or actual.products != expected.products
            or actual.releases != expected.releases
            or actual.artifacts != expected.artifacts
        ):
            raise CatalogUpdateError(f"来源 {source_key} 的 manifest 统计与候选数据库不一致。")
    return stats
