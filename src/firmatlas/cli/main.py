"""FirmAtlas CLI 入口。

MVP 命令结构见 README 0x0F；子命令随各开发阶段逐步注册。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import click

from firmatlas import __version__
from firmatlas.app import registry
from firmatlas.app.crawl import CrawlReport, crawl_source
from firmatlas.app.queries import OUTPUT_SCHEMA_VERSION, CatalogFilter
from firmatlas.domain.errors import FirmAtlasError
from firmatlas.domain.model import (
    DownloadStatus,
    ProductFamily,
    ProductType,
    VerificationStatus,
    VisibilityStatus,
)
from firmatlas.infra import database
from firmatlas.infra.http_client import HttpFetcher, make_http_client
from firmatlas.infra.query_service import SqliteCatalogQueryService
from firmatlas.infra.repository import SqliteUnitOfWorkFactory


@click.group(name="firmatlas")
@click.version_option(version=__version__, prog_name="firmatlas")
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False),
    default="data",
    show_default=True,
    help="数据根目录（数据库、固件与临时文件所在位置）。",
)
@click.option("--verbose", is_flag=True, default=False, help="输出详细日志到标准错误。")
@click.option("--no-color", is_flag=True, default=False, help="禁用彩色输出。")
@click.pass_context
def cli(ctx: click.Context, data_dir: str, verbose: bool, no_color: bool) -> None:
    """FirmAtlas：IoT 固件目录采集与按需下载。"""
    ctx.ensure_object(dict)
    ctx.obj["data_dir"] = data_dir
    ctx.obj["verbose"] = verbose
    ctx.obj["no_color"] = no_color


@cli.command(name="init")
@click.pass_context
def init_command(ctx: click.Context) -> None:
    """初始化数据目录与数据库（可重复执行）。"""
    data_dir = Path(ctx.obj["data_dir"])
    try:
        result = database.initialize(data_dir)
        # 幂等写入内置来源（已存在的 source_key 跳过）
        engine = database.open_database(data_dir)
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
    try:
        registry.check_supported(source_key)
        engine = database.open_database(data_dir)
    except FirmAtlasError as exc:
        raise click.ClickException(str(exc)) from exc

    async def _run() -> CrawlReport:
        async with make_http_client() as client:
            adapter = registry.build_adapter(source_key, HttpFetcher(client))
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
        engine = database.open_database(data_dir)
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
        engine = database.open_database(data_dir)
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
        engine = database.open_database(data_dir)
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
    click.echo("提示：用 firmatlas show <release-id> 查看详情（ID 可用前缀）。")


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
        engine = database.open_database(data_dir)
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


def main() -> None:
    cli(prog_name="firmatlas")
