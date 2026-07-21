"""CLI crawl / sources / runs 命令测试。

crawl 命令通过 monkeypatch 替换 registry.build_adapter 注入假适配器，
不发任何真实请求；数据库用 CliRunner 隔离目录下的真实 SQLite。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from click.testing import CliRunner

from firmatlas.adapters.events import DiscoveredProduct, DiscoveryCompleted
from firmatlas.app import registry
from firmatlas.cli.main import cli
from firmatlas.infra import database
from firmatlas.infra.repository import SqliteUnitOfWorkFactory


@pytest.fixture(autouse=True)
def no_proxy_env(monkeypatch):
    """清除代理环境变量：测试不发真实请求，但 httpx 构造客户端时会读取它们。"""
    for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)


class FakeAdapter:
    source_key = "tp-link-cn"

    def __init__(self, events):
        self._events = events

    async def discover(self):
        for event in self._events:
            yield event


def make_fake_builder(events):
    def _build(source_key, http):
        assert source_key == "tp-link-cn"
        return FakeAdapter(events)

    return _build


def completed(**overrides) -> DiscoveryCompleted:
    fields = {"is_complete": True, "incomplete_reason": None, "issues": ()}
    fields.update(overrides)
    return DiscoveryCompleted(**fields)


def test_init_seeds_sources(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["--data-dir", str(tmp_path / "data"), "init"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli, ["--data-dir", str(tmp_path / "data"), "sources"])
    assert result.exit_code == 0, result.output
    assert "tp-link-cn" in result.output
    assert "TP-Link" in result.output
    assert "hikvision-global" in result.output
    assert "Hikvision" in result.output
    assert "dlink-us" in result.output
    assert "D-Link" in result.output
    assert "omada-global" in result.output
    assert "Omada" in result.output


def test_init_is_idempotent_for_seeds(tmp_path):
    runner = CliRunner()
    data = str(tmp_path / "data")
    runner.invoke(cli, ["--data-dir", data, "init"])
    result = runner.invoke(cli, ["--data-dir", data, "init"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli, ["--data-dir", data, "sources"])
    assert result.output.count("tp-link-cn") == 1
    assert result.output.count("hikvision-global") == 1
    assert result.output.count("dlink-us") == 1
    assert result.output.count("omada-global") == 1


def test_sources_requires_init(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["--data-dir", str(tmp_path / "data"), "sources"])
    assert result.exit_code != 0
    assert "init" in result.output


def test_crawl_and_runs_end_to_end(tmp_path, monkeypatch, make_product_candidate):
    runner = CliRunner()
    data = str(tmp_path / "data")
    runner.invoke(cli, ["--data-dir", data, "init"])

    events = [DiscoveredProduct(product=make_product_candidate()), completed()]
    monkeypatch.setattr(registry, "build_adapter", make_fake_builder(events))

    result = runner.invoke(cli, ["--data-dir", data, "crawl", "tp-link-cn"])
    assert result.exit_code == 0, result.output
    assert "completed" in result.output
    assert "产品 1" in result.output

    result = runner.invoke(cli, ["--data-dir", data, "runs"])
    assert result.exit_code == 0, result.output
    assert "completed" in result.output
    assert "新增 4" in result.output

    # --source 筛选
    result = runner.invoke(cli, ["--data-dir", data, "runs", "--source", "tp-link-cn"])
    assert result.exit_code == 0
    assert "completed" in result.output


def test_crawl_failed_run_exits_nonzero(tmp_path, monkeypatch):
    runner = CliRunner()
    data = str(tmp_path / "data")
    runner.invoke(cli, ["--data-dir", data, "init"])

    class ExplodingAdapter:
        source_key = "tp-link-cn"

        async def discover(self):
            raise RuntimeError("API 入口不可达")
            yield  # noqa: B901 —— 使函数成为异步生成器

    monkeypatch.setattr(registry, "build_adapter", lambda key, http: ExplodingAdapter())

    result = runner.invoke(cli, ["--data-dir", data, "crawl", "tp-link-cn"])
    assert result.exit_code == 1
    assert "failed" in result.output

    # run 已落库为 failed
    result = runner.invoke(cli, ["--data-dir", data, "runs"])
    assert "failed" in result.output
    assert "API 入口不可达" in result.output


def test_crawl_unsupported_source(tmp_path):
    runner = CliRunner()
    data = str(tmp_path / "data")
    runner.invoke(cli, ["--data-dir", data, "init"])

    result = runner.invoke(cli, ["--data-dir", data, "crawl", "no-such-source"])
    assert result.exit_code != 0
    assert "不支持的来源" in result.output


def test_crawl_requires_init(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["--data-dir", str(tmp_path / "data"), "crawl", "tp-link-cn"])
    assert result.exit_code != 0
    assert "init" in result.output


def test_runs_empty(tmp_path):
    runner = CliRunner()
    data = str(tmp_path / "data")
    runner.invoke(cli, ["--data-dir", data, "init"])
    result = runner.invoke(cli, ["--data-dir", data, "runs"])
    assert result.exit_code == 0
    assert "暂无采集记录" in result.output


def test_cli_startup_recovers_stale_crawl_run(tmp_path):
    runner = CliRunner()
    data_dir = tmp_path / "data"
    result = runner.invoke(cli, ["--data-dir", str(data_dir), "init"])
    assert result.exit_code == 0, result.output

    engine = database.open_database(data_dir)
    try:
        with SqliteUnitOfWorkFactory(engine).begin() as uow:
            source = uow.sources.get_by_source_key("tp-link-cn")
            assert source is not None
            stale = uow.runs.create_run(source_id=source.id, started_at=datetime.now(UTC))
    finally:
        engine.dispose()

    result = runner.invoke(cli, ["--data-dir", str(data_dir), "runs"])

    assert result.exit_code == 0, result.output
    assert "已恢复上次异常中断的任务：采集 1，下载 0" in result.output
    assert stale.id in result.output
    assert "failed" in result.output
    assert "异常终止" in result.output
