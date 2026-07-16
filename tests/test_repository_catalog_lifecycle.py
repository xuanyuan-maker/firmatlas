"""目录生命周期操作的测试：消失对账、地址刷新落库、ArtifactContext。"""

from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa

from firmatlas.domain.errors import RepositoryError
from firmatlas.domain.model import ArtifactContext, VisibilityStatus
from firmatlas.infra import schema


def _seen(day: int) -> datetime:
    return datetime(2026, 7, day, 12, 0, 0, tzinfo=UTC)


def _text(day: int) -> str:
    return f"2026-07-{day:02d}T12:00:00Z"


def _upsert_tree(uow, *, source_id, run_id, seen_at, product):
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


@pytest.fixture
def make_tree(make_product_candidate, make_revision_candidate):
    """按后缀构造一棵各级 source_key 互不相同的产品子树。"""

    def _make(suffix: str):
        return make_product_candidate(
            source_key=f"product-{suffix}",
            hardware_revisions=(make_revision_candidate(source_key=f"rev-{suffix}"),),
        )

    return _make


def test_mark_unseen_marks_only_unseen_of_that_source(
    engine, uow_factory, seeded_source, seeded_run, make_source, make_tree
):
    other_source = make_source(source_key="tp-link-us", region_code="US")
    tree_a, tree_b = make_tree("a"), make_tree("b")
    with uow_factory.begin() as uow:
        uow.sources.ensure_seed_sources([other_source])
        other_run = uow.runs.create_run(source_id=other_source.id, started_at=_seen(1))
        # 第一轮：CN 来源两棵树，US 来源一棵树
        _upsert_tree(
            uow, source_id=seeded_source.id, run_id=seeded_run.id, seen_at=_seen(1), product=tree_a
        )
        _upsert_tree(
            uow, source_id=seeded_source.id, run_id=seeded_run.id, seen_at=_seen(1), product=tree_b
        )
        _upsert_tree(
            uow,
            source_id=other_source.id,
            run_id=other_run.id,
            seen_at=_seen(1),
            product=make_tree("us"),
        )
        # 第二轮：CN 只重新见到 tree_a
        run2 = uow.runs.create_run(source_id=seeded_source.id, started_at=_seen(2))
        _upsert_tree(
            uow, source_id=seeded_source.id, run_id=run2.id, seen_at=_seen(2), product=tree_a
        )
        summary = uow.catalog.mark_unseen_as_disappeared(
            source_id=seeded_source.id, run_id=run2.id, confirmed_at=_seen(2)
        )

    assert summary.releases_disappeared == 1
    assert summary.artifacts_disappeared == 1
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                schema.products.c.source_key,
                schema.firmware_releases.c.visibility_status,
                schema.firmware_releases.c.disappeared_at,
            ).select_from(
                schema.firmware_releases.join(
                    schema.hardware_revisions,
                    schema.firmware_releases.c.hardware_revision_id
                    == schema.hardware_revisions.c.id,
                ).join(
                    schema.products,
                    schema.hardware_revisions.c.product_id == schema.products.c.id,
                )
            )
        ).all()
    by_product = {row.source_key: row for row in rows}
    assert by_product["product-a"].visibility_status == "active"  # 重新见到的不受影响
    assert by_product["product-b"].visibility_status == "disappeared"
    assert by_product["product-b"].disappeared_at == _text(2)
    assert by_product["product-us"].visibility_status == "active"  # 其他来源不受影响


def test_mark_unseen_can_mark_artifact_alone(uow_factory, seeded_source, seeded_run, make_tree):
    """发布仍在但其中某个 Artifact 未再出现时，只有该 Artifact 被标记。"""
    tree = make_tree("a")
    with uow_factory.begin() as uow:
        p, r, _rel, _art = _upsert_tree(
            uow, source_id=seeded_source.id, run_id=seeded_run.id, seen_at=_seen(1), product=tree
        )
        run2 = uow.runs.create_run(source_id=seeded_source.id, started_at=_seen(2))
        # 第二轮重新见到产品/硬件版本/发布，但没有见到 Artifact
        uow.catalog.upsert_product(
            source_id=seeded_source.id, candidate=tree, run_id=run2.id, seen_at=_seen(2)
        )
        uow.catalog.upsert_hardware_revision(
            product_id=p.entity_id,
            candidate=tree.hardware_revisions[0],
            run_id=run2.id,
            seen_at=_seen(2),
        )
        uow.catalog.upsert_release(
            hardware_revision_id=r.entity_id,
            candidate=tree.hardware_revisions[0].releases[0],
            run_id=run2.id,
            seen_at=_seen(2),
        )
        summary = uow.catalog.mark_unseen_as_disappeared(
            source_id=seeded_source.id, run_id=run2.id, confirmed_at=_seen(2)
        )
        # 再对账一次：已 disappeared 的不会被重复统计
        second = uow.catalog.mark_unseen_as_disappeared(
            source_id=seeded_source.id, run_id=run2.id, confirmed_at=_seen(3)
        )
    assert summary.releases_disappeared == 0
    assert summary.artifacts_disappeared == 1
    assert second.releases_disappeared == 0
    assert second.artifacts_disappeared == 0


def test_update_artifact_url_keeps_identity(
    engine, uow_factory, seeded_source, seeded_run, make_tree
):
    with uow_factory.begin() as uow:
        *_, art = _upsert_tree(
            uow,
            source_id=seeded_source.id,
            run_id=seeded_run.id,
            seen_at=_seen(1),
            product=make_tree("a"),
        )
        uow.catalog.update_artifact_url(
            artifact_id=art.entity_id,
            download_url="https://example.com/fw/refreshed.zip",
            url_expires_at=_seen(9),
            resolved_at=_seen(5),
        )
    with engine.connect() as conn:
        row = conn.execute(sa.select(schema.firmware_artifacts)).one()
    assert row.download_url == "https://example.com/fw/refreshed.zip"
    assert row.url_expires_at == _text(9)
    assert row.url_last_resolved_at == _text(5)
    assert row.source_key == "artifact-1"  # 身份不因刷新而改变（AC-29）


def test_update_artifact_url_unknown_id_raises(uow_factory):
    with pytest.raises(RepositoryError, match="不存在"):
        with uow_factory.begin() as uow:
            uow.catalog.update_artifact_url(
                artifact_id="no-such-artifact",
                download_url="https://example.com/x.zip",
                url_expires_at=None,
                resolved_at=_seen(1),
            )


def test_get_artifact_context_returns_whole_chain(
    uow_factory, seeded_source, seeded_run, make_tree
):
    with uow_factory.begin() as uow:
        p, r, rel, art = _upsert_tree(
            uow,
            source_id=seeded_source.id,
            run_id=seeded_run.id,
            seen_at=_seen(1),
            product=make_tree("a"),
        )
        ctx = uow.catalog.get_artifact_context(art.entity_id)
        missing = uow.catalog.get_artifact_context("no-such-artifact")

    assert missing is None
    assert isinstance(ctx, ArtifactContext)
    assert ctx.source.id == seeded_source.id
    assert ctx.product.id == p.entity_id
    assert ctx.hardware_revision.id == r.entity_id
    assert ctx.release.id == rel.entity_id
    assert ctx.artifact.id == art.entity_id
    # 链上对象已是领域类型：时间带时区、日期为 date、状态为枚举
    assert ctx.artifact.url_last_resolved_at.tzinfo is not None
    assert ctx.release.release_date == date(2026, 5, 1)
    assert ctx.release.visibility_status is VisibilityStatus.ACTIVE
    assert ctx.hardware_revision.revision_explicit is True
