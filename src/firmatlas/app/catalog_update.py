"""Catalog 更新前置检查与差异报告。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from firmatlas.app.catalog_manifest import CatalogCounts
from firmatlas.app.config import AppConfig
from firmatlas.domain.errors import CatalogUpdateError
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
