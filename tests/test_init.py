"""firmatlas init 与数据库打开逻辑的测试（AC-03、AC-32）。"""

import pytest
import sqlalchemy as sa
from click.testing import CliRunner

from firmatlas.cli.main import cli
from firmatlas.domain.errors import DatabaseNotInitializedError, SchemaVersionMismatchError
from firmatlas.infra import database
from firmatlas.infra.schema import SCHEMA_VERSION


def read_user_version(db_path):
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return conn.exec_driver_sql("PRAGMA user_version").scalar_one()
    finally:
        engine.dispose()


def set_user_version(db_path, version):
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f"PRAGMA user_version = {version}")
    finally:
        engine.dispose()


def test_initialize_creates_layout_and_stamps_version(tmp_path):
    result = database.initialize(tmp_path / "data")

    assert result.created is True
    assert result.schema_version == SCHEMA_VERSION
    for sub in ("firmware", "tmp/downloads", "cache/http", "logs"):
        assert (tmp_path / "data" / sub).is_dir()
    assert result.db_path.is_file()
    assert read_user_version(result.db_path) == SCHEMA_VERSION


def test_initialize_is_idempotent_and_keeps_data(tmp_path):
    first = database.initialize(tmp_path / "data")
    # 初始化后写入一行，验证再次 init 不会动已有数据
    engine = database.create_engine(first.db_path)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO firmware_sources (id, vendor_key, vendor_name, source_key, name,"
                " region_code, base_url, adapter_key, discovery_method, enabled,"
                " created_at, updated_at) VALUES ('src-1', 'tp-link', 'TP-Link', 'tp-link-cn',"
                " 'TP-Link 中国官网', 'CN', 'https://example.invalid/', 'tp-link-cn', 'api', 1,"
                " '2026-07-16T00:00:00Z', '2026-07-16T00:00:00Z')"
            )
    finally:
        engine.dispose()

    second = database.initialize(tmp_path / "data")

    assert second.created is False
    engine = database.create_engine(second.db_path)
    try:
        with engine.connect() as conn:
            count = conn.exec_driver_sql("SELECT count(*) FROM firmware_sources").scalar_one()
    finally:
        engine.dispose()
    assert count == 1


def test_initialize_rejects_unknown_schema_version(tmp_path):
    result = database.initialize(tmp_path / "data")
    set_user_version(result.db_path, 99)

    with pytest.raises(SchemaVersionMismatchError):
        database.initialize(tmp_path / "data")


def test_open_database_returns_engine_with_foreign_keys_on(tmp_path):
    database.initialize(tmp_path / "data")

    engine = database.open_database(tmp_path / "data")
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    finally:
        engine.dispose()


def test_open_database_requires_init_first(tmp_path):
    with pytest.raises(DatabaseNotInitializedError):
        database.open_database(tmp_path / "data")


def test_open_database_rejects_version_mismatch(tmp_path):
    result = database.initialize(tmp_path / "data")
    set_user_version(result.db_path, 99)

    with pytest.raises(SchemaVersionMismatchError):
        database.open_database(tmp_path / "data")
    # 拒绝打开的同时不得修改版本戳（不静默删改）
    assert read_user_version(result.db_path) == 99


def test_cli_init_twice_reports_idempotent(tmp_path):
    runner = CliRunner()
    args = ["--data-dir", str(tmp_path / "data"), "init"]

    first = runner.invoke(cli, args)
    second = runner.invoke(cli, args)

    assert first.exit_code == 0, first.output
    assert "已初始化数据库" in first.output
    assert second.exit_code == 0, second.output
    assert "未做改动" in second.output


def test_cli_init_reports_version_mismatch_as_error(tmp_path):
    runner = CliRunner()
    args = ["--data-dir", str(tmp_path / "data"), "init"]
    runner.invoke(cli, args)
    set_user_version(tmp_path / "data" / "firmatlas.db", 99)

    result = runner.invoke(cli, args)

    assert result.exit_code != 0
    assert "结构版本" in result.output
