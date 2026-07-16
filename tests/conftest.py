"""Repository 测试共用 fixture：临时数据库、UnitOfWork 工厂与领域对象构造器。"""

import pytest

from firmatlas.domain.ids import new_id
from firmatlas.domain.model import DiscoveryMethod, FirmwareSource
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
