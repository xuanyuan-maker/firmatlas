"""Repository 与 UnitOfWork 的 SQLite 实现（接口设计 §6）。

约定：
- 只有本模块（和 database.py、schema.py）允许接触 SQLAlchemy；
  对外输入输出一律是 domain 包里的 dataclass 与枚举（AC-19）；
- 所有 SQLAlchemy/SQLite 异常在离开本模块前包装成 RepositoryError，
  原始异常挂在 __cause__ 上供调试，错误消息不携带 SQL 字符串；
- 时间在数据库中是 RFC 3339 文本，读写时经 timeutil 与 datetime 互转。
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime

import sqlalchemy as sa

from firmatlas.domain.errors import FirmAtlasError, RepositoryError
from firmatlas.domain.model import DiscoveryMethod, FirmwareSource
from firmatlas.domain.timeutil import format_rfc3339, parse_rfc3339
from firmatlas.infra import schema


@contextmanager
def _wrap_errors(operation: str) -> Iterator[None]:
    """把底层数据库异常统一转换成 RepositoryError；自家异常原样放行。"""
    try:
        yield
    except FirmAtlasError:
        raise
    except sa.exc.SQLAlchemyError as exc:
        raise RepositoryError(f"{operation}失败（{type(exc).__name__}）") from exc


def _opt_parse(text: str | None) -> datetime | None:
    return parse_rfc3339(text) if text is not None else None


def _opt_format(value: datetime | None) -> str | None:
    return format_rfc3339(value) if value is not None else None


def _source_from_row(row: sa.Row) -> FirmwareSource:
    return FirmwareSource(
        id=row.id,
        vendor_key=row.vendor_key,
        vendor_name=row.vendor_name,
        source_key=row.source_key,
        name=row.name,
        region_code=row.region_code,
        locale=row.locale,
        base_url=row.base_url,
        adapter_key=row.adapter_key,
        discovery_method=DiscoveryMethod(row.discovery_method),
        enabled=bool(row.enabled),
        created_at=parse_rfc3339(row.created_at),
        updated_at=parse_rfc3339(row.updated_at),
    )


class SqliteSourceRepository:
    def __init__(self, conn: sa.Connection) -> None:
        self._conn = conn

    def list_sources(self) -> list[FirmwareSource]:
        t = schema.firmware_sources
        with _wrap_errors("查询来源列表"):
            rows = self._conn.execute(sa.select(t).order_by(t.c.source_key)).all()
        return [_source_from_row(row) for row in rows]

    def get_by_source_key(self, source_key: str) -> FirmwareSource | None:
        t = schema.firmware_sources
        with _wrap_errors("查询来源"):
            row = self._conn.execute(sa.select(t).where(t.c.source_key == source_key)).first()
        return _source_from_row(row) if row is not None else None

    def ensure_seed_sources(self, seeds: Sequence[FirmwareSource]) -> None:
        t = schema.firmware_sources
        with _wrap_errors("写入内置来源"):
            for seed in seeds:
                exists = self._conn.execute(
                    sa.select(t.c.id).where(t.c.source_key == seed.source_key)
                ).first()
                if exists is not None:
                    continue
                self._conn.execute(
                    t.insert().values(
                        id=seed.id,
                        vendor_key=seed.vendor_key,
                        vendor_name=seed.vendor_name,
                        source_key=seed.source_key,
                        name=seed.name,
                        region_code=seed.region_code,
                        locale=seed.locale,
                        base_url=seed.base_url,
                        adapter_key=seed.adapter_key,
                        discovery_method=seed.discovery_method.value,
                        enabled=int(seed.enabled),
                        created_at=format_rfc3339(seed.created_at),
                        updated_at=format_rfc3339(seed.updated_at),
                    )
                )


class SqliteUnitOfWork:
    """一次事务内可用的各 Repository 集合。由工厂创建，业务层不直接构造。"""

    def __init__(self, conn: sa.Connection) -> None:
        self.sources = SqliteSourceRepository(conn)


class SqliteUnitOfWorkFactory:
    """用法：with factory.begin() as uow: ...  正常退出提交，抛异常回滚。"""

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    @contextmanager
    def begin(self) -> Iterator[SqliteUnitOfWork]:
        try:
            with self._engine.begin() as conn:
                yield SqliteUnitOfWork(conn)
        except FirmAtlasError:
            raise
        except sa.exc.SQLAlchemyError as exc:
            raise RepositoryError(f"数据库事务失败（{type(exc).__name__}）") from exc
