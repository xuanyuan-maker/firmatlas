"""DownloadRepository 的测试：状态机与单活动任务约束（AC-30）。"""

from datetime import UTC, datetime

import pytest

from firmatlas.domain.errors import (
    ActiveDownloadExistsError,
    InvalidTransitionError,
    RepositoryError,
)
from firmatlas.domain.model import DownloadPatch, DownloadStatus, VerificationStatus


def _at(hour: int) -> datetime:
    return datetime(2026, 7, 16, hour, 0, 0, tzinfo=UTC)


@pytest.fixture
def artifact_ids(
    uow_factory, seeded_source, seeded_run, make_product_candidate, make_artifact_candidate
):
    """入库一棵产品树并挂两个 Artifact，返回两个 Artifact 的 ID。"""
    product = make_product_candidate()
    revision = product.hardware_revisions[0]
    release = revision.releases[0]
    with uow_factory.begin() as uow:
        p = uow.catalog.upsert_product(
            source_id=seeded_source.id, candidate=product, run_id=seeded_run.id, seen_at=_at(0)
        )
        r = uow.catalog.upsert_hardware_revision(
            product_id=p.entity_id, candidate=revision, run_id=seeded_run.id, seen_at=_at(0)
        )
        rel = uow.catalog.upsert_release(
            hardware_revision_id=r.entity_id,
            candidate=release,
            run_id=seeded_run.id,
            seen_at=_at(0),
        )
        ids = []
        for key in ("artifact-1", "artifact-2"):
            art = uow.catalog.upsert_artifact(
                release_id=rel.entity_id,
                candidate=make_artifact_candidate(source_key=key),
                run_id=seeded_run.id,
                seen_at=_at(0),
            )
            ids.append(art.entity_id)
    return tuple(ids)


def test_create_download_starts_queued(uow_factory, artifact_ids):
    with uow_factory.begin() as uow:
        record = uow.downloads.create_download(artifact_id=artifact_ids[0], requested_at=_at(9))
    assert record.status is DownloadStatus.QUEUED
    assert record.verification_status is VerificationStatus.NOT_CHECKED
    assert record.requested_at == _at(9)
    assert record.bytes_received == 0
    assert record.url_refresh_count == 0


def test_second_active_download_for_same_artifact_rejected(uow_factory, artifact_ids):
    with uow_factory.begin() as uow:
        first = uow.downloads.create_download(artifact_id=artifact_ids[0], requested_at=_at(9))
        # 另一个 Artifact 不受影响
        uow.downloads.create_download(artifact_id=artifact_ids[1], requested_at=_at(9))
        with pytest.raises(ActiveDownloadExistsError):
            uow.downloads.create_download(artifact_id=artifact_ids[0], requested_at=_at(10))
        # 第一条失败后即可再次发起
        uow.downloads.transition(
            download_id=first.id,
            patch=DownloadPatch(
                status=DownloadStatus.FAILED, error_code="TIMEOUT", error_message="连接超时"
            ),
        )
        retry = uow.downloads.create_download(artifact_id=artifact_ids[0], requested_at=_at(11))
    assert retry.id != first.id


def test_full_lifecycle_queued_to_completed(uow_factory, artifact_ids):
    with uow_factory.begin() as uow:
        record = uow.downloads.create_download(artifact_id=artifact_ids[0], requested_at=_at(9))
        downloading = uow.downloads.transition(
            download_id=record.id,
            patch=DownloadPatch(
                status=DownloadStatus.DOWNLOADING,
                started_at=_at(10),
                resolved_url="https://example.com/fw/TL-WR841N_v14.zip",
                attempt_count=1,
                temporary_relative_path="tmp/downloads/abc.part",
            ),
        )
        # downloading → downloading：过程中的进度更新
        progressed = uow.downloads.transition(
            download_id=record.id,
            patch=DownloadPatch(status=DownloadStatus.DOWNLOADING, bytes_received=1_048_576),
        )
        completed = uow.downloads.transition(
            download_id=record.id,
            patch=DownloadPatch(
                status=DownloadStatus.COMPLETED,
                verification_status=VerificationStatus.VERIFIED,
                finished_at=_at(11),
                bytes_received=4_194_304,
                size_bytes=4_194_304,
                sha256="a" * 64,
                final_relative_path="firmware/tp-link/cn/tl-wr841n/v14/20260501/abc__fw.bin",
            ),
        )
    assert downloading.status is DownloadStatus.DOWNLOADING
    assert downloading.started_at == _at(10)
    assert progressed.bytes_received == 1_048_576
    assert progressed.started_at == _at(10)  # 未在 patch 中的字段保持原值
    assert completed.status is DownloadStatus.COMPLETED
    assert completed.verification_status is VerificationStatus.VERIFIED
    assert completed.finished_at == _at(11)
    assert completed.sha256 == "a" * 64


def test_invalid_transitions_raise(uow_factory, artifact_ids):
    with uow_factory.begin() as uow:
        record = uow.downloads.create_download(artifact_id=artifact_ids[0], requested_at=_at(9))
        # queued 不能直接 completed（必须经过 downloading）
        with pytest.raises(InvalidTransitionError, match="queued"):
            uow.downloads.transition(
                download_id=record.id, patch=DownloadPatch(status=DownloadStatus.COMPLETED)
            )
        uow.downloads.transition(
            download_id=record.id, patch=DownloadPatch(status=DownloadStatus.CANCELLED)
        )
        # 终态之后不允许任何变迁
        with pytest.raises(InvalidTransitionError, match="cancelled"):
            uow.downloads.transition(
                download_id=record.id, patch=DownloadPatch(status=DownloadStatus.DOWNLOADING)
            )


def test_transition_unknown_id_raises(uow_factory):
    with pytest.raises(RepositoryError, match="不存在"):
        with uow_factory.begin() as uow:
            uow.downloads.transition(
                download_id="no-such-download",
                patch=DownloadPatch(status=DownloadStatus.DOWNLOADING),
            )


def test_list_downloads_filters_and_orders(uow_factory, artifact_ids):
    first_artifact, second_artifact = artifact_ids
    with uow_factory.begin() as uow:
        early = uow.downloads.create_download(artifact_id=first_artifact, requested_at=_at(8))
        uow.downloads.transition(
            download_id=early.id,
            patch=DownloadPatch(status=DownloadStatus.FAILED, error_code="TIMEOUT"),
        )
        late = uow.downloads.create_download(artifact_id=second_artifact, requested_at=_at(10))

        all_records = uow.downloads.list_downloads()
        failed_only = uow.downloads.list_downloads(status=DownloadStatus.FAILED)
        by_artifact = uow.downloads.list_downloads(artifact_id=second_artifact)
        stale = uow.downloads.find_stale_active()

    assert [r.id for r in all_records] == [late.id, early.id]  # 发起时间倒序
    assert [r.id for r in failed_only] == [early.id]
    assert [r.id for r in by_artifact] == [late.id]
    assert [r.id for r in stale] == [late.id]  # queued 遗留会占住单活动任务约束，必须能找出来
