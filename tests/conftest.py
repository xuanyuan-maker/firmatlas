"""Repository 测试共用 fixture：临时数据库、UnitOfWork 工厂与领域对象构造器。"""

from datetime import date

import pytest

from firmatlas.domain.candidates import (
    FirmwareArtifactCandidate,
    FirmwareReleaseCandidate,
    HardwareRevisionCandidate,
    ProductCandidate,
)
from firmatlas.domain.ids import new_id
from firmatlas.domain.model import (
    ArtifactType,
    DiscoveryMethod,
    FirmwareSource,
    ProductFamily,
    ProductType,
)
from firmatlas.domain.timeutil import utc_now
from firmatlas.infra.database import initialize, open_database
from firmatlas.infra.repository import SqliteUnitOfWorkFactory


@pytest.fixture
def engine(tmp_path):
    result = initialize(tmp_path / "data")
    engine = open_database(result.data_dir)
    yield engine
    engine.dispose()


@pytest.fixture
def uow_factory(engine):
    return SqliteUnitOfWorkFactory(engine)


@pytest.fixture
def seeded_source(uow_factory, make_source):
    """预先入库一个 tp-link-cn 来源，返回其领域对象。"""
    source = make_source()
    with uow_factory.begin() as uow:
        uow.sources.ensure_seed_sources([source])
    return source


@pytest.fixture
def make_source():
    """构造一个可入库的 FirmwareSource，字段可用关键字参数覆盖。"""

    def _make(**overrides) -> FirmwareSource:
        now = utc_now()
        fields = {
            "id": new_id(),
            "vendor_key": "tp-link",
            "vendor_name": "TP-Link",
            "source_key": "tp-link-cn",
            "name": "TP-Link 中国官网",
            "region_code": "CN",
            "locale": "zh-CN",
            "base_url": "https://www.tp-link.com.cn/",
            "adapter_key": "tp_link_cn",
            "discovery_method": DiscoveryMethod.API,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        }
        fields.update(overrides)
        return FirmwareSource(**fields)

    return _make


@pytest.fixture
def seeded_run(uow_factory, seeded_source):
    """预先创建一次 running 状态的采集运行（目录写入的 last_seen_run_id 外键需要）。"""
    with uow_factory.begin() as uow:
        return uow.runs.create_run(source_id=seeded_source.id, started_at=utc_now())


@pytest.fixture
def make_artifact_candidate():
    def _make(**overrides) -> FirmwareArtifactCandidate:
        fields = {
            "source_key": "artifact-1",
            "artifact_type": ArtifactType.FIRMWARE,
            "original_filename": "TL-WR841N_v14.bin",
            "download_url": "https://example.com/fw/TL-WR841N_v14.zip",
            "url_expires_at": None,
            "advertised_size": 4_194_304,
            "media_type": "application/zip",
            "official_checksum": None,
        }
        fields.update(overrides)
        return FirmwareArtifactCandidate(**fields)

    return _make


@pytest.fixture
def make_release_candidate(make_artifact_candidate):
    def _make(**overrides) -> FirmwareReleaseCandidate:
        fields = {
            "source_key": "release-1",
            "version_raw": "TL-WR841N V14 20260501",
            "version_normalized": "20260501",
            "release_date": date(2026, 5, 1),
            "title": "TL-WR841N V14 升级软件",
            "release_notes": "修复安全问题",
            "release_notes_url": None,
            "source_url": "https://example.com/product/wr841n#firmware",
            "artifacts": (make_artifact_candidate(),),
        }
        fields.update(overrides)
        return FirmwareReleaseCandidate(**fields)

    return _make


@pytest.fixture
def make_revision_candidate(make_release_candidate):
    def _make(**overrides) -> HardwareRevisionCandidate:
        fields = {
            "source_key": "v14",
            "raw_revision": "V14.0",
            "normalized_revision": "v14",
            "revision_explicit": True,
            "source_url": "https://example.com/product/wr841n",
            "releases": (make_release_candidate(),),
        }
        fields.update(overrides)
        return HardwareRevisionCandidate(**fields)

    return _make


@pytest.fixture
def make_product_candidate(make_revision_candidate):
    def _make(**overrides) -> ProductCandidate:
        fields = {
            "source_key": "wr841n",
            "display_name": "TL-WR841N 无线路由器",
            "model_raw": "TL-WR841N",
            "model_normalized": "tl-wr841n",
            "series": "TL-WR",
            "product_family": ProductFamily.ROUTER,
            "product_type": ProductType.HOME_ROUTER,
            "source_category": "无线路由器",
            "source_url": "https://example.com/product/wr841n",
            "hardware_revisions": (make_revision_candidate(),),
        }
        fields.update(overrides)
        return ProductCandidate(**fields)

    return _make
