"""FirmAtlas CLI 入口。

MVP 命令结构见需求分析 0x0F。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import asdict
from pathlib import Path

import click
import sqlalchemy as sa

from firmatlas import __version__
from firmatlas.app import registry
from firmatlas.app.config import AppConfig, load_config
from firmatlas.app.crawl import CrawlReport, crawl_source
from firmatlas.app.download import DownloadReport, download_artifact
from firmatlas.app.queries import OUTPUT_SCHEMA_VERSION, CatalogFilter
from firmatlas.app.recovery import recover_stale_operations
from firmatlas.domain.errors import FirmAtlasError
from firmatlas.domain.model import (
    DownloadStatus,
    ProductFamily,
    ProductType,
    VerificationStatus,
    VisibilityStatus,
)
from firmatlas.infra import database
from firmatlas.infra.artifact_store import ArtifactStore
from firmatlas.infra.downloader import Downloader
from firmatlas.infra.http_client import HttpFetcher, make_http_client
from firmatlas.infra.process_lock import DataDirectoryLock
from firmatlas.infra.query_service import SqliteCatalogQueryService
from firmatlas.infra.repository import SqliteUnitOfWorkFactory


@click.group(name="firmatlas")
@click.version_option(version=__version__, prog_name="firmatlas")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="TOML 配置文件路径。",
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="数据根目录；默认 data，可覆盖配置文件。",
)
@click.option("--verbose", is_flag=True, default=None, help="输出详细日志到标准错误。")
@click.option("--no-color", is_flag=True, default=None, help="禁用彩色输出。")
@click.pass_context
def cli(
    ctx: click.Context,
    config_path: Path | None,
    data_dir: Path | None,
    verbose: bool | None,
    no_color: bool | None,
) -> None:
    """FirmAtlas：IoT 固件目录采集与按需下载。"""
    ctx.ensure_object(dict)
    try:
        config = load_config(
            config_path=config_path,
            data_dir=data_dir,
            verbose=verbose,
            no_color=no_color,
        )
        lock = DataDirectoryLock(config.data_dir)
        lock.acquire()
    except FirmAtlasError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.call_on_close(lock.release)
    ctx.obj["config"] = config
    ctx.obj["data_dir"] = config.data_dir
    ctx.obj["verbose"] = config.verbose
    ctx.obj["no_color"] = config.no_color


def _open_database_with_recovery(data_dir: Path) -> sa.Engine:
    """打开数据库，并在当前进程独占数据目录时恢复遗留任务。"""
    engine = database.open_database(data_dir)
    try:
        report = recover_stale_operations(
            uow_factory=SqliteUnitOfWorkFactory(engine)
        )
    except BaseException:
        engine.dispose()
        raise
    if report.crawl_runs_recovered or report.downloads_recovered:
        click.echo(
            "已恢复上次异常中断的任务："
            f"采集 {report.crawl_runs_recovered}，下载 {report.downloads_recovered}。",
            err=True,
        )
    return engine


@cli.command(name="config")
@click.pass_context
def config_command(ctx: click.Context) -> None:
    """显示合并默认值、TOML 和 CLI 参数后的有效配置。"""
    config: AppConfig = ctx.obj["config"]
    config_source = config.config_path if config.config_path else "未指定（使用内置默认值）"
    click.echo(f"配置文件：{config_source}")
    click.echo(f"数据目录：{config.data_dir}")
    click.echo(f"详细日志：{'开启' if config.verbose else '关闭'}")
    click.echo(f"颜色输出：{'关闭' if config.no_color else '开启'}")
    click.echo(f"HTTP 请求超时：{config.http.request_timeout:g}s")
    click.echo(f"HTTP 连接超时：{config.http.connect_timeout:g}s")
    click.echo(f"HTTP 最大重试次数：{config.http.max_retries}")
    click.echo(f"HTTP 退避基数：{config.http.retry_backoff_base:g}s")
    click.echo(f"下载读取超时：{config.download.read_timeout:g}s")
    click.echo(f"下载连接超时：{config.download.connect_timeout:g}s")


@cli.command(name="init")
@click.pass_context
def init_command(ctx: click.Context) -> None:
    """初始化数据目录与数据库（可重复执行）。"""
    data_dir = Path(ctx.obj["data_dir"])
    try:
        result = database.initialize(data_dir)
        # 幂等写入内置来源（已存在的 source_key 跳过）
        engine = _open_database_with_recovery(data_dir)
        try:
            with SqliteUnitOfWorkFactory(engine).begin() as uow:
                uow.sources.ensure_seed_sources(registry.seed_sources())
        finally:
            engine.dispose()
    except FirmAtlasError as exc:
        raise click.ClickException(str(exc)) from exc
    if result.created:
        click.echo(f"已初始化数据库：{result.db_path}（结构版本 {result.schema_version}）")
    else:
        click.echo(
            f"数据库已初始化，未做改动：{result.db_path}（结构版本 {result.schema_version}）"
        )


@cli.command(name="crawl")
@click.argument("source_key")
@click.pass_context
def crawl_command(ctx: click.Context, source_key: str) -> None:
    """采集指定来源的固件元数据（如 tp-link-cn）。"""
    data_dir = Path(ctx.obj["data_dir"])
    config: AppConfig = ctx.obj["config"]
    try:
        registry.check_supported(source_key)
        _ensure_auth_before_crawl(source_key, data_dir)
        engine = _open_database_with_recovery(data_dir)
    except FirmAtlasError as exc:
        raise click.ClickException(str(exc)) from exc

    async def _run() -> CrawlReport:
        async with make_http_client(
            request_timeout=config.http.request_timeout,
            connect_timeout=config.http.connect_timeout,
            legacy_tls=registry.requires_legacy_tls(source_key),
        ) as client:
            http = HttpFetcher(
                client,
                max_retries=config.http.max_retries,
                retry_backoff_base=config.http.retry_backoff_base,
                request_interval=registry.crawl_request_interval(source_key),
            )
            adapter = registry.build_adapter(source_key, http, data_dir)
            return await crawl_source(
                adapter=adapter, uow_factory=SqliteUnitOfWorkFactory(engine)
            )

    try:
        report = asyncio.run(_run())
    except FirmAtlasError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        engine.dispose()

    _echo_report(report)
    if report.status.value == "failed":
        raise SystemExit(1)


def _echo_report(report: CrawlReport) -> None:
    s = report.stats
    click.echo(f"采集完成：{report.source_key}（run {report.run_id}）")
    click.echo(f"  状态：{report.status.value}（complete={str(report.is_complete).lower()}）")
    click.echo(
        f"  产品 {s.products_seen} / 发布 {s.releases_seen} / Artifact {s.artifacts_seen}"
        f"（新增 {s.items_added}、更新 {s.items_updated}、消失 {s.items_disappeared}）"
    )
    click.echo(f"  跳过 {s.items_skipped}、错误 {s.error_count}")
    if report.error_summary:
        click.echo(f"  错误摘要：{report.error_summary}")
    if report.issues:
        click.echo(f"  问题 {len(report.issues)} 条（详情见 firmatlas runs）")


@cli.command(name="sources")
@click.pass_context
def sources_command(ctx: click.Context) -> None:
    """列出已注册的固件来源。"""
    data_dir = Path(ctx.obj["data_dir"])
    try:
        engine = _open_database_with_recovery(data_dir)
    except FirmAtlasError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        with SqliteUnitOfWorkFactory(engine).begin() as uow:
            sources = uow.sources.list_sources()
    finally:
        engine.dispose()

    if not sources:
        click.echo("尚无注册来源，请先运行 firmatlas init。")
        return
    for src in sources:
        enabled = "启用" if src.enabled else "停用"
        click.echo(
            f"{src.source_key}  {src.vendor_name}  {src.region_code}"
            f"  [{src.discovery_method.value}]  {enabled}  {src.name}"
        )


@cli.command(name="runs")
@click.option("--source", "source_key", default=None, help="只看指定来源（source_key）。")
@click.option("--limit", default=20, show_default=True, help="最多显示条数。")
@click.pass_context
def runs_command(ctx: click.Context, source_key: str | None, limit: int) -> None:
    """列出采集运行历史（最新在前）。"""
    data_dir = Path(ctx.obj["data_dir"])
    try:
        engine = _open_database_with_recovery(data_dir)
    except FirmAtlasError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        with SqliteUnitOfWorkFactory(engine).begin() as uow:
            source_id = None
            if source_key is not None:
                source = uow.sources.get_by_source_key(source_key)
                if source is None:
                    raise click.ClickException(f"来源 {source_key!r} 未注册。")
                source_id = source.id
            runs = uow.runs.list_runs(source_id=source_id, limit=limit)
            id_to_key = {s.id: s.source_key for s in uow.sources.list_sources()}
    finally:
        engine.dispose()

    if not runs:
        click.echo("暂无采集记录。")
        return
    for run in runs:
        finished = run.finished_at.isoformat() if run.finished_at else "-"
        click.echo(
            f"{run.id}  {id_to_key.get(run.source_id, run.source_id)}"
            f"  {run.status.value:<9}  开始 {run.started_at.isoformat()}  结束 {finished}"
        )
        click.echo(
            f"    产品 {run.products_seen} / 发布 {run.releases_seen}"
            f" / Artifact {run.artifacts_seen}，新增 {run.items_added}、更新 {run.items_updated}"
            f"、消失 {run.items_disappeared}、跳过 {run.items_skipped}、错误 {run.error_count}"
        )
        if run.error_summary:
            click.echo(f"    错误摘要：{run.error_summary}")
        for issue in run.issues:
            click.echo(f"    [{issue.code}] {issue.detail}")


@cli.command(name="list")
@click.option("--vendor", default=None, help="厂商 key（如 tp-link）。")
@click.option("--source", default=None, help="来源 source_key（如 tp-link-cn）。")
@click.option("--region", default=None, help="地区代码（如 CN）。")
@click.option(
    "--family", default=None, type=click.Choice([e.value for e in ProductFamily]),
    help="产品族。",
)
@click.option(
    "--type", "product_type", default=None, type=click.Choice([e.value for e in ProductType]),
    help="产品类型。",
)
@click.option("--series", default=None, help="系列（包含匹配）。")
@click.option("--model", default=None, help="型号（包含匹配，不区分大小写）。")
@click.option("--hardware", default=None, help="硬件版本（包含匹配）。")
@click.option("--version", "fw_version", default=None, help="固件版本（包含匹配）。")
@click.option(
    "--visibility", default=None, type=click.Choice([e.value for e in VisibilityStatus]),
    help="可见性状态。",
)
@click.option(
    "--download-status", default=None, type=click.Choice([e.value for e in DownloadStatus]),
    help="最近一次下载状态。",
)
@click.option(
    "--verification-status", default=None,
    type=click.Choice([e.value for e in VerificationStatus]),
    help="最近一次校验状态。",
)
@click.option("--limit", default=50, show_default=True, help="最多显示条数。")
@click.option("--offset", default=0, show_default=True, help="跳过前 N 条。")
@click.option(
    "--format", "output_format", default="table", show_default=True,
    type=click.Choice(["table", "json"]), help="输出格式。",
)
@click.pass_context
def list_command(
    ctx: click.Context,
    vendor: str | None,
    source: str | None,
    region: str | None,
    family: str | None,
    product_type: str | None,
    series: str | None,
    model: str | None,
    hardware: str | None,
    fw_version: str | None,
    visibility: str | None,
    download_status: str | None,
    verification_status: str | None,
    limit: int,
    offset: int,
    output_format: str,
) -> None:
    """浏览和筛选固件目录（AC-21 ~ AC-23）。"""
    catalog_filter = CatalogFilter(
        vendor=vendor,
        source=source,
        region=region,
        family=ProductFamily(family) if family else None,
        type=ProductType(product_type) if product_type else None,
        series=series,
        model=model,
        hardware=hardware,
        version=fw_version,
        visibility=VisibilityStatus(visibility) if visibility else None,
        download_status=DownloadStatus(download_status) if download_status else None,
        verification_status=(
            VerificationStatus(verification_status) if verification_status else None
        ),
        limit=limit,
        offset=offset,
    )

    data_dir = Path(ctx.obj["data_dir"])
    try:
        engine = _open_database_with_recovery(data_dir)
    except FirmAtlasError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        page = SqliteCatalogQueryService(engine).list_firmware(catalog_filter)
    finally:
        engine.dispose()

    if output_format == "json":
        payload = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "total": page.total,
            "count": len(page.rows),
            "offset": offset,
            "rows": [_jsonable(asdict(row)) for row in page.rows],
        }
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not page.rows:
        click.echo("没有符合条件的固件记录。")
        return
    headers = ("RELEASE-ID", "来源", "型号", "类型", "硬件", "固件版本", "日期", "状态", "件数")
    table = [
        (
            row.release_id[:8],
            row.source_key,
            row.model,
            row.product_type.value,
            row.hardware,
            row.version_normalized or row.version,
            row.release_date.isoformat() if row.release_date else "-",
            row.visibility.value,
            str(row.artifact_count),
        )
        for row in page.rows
    ]
    _echo_table(headers, table)
    click.echo(f"共 {page.total} 条，当前显示 {offset + 1}-{offset + len(page.rows)} 条。")
    click.echo("提示：firmatlas show <release-id> 查看详情，firmatlas download <release-id> 下载。")


@cli.command(name="show")
@click.argument("release_id")
@click.option(
    "--format", "output_format", default="table", show_default=True,
    type=click.Choice(["table", "json"]), help="输出格式。",
)
@click.pass_context
def show_command(ctx: click.Context, release_id: str, output_format: str) -> None:
    """查看固件发布详情及其 Artifact 列表。"""
    data_dir = Path(ctx.obj["data_dir"])
    try:
        engine = _open_database_with_recovery(data_dir)
    except FirmAtlasError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        service = SqliteCatalogQueryService(engine)
        detail = service.show_release(release_id)
        if detail is None:
            matches = service.find_release_ids_by_prefix(release_id)
            if len(matches) == 1:
                detail = service.show_release(matches[0])
            elif len(matches) > 1:
                raise click.ClickException(
                    f"ID 前缀 {release_id!r} 匹配到 {len(matches)} 条记录，请提供更长的前缀。"
                )
    finally:
        engine.dispose()

    if detail is None:
        raise click.ClickException(f"未找到发布 {release_id!r}。")

    if output_format == "json":
        payload = {"schema_version": OUTPUT_SCHEMA_VERSION, "release": _jsonable(asdict(detail))}
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    click.echo(f"发布 {detail.release_id}")
    click.echo(f"  来源：{detail.source_key}（{detail.vendor_key} / {detail.region_code}）")
    click.echo(f"  产品：{detail.display_name}（{detail.model}，{detail.product_type.value}）")
    click.echo(f"  硬件版本：{detail.hardware_raw or detail.hardware}")
    click.echo(f"  固件版本：{detail.version_normalized or detail.version}")
    click.echo(f"  原始标题：{detail.title or '-'}")
    date_text = detail.release_date.isoformat() if detail.release_date else "-"
    click.echo(f"  发布日期：{date_text}")
    click.echo(f"  可见性：{detail.visibility.value}")
    if detail.disappeared_at is not None:
        click.echo(f"  消失时间：{detail.disappeared_at.isoformat()}")
    click.echo(f"  首次发现：{detail.first_seen_at.isoformat()}")
    click.echo(f"  最近发现：{detail.last_seen_at.isoformat()}")
    click.echo(f"  来源页面：{detail.source_url}")
    if detail.release_notes:
        click.echo(f"  发布说明：{detail.release_notes}")
    click.echo(f"  Artifact（{len(detail.artifacts)} 个）：")
    for a in detail.artifacts:
        size = f"{a.advertised_size} B" if a.advertised_size is not None else "大小未知"
        download = a.last_download_status.value if a.last_download_status else "未下载"
        click.echo(f"    {a.artifact_id}  [{a.artifact_type.value}]  {size}  {download}")
        click.echo(f"      {a.download_url}")


@cli.command(name="download")
@click.argument("artifact_ids", nargs=-1, required=True)
@click.pass_context
def download_command(ctx: click.Context, artifact_ids: tuple[str, ...]) -> None:
    """下载固件并校验归档（接受 list 的发布 ID 或 show 的 Artifact ID，可用前缀）。"""
    data_dir = Path(ctx.obj["data_dir"])
    config: AppConfig = ctx.obj["config"]
    try:
        engine = _open_database_with_recovery(data_dir)
    except FirmAtlasError as exc:
        raise click.ClickException(str(exc)) from exc

    uow_factory = SqliteUnitOfWorkFactory(engine)
    store = ArtifactStore(data_dir)
    failures = 0

    async def _run_all() -> None:
        nonlocal failures
        async with AsyncExitStack() as stack:
            clients = {}
            downloaders = {}
            for raw_id in artifact_ids:
                try:
                    artifact_id, source_key = _resolve_artifact(engine, uow_factory, raw_id)
                except FirmAtlasError as exc:
                    failures += 1
                    click.echo(f"{raw_id}: {exc}", err=True)
                    continue
                legacy_tls = registry.requires_legacy_tls(source_key)
                if legacy_tls not in clients:
                    clients[legacy_tls] = await stack.enter_async_context(
                        make_http_client(
                            request_timeout=config.http.request_timeout,
                            connect_timeout=config.http.connect_timeout,
                            legacy_tls=legacy_tls,
                        )
                    )
                    downloaders[legacy_tls] = Downloader(
                        clients[legacy_tls],
                        read_timeout=config.download.read_timeout,
                        connect_timeout=config.download.connect_timeout,
                    )
                client = clients[legacy_tls]
                downloader = downloaders[legacy_tls]
                adapter = _build_refreshing_adapter(source_key, client, config, data_dir)
                try:
                    report = await download_artifact(
                        artifact_id=artifact_id,
                        uow_factory=uow_factory,
                        downloader=downloader,
                        store=store,
                        data_dir=data_dir,
                        adapter=adapter,
                    )
                except FirmAtlasError as exc:
                    failures += 1
                    click.echo(f"{artifact_id}: {exc}", err=True)
                    continue
                _echo_download_report(report)
                if report.status is not DownloadStatus.COMPLETED:
                    failures += 1

    try:
        asyncio.run(_run_all())
    finally:
        engine.dispose()

    if failures:
        raise SystemExit(1)


def _resolve_artifact(engine, uow_factory, raw_id: str) -> tuple[str, str]:
    """把用户输入的 ID（Artifact 或发布，均可为前缀）解析为完整 Artifact ID。

    解析顺序：
    1. 按 Artifact ID 前缀查——唯一命中即用；
    2. 未命中则按发布 ID 前缀查（list 输出的是发布 ID）：
       - 发布下只有 1 个 Artifact → 直接下载它；
       - 多个 → 列出供选择，要求用户改用 Artifact ID。
    返回 (artifact_id, source_key)。
    """
    service = SqliteCatalogQueryService(engine)

    matches = service.find_artifact_ids_by_prefix(raw_id)
    if len(matches) > 1:
        raise FirmAtlasError(f"ID 前缀 {raw_id!r} 匹配到 {len(matches)} 条记录，请提供更长的前缀。")
    if len(matches) == 1:
        return matches[0], _source_key_of(uow_factory, matches[0])

    # 未命中 Artifact：尝试按发布 ID 解析（list 输出的短 ID）
    release_matches = service.find_release_ids_by_prefix(raw_id)
    if not release_matches:
        raise FirmAtlasError(f"未找到 Artifact 或发布 {raw_id!r}。")
    if len(release_matches) > 1:
        raise FirmAtlasError(
            f"ID 前缀 {raw_id!r} 匹配到 {len(release_matches)} 个发布，请提供更长的前缀。"
        )
    detail = service.show_release(release_matches[0])
    assert detail is not None  # 前缀查询刚命中，不可能不存在
    if not detail.artifacts:
        raise FirmAtlasError(f"发布 {raw_id!r} 下没有可下载的 Artifact。")
    if len(detail.artifacts) > 1:
        lines = "\n".join(
            f"  {a.artifact_id}  [{a.artifact_type.value}]  {a.original_filename or '-'}"
            for a in detail.artifacts
        )
        raise FirmAtlasError(
            f"发布 {raw_id!r} 下有 {len(detail.artifacts)} 个 Artifact，"
            f"请指定其中一个：\n{lines}"
        )
    artifact_id = detail.artifacts[0].artifact_id
    return artifact_id, _source_key_of(uow_factory, artifact_id)


def _source_key_of(uow_factory, artifact_id: str) -> str:
    with uow_factory.begin() as uow:
        ctx = uow.catalog.get_artifact_context(artifact_id)
    assert ctx is not None
    return ctx.source.source_key


def _build_refreshing_adapter(source_key: str, client, config: AppConfig, data_dir: Path):
    """来源有适配器且实现了地址刷新时返回适配器，否则返回 None（下载仍可进行）。"""
    try:
        http = HttpFetcher(
            client,
            max_retries=config.http.max_retries,
            retry_backoff_base=config.http.retry_backoff_base,
        )
        adapter = registry.build_adapter(source_key, http, data_dir)
    except FirmAtlasError:
        return None
    return adapter if hasattr(adapter, "refresh_artifact_url") else None


def _echo_download_report(report: DownloadReport) -> None:
    if report.status is DownloadStatus.COMPLETED:
        verified = {
            VerificationStatus.VERIFIED: "官方校验和一致",
            VerificationStatus.NOT_AVAILABLE: "无官方校验和",
        }.get(report.verification_status, report.verification_status.value)
        click.echo(f"{report.artifact_id}: 下载完成（{report.bytes_received} B，{verified}）")
        click.echo(f"  SHA-256：{report.sha256}")
        click.echo(f"  归档位置：{report.final_relative_path}")
    else:
        click.echo(
            f"{report.artifact_id}: 下载失败（{report.status.value}"
            f"，错误 {report.error_code}）",
            err=True,
        )
        if report.error_message:
            click.echo(f"  {report.error_message}", err=True)
    if report.url_refreshed:
        click.echo("  （下载地址已自动刷新一次）")


@cli.command(name="downloads")
@click.option(
    "--status", default=None, type=click.Choice([e.value for e in DownloadStatus]),
    help="按状态筛选。",
)
@click.option("--artifact", "artifact_id", default=None, help="只看指定 Artifact 的记录。")
@click.option("--limit", default=20, show_default=True, help="最多显示条数。")
@click.pass_context
def downloads_command(
    ctx: click.Context, status: str | None, artifact_id: str | None, limit: int
) -> None:
    """列出下载历史（最新在前）。"""
    data_dir = Path(ctx.obj["data_dir"])
    try:
        engine = _open_database_with_recovery(data_dir)
    except FirmAtlasError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        with SqliteUnitOfWorkFactory(engine).begin() as uow:
            records = uow.downloads.list_downloads(
                status=DownloadStatus(status) if status else None,
                artifact_id=artifact_id,
                limit=limit,
            )
    finally:
        engine.dispose()

    if not records:
        click.echo("暂无下载记录。")
        return
    headers = ("下载ID", "ARTIFACT", "状态", "校验", "字节数", "发起时间")
    table = [
        (
            r.id[:8],
            r.artifact_id[:8],
            r.status.value,
            r.verification_status.value,
            str(r.bytes_received),
            r.requested_at.isoformat(),
        )
        for r in records
    ]
    _echo_table(headers, table)
    for r in records:
        if r.status is DownloadStatus.COMPLETED and r.final_relative_path:
            click.echo(f"{r.id[:8]}  → {r.final_relative_path}")
        elif r.error_message:
            click.echo(f"{r.id[:8]}  ✗ {r.error_message}")


def _jsonable(value: object) -> object:
    """把 dataclass asdict 结果中的枚举/日期时间递归转为 JSON 可序列化文本。"""
    import datetime as dt
    import enum

    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _echo_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    """按列最大宽度对齐输出表格（中文按 2 列宽估算）。"""

    def width(text: str) -> int:
        return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)

    def pad(text: str, target: int) -> str:
        return text + " " * (target - width(text))

    widths = [
        max(width(headers[i]), *(width(row[i]) for row in rows)) for i in range(len(headers))
    ]
    click.echo("  ".join(pad(h, w) for h, w in zip(headers, widths, strict=True)).rstrip())
    for row in rows:
        click.echo("  ".join(pad(c, w) for c, w in zip(row, widths, strict=True)).rstrip())


# ---------------------------------------------------------------------------
# auth 命令：管理需要登录的来源的认证 token
# ---------------------------------------------------------------------------

_AUTH_SOURCE_KEYS = frozenset({"ruijie-cn"})

_AUTH_INSTRUCTIONS = {
    "ruijie-cn": (
        "锐捷官网 (ruijie.com.cn)",
        "1. 浏览器打开 https://www.ruijie.com.cn/user/login/ 并登录\n"
        "2. F12 打开开发者工具 → Console\n"
        "3. 执行: document.cookie.match(/GW_ACCESS_TOKEN=([^;]+)/)[1]\n"
        "4. 复制输出的 token 值",
    ),
}


def _ensure_auth_before_crawl(source_key: str, data_dir: Path) -> None:
    """crawl 前确保来源已认证；token 缺失时交互式引导用户输入。"""
    if source_key not in _AUTH_SOURCE_KEYS:
        return

    if source_key == "ruijie-cn":
        from firmatlas.adapters.ruijie_cn.auth import (
            TokenNotConfiguredError,
            load_token,
            prompt_interactive_token,
            save_token,
        )

        try:
            load_token(data_dir)
        except TokenNotConfiguredError:
            token = prompt_interactive_token()
            path = save_token(token, data_dir)
            click.echo(f"✅ Token 已保存到 {path}")
            click.echo()


@cli.command(name="auth")
@click.argument("source_key")
@click.option("--check", "check_only", is_flag=True, default=False, help="检查 token 是否有效。")
@click.option("--save", "save_value", default=None, help="保存 token 值。")
@click.pass_context
def auth_command(
    ctx: click.Context, source_key: str, check_only: bool, save_value: str | None
) -> None:
    """管理需要登录的来源的认证 token（如 ruijie-cn）。"""
    data_dir = Path(ctx.obj["data_dir"])

    if source_key not in _AUTH_SOURCE_KEYS:
        raise click.ClickException(
            f"来源 {source_key!r} 不需要 token 认证。"
            f"需要认证的来源：{', '.join(sorted(_AUTH_SOURCE_KEYS))}"
        )

    from firmatlas.adapters.ruijie_cn.auth import (
        TokenNotConfiguredError,
        load_token,
        save_token,
    )

    # --save 模式
    if save_value is not None:
        path = save_token(save_value, data_dir)
        click.echo(f"✅ Token 已保存到 {path}")
        return

    # --check 模式
    if check_only:
        try:
            token_info = load_token(data_dir)
        except TokenNotConfiguredError as exc:
            raise click.ClickException(str(exc)) from exc

        click.echo("正在检查 token 有效性...")
        valid, detail = asyncio.run(_check_ruijie_token(token_info.token))
        if valid:
            click.echo(f"✅ Token 有效（{detail}）")
        else:
            click.echo(f"❌ Token 无效或已过期（{detail}）", err=True)
            raise SystemExit(1)
        return

    # 默认：显示指引
    name, instructions = _AUTH_INSTRUCTIONS[source_key]
    click.echo(f"━━━ {name} Token 获取指引 ━━━")
    click.echo()
    click.echo(instructions)
    click.echo()
    click.echo("获取到 token 后，通过以下方式之一保存：")
    click.echo("  1. 环境变量: export RUIJIE_TOKEN=\"你的token\"")
    click.echo(f"  2. 文件保存: firmatlas auth {source_key} --save \"你的token\"")
    click.echo(f"  3. 环境变量（临时）: RUIJIE_TOKEN=\"xxx\" firmatlas crawl {source_key}")
    click.echo()
    click.echo(f"检查 token 是否有效: firmatlas auth {source_key} --check")


async def _check_ruijie_token(token: str) -> tuple[bool, str]:
    """通过锐捷 API 验证 token 是否有效。"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.get(
                "https://www.ruijie.com.cn/application/plf/softUser/getRoleName",
                headers={
                    "Cookie": f"GW_ACCESS_TOKEN={token}; xp-Admin-Token={token}",
                    "X-Requested-With": "XMLHttpRequest",
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                        " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
            )
            try:
                data = resp.json()
            except Exception:
                # 响应不是 JSON（可能是登录页或错误页）
                if resp.status_code in (401, 403):
                    return False, "token 已过期或无效"
                return False, f"服务器返回非 JSON 响应 (HTTP {resp.status_code})"

            if isinstance(data, dict) and data.get("code") == 200:
                role_data = data.get("data")
                if role_data is None:
                    # code=200 但 data 为 null → token 无效或已过期
                    return False, "token 无效或已过期（服务器返回空会话）"
                role = role_data.get("roleName", "未知") if isinstance(role_data, dict) else "未知"
                return True, f"角色: {role}"
            msg = data.get("message", "") if isinstance(data, dict) else ""
            return False, msg or f"HTTP {resp.status_code}"
    except Exception as exc:
        return False, str(exc)


def main() -> None:
    cli(prog_name="firmatlas")
