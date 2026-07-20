"""进程异常退出后的采集与下载遗留状态恢复测试。"""

from datetime import UTC, datetime

from firmatlas.app.recovery import recover_stale_operations
from firmatlas.domain.model import (
    CrawlRunStatus,
    DownloadPatch,
    DownloadStatus,
    VerificationStatus,
)


def _at(hour: int) -> datetime:
    return datetime(2026, 7, 20, hour, 0, 0, tzinfo=UTC)


def _seed_artifact_ids(
    uow_factory,
    seeded_source,
    seeded_run,
    make_product_candidate,
    make_artifact_candidate,
) -> tuple[str, str]:
    product = make_product_candidate()
    revision = product.hardware_revisions[0]
    release = revision.releases[0]
    with uow_factory.begin() as uow:
        saved_product = uow.catalog.upsert_product(
            source_id=seeded_source.id,
            candidate=product,
            run_id=seeded_run.id,
            seen_at=_at(8),
        )
        saved_revision = uow.catalog.upsert_hardware_revision(
            product_id=saved_product.entity_id,
            candidate=revision,
            run_id=seeded_run.id,
            seen_at=_at(8),
        )
        saved_release = uow.catalog.upsert_release(
            hardware_revision_id=saved_revision.entity_id,
            candidate=release,
            run_id=seeded_run.id,
            seen_at=_at(8),
        )
        ids = tuple(
            uow.catalog.upsert_artifact(
                release_id=saved_release.entity_id,
                candidate=make_artifact_candidate(source_key=f"artifact-{index}"),
                run_id=seeded_run.id,
                seen_at=_at(8),
            ).entity_id
            for index in (1, 2)
        )
    return ids


def test_recover_stale_operations_marks_records_and_is_idempotent(
    uow_factory,
    seeded_source,
    seeded_run,
    make_product_candidate,
    make_artifact_candidate,
):
    artifact_ids = _seed_artifact_ids(
        uow_factory,
        seeded_source,
        seeded_run,
        make_product_candidate,
        make_artifact_candidate,
    )
    with uow_factory.begin() as uow:
        queued = uow.downloads.create_download(artifact_id=artifact_ids[0], requested_at=_at(9))
        downloading = uow.downloads.create_download(
            artifact_id=artifact_ids[1], requested_at=_at(9)
        )
        uow.downloads.transition(
            download_id=downloading.id,
            patch=DownloadPatch(
                status=DownloadStatus.DOWNLOADING,
                started_at=_at(9),
                temporary_relative_path="tmp/downloads/interrupted.part",
                bytes_received=4096,
            ),
        )

    report = recover_stale_operations(uow_factory=uow_factory, recovered_at=_at(10))

    assert report.crawl_runs_recovered == 1
    assert report.downloads_recovered == 2
    with uow_factory.begin() as uow:
        run = next(run for run in uow.runs.list_runs() if run.id == seeded_run.id)
        downloads = {record.id: record for record in uow.downloads.list_downloads()}

    assert run.status is CrawlRunStatus.FAILED
    assert run.is_complete is False
    assert run.finished_at == _at(10)
    assert run.error_count == 1
    assert run.error_summary is not None and "异常终止" in run.error_summary
    assert run.issues[-1].code == "process_interrupted"

    for record_id in (queued.id, downloading.id):
        record = downloads[record_id]
        assert record.status is DownloadStatus.INTERRUPTED
        assert record.verification_status is VerificationStatus.NOT_CHECKED
        assert record.finished_at == _at(10)
        assert record.error_code == "interrupted"
        assert record.error_message is not None and "异常终止" in record.error_message
    assert downloads[downloading.id].temporary_relative_path == "tmp/downloads/interrupted.part"
    assert downloads[downloading.id].bytes_received == 4096
    assert downloads[downloading.id].final_relative_path is None

    second = recover_stale_operations(uow_factory=uow_factory, recovered_at=_at(11))
    assert second.crawl_runs_recovered == 0
    assert second.downloads_recovered == 0
