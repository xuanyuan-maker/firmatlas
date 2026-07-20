"""SQLite 数据库的初始化与打开（需求分析 0x0C/0x0D）。

- `initialize`：`firmatlas init` 的核心，幂等创建目录骨架、建表、盖版本戳。
- `open_database`：其余命令打开数据库的唯一入口，版本不匹配即拒绝，
  绝不静默删改（AC-32）。
"""

from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

from firmatlas.domain.errors import DatabaseNotInitializedError, SchemaVersionMismatchError
from firmatlas.infra.schema import SCHEMA_VERSION, metadata

DB_FILENAME = "firmatlas.db"

# data/ 下需要预创建的子目录（需求分析 0x0D）
DATA_SUBDIRS = ("firmware", "tmp/downloads", "cache/http", "logs")


def create_engine(db_path: Path) -> sa.Engine:
    """创建 SQLite 引擎；每个新连接自动开启外键检查。"""
    engine = sa.create_engine(f"sqlite:///{db_path}")

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    return engine


def _read_user_version(conn: sa.Connection) -> int:
    return conn.exec_driver_sql("PRAGMA user_version").scalar_one()


@dataclass(frozen=True)
class InitResult:
    data_dir: Path
    db_path: Path
    created: bool  # True=本次建表并盖版本戳；False=数据库此前已初始化
    schema_version: int


def initialize(data_dir: Path) -> InitResult:
    """初始化数据目录与数据库，可重复执行（AC-03）。

    - 版本戳为 0（全新或未盖戳的库）：建缺失表并盖戳为 SCHEMA_VERSION。
    - 版本戳等于 SCHEMA_VERSION：视为已初始化，不做改动。
    - 其他版本：抛 SchemaVersionMismatchError，不碰数据库内容。
    """
    for sub in DATA_SUBDIRS:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    db_path = data_dir / DB_FILENAME
    engine = create_engine(db_path)
    try:
        with engine.begin() as conn:
            version = _read_user_version(conn)
            if version == SCHEMA_VERSION:
                return InitResult(
                    data_dir=data_dir,
                    db_path=db_path,
                    created=False,
                    schema_version=SCHEMA_VERSION,
                )
            if version != 0:
                raise SchemaVersionMismatchError(
                    f"数据库 {db_path} 的结构版本为 {version}，"
                    f"当前程序期望 {SCHEMA_VERSION}；已停止操作，未做任何修改。"
                )
            metadata.create_all(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        engine.dispose()
    return InitResult(
        data_dir=data_dir, db_path=db_path, created=True, schema_version=SCHEMA_VERSION
    )


def open_database(data_dir: Path) -> sa.Engine:
    """打开已初始化的数据库，返回可复用的 Engine。

    数据库不存在或版本不匹配时直接抛异常，由 CLI 转成明确提示。
    """
    db_path = data_dir / DB_FILENAME
    if not db_path.exists():
        raise DatabaseNotInitializedError(
            f"数据库 {db_path} 不存在，请先运行 firmatlas init。"
        )
    engine = create_engine(db_path)
    try:
        with engine.connect() as conn:
            version = _read_user_version(conn)
    except BaseException:
        engine.dispose()
        raise
    if version != SCHEMA_VERSION:
        engine.dispose()
        raise SchemaVersionMismatchError(
            f"数据库 {db_path} 的结构版本为 {version}，当前程序期望 {SCHEMA_VERSION}；"
            f"拒绝打开，不会自动删除或修改数据库。"
        )
    return engine
