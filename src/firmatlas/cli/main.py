"""FirmAtlas CLI 入口。

MVP 命令结构见 README 0x0F；子命令随各开发阶段逐步注册。
"""

from __future__ import annotations

from pathlib import Path

import click

from firmatlas import __version__
from firmatlas.domain.errors import FirmAtlasError
from firmatlas.infra import database


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
    except FirmAtlasError as exc:
        raise click.ClickException(str(exc)) from exc
    if result.created:
        click.echo(f"已初始化数据库：{result.db_path}（结构版本 {result.schema_version}）")
    else:
        click.echo(
            f"数据库已初始化，未做改动：{result.db_path}（结构版本 {result.schema_version}）"
        )


def main() -> None:
    cli(prog_name="firmatlas")
