"""FirmAtlas CLI 入口。

MVP 命令结构见 README 0x0F；子命令随各开发阶段逐步注册。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from firmatlas import __version__
from firmatlas.app import registry
from firmatlas.app.crawl import CrawlReport, crawl_source
from firmatlas.domain.errors import FirmAtlasError
from firmatlas.infra import database
from firmatlas.infra.http_client import HttpFetcher, make_http_client
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


def main() -> None:
    cli(prog_name="firmatlas")
