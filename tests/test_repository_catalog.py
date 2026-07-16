"""CatalogRepository 幂等 upsert 的测试（AC-13、AC-14、AC-17）。

断言落库值时直接用 SQLAlchemy 查表：这是基础设施层自身的测试，
允许接触数据库；业务层代码则只能经 ports 接口访问。
"""

from datetime import UTC, datetime

import sqlalchemy as sa

from firmatlas.infra import schema


def _seen(day: int) -> datetime:
    return datetime(2026, 7, day, 12, 0, 0, tzinfo=UTC)


def _text(day: int) -> str:
    return f"2026-07-{day:02d}T12:00:00Z"


def _upsert_tree(uow, *, source_id, run_id, seen_at, product):
    """把一棵产品候选子树整体写入，返回各级 UpsertResult。"""
    p = uow.catalog.upsert_product(
        source_id=source_id, candidate=product, run_id=run_id, seen_at=seen_at
    )
    revision = product.hardware_revisions[0]
    r = uow.catalog.upsert_hardware_revision(
        product_id=p.entity_id, candidate=revision, run_id=run_id, seen_at=seen_at
    )
    release = revision.releases[0]
    rel = uow.catalog.upsert_release(
        hardware_revision_id=r.entity_id, candidate=release, run_id=run_id, seen_at=seen_at
    )
    art = uow.catalog.upsert_artifact(
        release_id=rel.entity_id, candidate=release.artifacts[0], run_id=run_id, seen_at=seen_at
    )
    return p, r, rel, art


def test_first_upsert_creates_whole_tree(
    engine, uow_factory, seeded_source, seeded_run, make_product_candidate
):
    with uow_factory.begin() as uow:
        results = _upsert_tree(
            uow,
            source_id=seeded_source.id,
            run_id=seeded_run.id,
            seen_at=_seen(1),
            product=make_product_candidate(),
        )
    assert all(r.created for r in results)

    with engine.connect() as conn:
        release = conn.execute(sa.select(schema.firmware_releases)).one()
        artifact = conn.execute(sa.select(schema.firmware_artifacts)).one()
    assert release.visibility_status == "active"
    assert release.release_date == "2026-05-01"
    assert release.first_seen_at == _text(1)
    assert release.last_seen_at == _text(1)
    assert artifact.visibility_status == "active"
    assert artifact.url_last_resolved_at == _text(1)
    assert artifact.last_seen_run_id == seeded_run.id


def test_reupsert_is_idempotent_and_keeps_first_seen(
    engine, uow_factory, seeded_source, seeded_run, make_product_candidate
):
    product = make_product_candidate()
    with uow_factory.begin() as uow:
        first = _upsert_tree(
            uow,
            source_id=seeded_source.id,
            run_id=seeded_run.id,
            seen_at=_seen(1),
            product=product,
        )
        run2 = uow.runs.create_run(source_id=seeded_source.id, started_at=_seen(2))
        second = _upsert_tree(
            uow, source_id=seeded_source.id, run_id=run2.id, seen_at=_seen(2), product=product
        )

    # 同一 source_key 命中同一行：不新增、ID 不变（AC-13）
    assert all(not r.created for r in second)
    assert [r.entity_id for r in first] == [r.entity_id for r in second]

    with engine.connect() as conn:
        for table in (
            schema.products,
            schema.hardware_revisions,
            schema.firmware_releases,
            schema.firmware_artifacts,
        ):
            row = conn.execute(sa.select(table)).one()  # one() 同时证明没有重复行
            assert row.first_seen_at == _text(1)  # 首次发现时间保留（AC-14）
            assert row.last_seen_at == _text(2)
            assert row.last_seen_run_id == run2.id


def test_reupsert_updates_non_identity_fields(
    engine, uow_factory, seeded_source, seeded_run, make_product_candidate
):
    with uow_factory.begin() as uow:
        _upsert_tree(
            uow,
            source_id=seeded_source.id,
            run_id=seeded_run.id,
            seen_at=_seen(1),
            product=make_product_candidate(),
        )
        renamed = make_product_candidate(display_name="TL-WR841N 无线路由器（新版页面）")
        result = uow.catalog.upsert_product(
            source_id=seeded_source.id, candidate=renamed, run_id=seeded_run.id, seen_at=_seen(2)
        )
    assert result.created is False
    with engine.connect() as conn:
        row = conn.execute(sa.select(schema.products)).one()
    assert row.display_name == "TL-WR841N 无线路由器（新版页面）"


def test_reupsert_restores_disappeared_to_active(
    engine, uow_factory, seeded_source, seeded_run, make_product_candidate
):
    product = make_product_candidate()
    with uow_factory.begin() as uow:
        _upsert_tree(
            uow,
            source_id=seeded_source.id,
            run_id=seeded_run.id,
            seen_at=_seen(1),
            product=product,
        )
    # 人为把发布和 Artifact 置为 disappeared，模拟此前的消失对账
    with engine.begin() as conn:
        for table in (schema.firmware_releases, schema.firmware_artifacts):
            conn.execute(
                table.update().values(visibility_status="disappeared", disappeared_at=_text(3))
            )

    with uow_factory.begin() as uow:
        _upsert_tree(
            uow,
            source_id=seeded_source.id,
            run_id=seeded_run.id,
            seen_at=_seen(5),
            product=product,
        )
    # 重新出现 → 恢复 active 并清空 disappeared_at（AC-17）
    with engine.connect() as conn:
        for table in (schema.firmware_releases, schema.firmware_artifacts):
            row = conn.execute(sa.select(table)).one()
            assert row.visibility_status == "active"
            assert row.disappeared_at is None


def test_artifact_url_change_refreshes_resolved_at(
    engine, uow_factory, seeded_source, seeded_run, make_product_candidate, make_artifact_candidate
):
    product = make_product_candidate()
    with uow_factory.begin() as uow:
        *_, rel, _ = _upsert_tree(
            uow,
            source_id=seeded_source.id,
            run_id=seeded_run.id,
            seen_at=_seen(1),
            product=product,
        )
        # 同一 URL 再见到：不刷新 url_last_resolved_at
        uow.catalog.upsert_artifact(
            release_id=rel.entity_id,
            candidate=make_artifact_candidate(),
            run_id=seeded_run.id,
            seen_at=_seen(2),
        )
    with engine.connect() as conn:
        row = conn.execute(sa.select(schema.firmware_artifacts)).one()
    assert row.url_last_resolved_at == _text(1)
    assert row.last_seen_at == _text(2)

    with uow_factory.begin() as uow:
        # URL 变化：刷新 url_last_resolved_at
        uow.catalog.upsert_artifact(
            release_id=rel.entity_id,
            candidate=make_artifact_candidate(download_url="https://example.com/fw/new.zip"),
            run_id=seeded_run.id,
            seen_at=_seen(4),
        )
    with engine.connect() as conn:
        row = conn.execute(sa.select(schema.firmware_artifacts)).one()
    assert row.download_url == "https://example.com/fw/new.zip"
    assert row.url_last_resolved_at == _text(4)
