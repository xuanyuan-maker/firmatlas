"""SourceRepository 与 UnitOfWork 事务边界的测试。"""

from datetime import datetime

import pytest

from firmatlas.domain.errors import RepositoryError
from firmatlas.domain.model import FirmwareSource


def test_ensure_seed_sources_inserts_and_is_idempotent(uow_factory, make_source):
    seed = make_source()
    for _ in range(2):  # 重复执行不产生重复行（AC-03 幂等 init 的一环）
        with uow_factory.begin() as uow:
            uow.sources.ensure_seed_sources([seed])
    with uow_factory.begin() as uow:
        sources = uow.sources.list_sources()
    assert len(sources) == 1
    assert sources[0].source_key == "tp-link-cn"


def test_list_sources_returns_domain_objects_only(uow_factory, make_source):
    with uow_factory.begin() as uow:
        uow.sources.ensure_seed_sources(
            [make_source(), make_source(source_key="tp-link-us", region_code="US")]
        )
        sources = uow.sources.list_sources()
    # 不泄漏 SQLAlchemy Row：返回的是领域 dataclass，布尔/时间已转成原生类型（AC-19）
    assert all(isinstance(s, FirmwareSource) for s in sources)
    assert isinstance(sources[0].enabled, bool)
    assert isinstance(sources[0].created_at, datetime)
    assert sources[0].created_at.tzinfo is not None
    assert [s.source_key for s in sources] == ["tp-link-cn", "tp-link-us"]


def test_get_by_source_key(uow_factory, make_source):
    seed = make_source()
    with uow_factory.begin() as uow:
        uow.sources.ensure_seed_sources([seed])
        found = uow.sources.get_by_source_key("tp-link-cn")
        missing = uow.sources.get_by_source_key("no-such-source")
    assert found is not None
    assert found.id == seed.id
    assert found.locale == "zh-CN"
    assert missing is None


def test_uow_rolls_back_on_exception(uow_factory, make_source):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with uow_factory.begin() as uow:
            uow.sources.ensure_seed_sources([make_source()])
            raise Boom()
    with uow_factory.begin() as uow:
        assert uow.sources.list_sources() == []


def test_database_errors_are_wrapped_as_repository_error(uow_factory, make_source):
    # region_code 长度违反 CHECK 约束，触发底层 IntegrityError，
    # 应以 RepositoryError 暴露给调用方，而不是 SQLAlchemy 异常
    bad = make_source(region_code="CHINA")
    with pytest.raises(RepositoryError) as exc_info:
        with uow_factory.begin() as uow:
            uow.sources.ensure_seed_sources([bad])
    assert "写入内置来源" in str(exc_info.value)
