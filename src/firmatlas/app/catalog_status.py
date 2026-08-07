"""Catalog 本地状态查询用例。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from firmatlas.app.catalog_manifest import CatalogManifest
from firmatlas.app.config import CatalogConfig
from firmatlas.infra.catalog_source import read_local_manifest


@dataclass(frozen=True)
class CatalogStatusReport:
    mode: str
    manifest_url: str | None
    local_manifest_path: Path
    local_manifest: CatalogManifest | None


def get_catalog_status(*, data_dir: Path, config: CatalogConfig) -> CatalogStatusReport:
    return CatalogStatusReport(
        mode=config.mode,
        manifest_url=config.manifest_url,
        local_manifest_path=data_dir / "catalog-manifest.json",
        local_manifest=read_local_manifest(data_dir),
    )
