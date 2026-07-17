"""表结构测试：验证 7 张表可建出，且关键约束在 SQLite 层真的生效。"""

import pytest
import sqlalchemy as sa

from firmatlas.infra import schema

NOW = "2026-07-16T00:00:00Z"


@pytest.fixture()
def conn():
    """内存 SQLite 连接：已建全部表并开启外键检查。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        schema.metadata.create_all(connection)
        yield connection


def source_row(**overrides):
    row = {
        "id": "src-1",
        "vendor_key": "tp-link",
        "vendor_name": "TP-Link",
        "source_key": "tp-link-cn",
        "name": "TP-Link 中国官网",
        "region_code": "CN",
        "locale": "zh-CN",
        "base_url": "https://example.invalid/",
        "adapter_key": "tp-link-cn",
        "discovery_method": "api",
        "enabled": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def run_row(**overrides):
    row = {
        "id": "run-1",
        "source_id": "src-1",
        "status": "running",
        "is_complete": 0,
        "started_at": NOW,
        "created_at": NOW,
    }
    row.update(overrides)
    return row


def product_row(**overrides):
    row = {
        "id": "prod-1",
        "source_id": "src-1",
        "source_key": "archer-ax23",
        "display_name": "Archer AX23",
        "model_raw": "Archer AX23",
        "model_normalized": "archer-ax23",
        "product_family": "router",
        "product_type": "router",
        "source_url": "https://example.invalid/archer-ax23",
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "last_seen_run_id": "run-1",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def artifact_row(**overrides):
    row = {
        "id": "art-1",
        "release_id": "rel-1",
        "source_key": "fw-main",
        "artifact_type": "firmware",
        "download_url": "https://example.invalid/fw.bin",
        "url_last_resolved_at": NOW,
        "visibility_status": "active",
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "last_seen_run_id": "run-1",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def download_row(**overrides):
    row = {
        "id": "dl-1",
        "artifact_id": "art-1",
        "status": "queued",
        "verification_status": "not_checked",
        "requested_at": NOW,
    }
    row.update(overrides)
    return row


def insert_artifact_chain(conn):
    """插入 source → run → product → revision → release → artifact 的最小合法链。"""
    conn.execute(sa.insert(schema.firmware_sources), source_row())
    conn.execute(sa.insert(schema.crawl_runs), run_row())
    conn.execute(sa.insert(schema.products), product_row())
    conn.execute(
        sa.insert(schema.hardware_revisions),
        {
            "id": "rev-1",
            "product_id": "prod-1",
            "source_key": "v1",
            "normalized_revision": "v1",
            "revision_explicit": 1,
            "first_seen_at": NOW,
            "last_seen_at": NOW,
            "last_seen_run_id": "run-1",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    conn.execute(
        sa.insert(schema.firmware_releases),
        {
            "id": "rel-1",
            "hardware_revision_id": "rev-1",
            "source_key": "1.2.0",
            "version_raw": "1.2.0 Build 20260101",
            "source_url": "https://example.invalid/archer-ax23#firmware",
            "visibility_status": "active",
            "first_seen_at": NOW,
            "last_seen_at": NOW,
            "last_seen_run_id": "run-1",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    conn.execute(sa.insert(schema.firmware_artifacts), artifact_row())


def test_create_all_creates_seven_tables(conn):
    names = set(sa.inspect(conn).get_table_names())
    assert names == {
        "firmware_sources",
        "products",
        "hardware_revisions",
        "firmware_releases",
        "firmware_artifacts",
        "crawl_runs",
        "download_records",
    }


def test_full_entity_chain_accepts_valid_rows(conn):
    insert_artifact_chain(conn)
    conn.execute(sa.insert(schema.download_records), download_row())


def test_discovery_method_check_rejects_illegal_value(conn):
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(sa.insert(schema.firmware_sources), source_row(discovery_method="rss"))


def test_region_code_check_rejects_non_two_letter_value(conn):
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(sa.insert(schema.firmware_sources), source_row(region_code="China"))


def test_product_family_type_combination_check(conn):
    conn.execute(sa.insert(schema.firmware_sources), source_row())
    conn.execute(sa.insert(schema.crawl_runs), run_row())
    # camera 家族不允许配路由器类型
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.insert(schema.products),
            product_row(product_family="camera", product_type="router"),
        )


def test_products_unique_source_id_source_key(conn):
    conn.execute(sa.insert(schema.firmware_sources), source_row())
    conn.execute(sa.insert(schema.crawl_runs), run_row())
    conn.execute(sa.insert(schema.products), product_row())
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(sa.insert(schema.products), product_row(id="prod-2"))


def test_foreign_key_rejects_missing_parent(conn):
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(sa.insert(schema.crawl_runs), run_row(source_id="no-such-source"))


def test_run_complete_implies_status_completed(conn):
    conn.execute(sa.insert(schema.firmware_sources), source_row())
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(sa.insert(schema.crawl_runs), run_row(is_complete=1, status="partial"))


def test_running_run_must_not_have_finished_at(conn):
    conn.execute(sa.insert(schema.firmware_sources), source_row())
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(sa.insert(schema.crawl_runs), run_row(status="running", finished_at=NOW))


def test_completed_download_requires_path_size_sha256(conn):
    insert_artifact_chain(conn)
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.insert(schema.download_records),
            download_row(status="completed", verification_status="verified"),
        )


def test_only_one_active_download_per_artifact(conn):
    insert_artifact_chain(conn)
    conn.execute(sa.insert(schema.download_records), download_row(id="dl-1", status="queued"))
    # 同一 Artifact 第二条活动记录（downloading）被部分唯一索引拒绝
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.insert(schema.download_records), download_row(id="dl-2", status="downloading")
        )
    # 已结束的记录不受限制：failed 后允许再次排队
    conn.execute(
        sa.insert(schema.download_records),
        download_row(id="dl-3", artifact_id="art-1", status="failed"),
    )


def test_finished_downloads_are_not_limited_by_partial_index(conn):
    insert_artifact_chain(conn)
    conn.execute(sa.insert(schema.download_records), download_row(id="dl-1", status="failed"))
    conn.execute(sa.insert(schema.download_records), download_row(id="dl-2", status="cancelled"))
    conn.execute(sa.insert(schema.download_records), download_row(id="dl-3", status="queued"))
