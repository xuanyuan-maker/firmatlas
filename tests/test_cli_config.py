"""全局配置参数与 firmatlas config 命令测试。"""

from click.testing import CliRunner

from firmatlas.adapters.events import DiscoveryCompleted
from firmatlas.app import registry
from firmatlas.cli import main as cli_main
from firmatlas.cli.main import cli


def test_config_command_shows_defaults(tmp_path):
    data_dir = tmp_path / "data"
    result = CliRunner().invoke(cli, ["--data-dir", str(data_dir), "config"])

    assert result.exit_code == 0, result.output
    assert "配置文件：未指定" in result.output
    assert f"数据目录：{data_dir}" in result.output
    assert "详细日志：关闭" in result.output
    assert "颜色输出：开启" in result.output
    assert "HTTP 请求超时：30s" in result.output
    assert "HTTP 最大重试次数：3" in result.output
    assert "下载读取超时：60s" in result.output


def test_config_command_merges_file_and_cli_options(tmp_path):
    file_data_dir = tmp_path / "file-data"
    cli_data_dir = tmp_path / "cli-data"
    config_path = tmp_path / "firmatlas.toml"
    config_path.write_text(
        f"""
data_dir = "{file_data_dir}"
verbose = false
no_color = true

[http]
request_timeout = 45
max_retries = 5

[download]
read_timeout = 90
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--config",
            str(config_path),
            "--data-dir",
            str(cli_data_dir),
            "--verbose",
            "config",
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"配置文件：{config_path}" in result.output
    assert f"数据目录：{cli_data_dir}" in result.output
    assert "详细日志：开启" in result.output
    assert "颜色输出：关闭" in result.output
    assert "HTTP 请求超时：45s" in result.output
    assert "HTTP 最大重试次数：5" in result.output
    assert "下载读取超时：90s" in result.output
    assert not file_data_dir.exists()
    assert cli_data_dir.is_dir()


def test_invalid_config_is_reported_before_data_directory_lock(tmp_path):
    data_dir = tmp_path / "should-not-exist"
    config_path = tmp_path / "invalid.toml"
    config_path.write_text(f'data_dir = "{data_dir}"\n[http]\nmax_retries = -1', encoding="utf-8")

    result = CliRunner().invoke(cli, ["--config", str(config_path), "config"])

    assert result.exit_code != 0
    assert "max_retries" in result.output
    assert "不能小于 0" in result.output
    assert not data_dir.exists()


def test_crawl_receives_effective_http_config(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    config_path = tmp_path / "firmatlas.toml"
    config_path.write_text(
        f"""
data_dir = "{data_dir}"
[http]
request_timeout = 41
connect_timeout = 7
max_retries = 2
retry_backoff_base = 0.25
""".strip(),
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["--config", str(config_path), "init"])
    assert result.exit_code == 0, result.output

    captured = {}

    class DummyClientContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

    class EmptyAdapter:
        def __init__(self, source_key):
            self.source_key = source_key

        async def discover(self):
            yield DiscoveryCompleted(is_complete=True, incomplete_reason=None, issues=())

    def make_client(**options):
        captured["client"] = options
        return DummyClientContext()

    def make_fetcher(client, **options):
        captured["fetcher"] = options
        return object()

    monkeypatch.setattr(cli_main, "make_http_client", make_client)
    monkeypatch.setattr(cli_main, "HttpFetcher", make_fetcher)
    monkeypatch.setattr(
        registry,
        "build_adapter",
        lambda source_key, http: EmptyAdapter(source_key),
    )

    result = runner.invoke(cli, ["--config", str(config_path), "crawl", "tp-link-cn"])

    assert result.exit_code == 0, result.output
    assert captured["client"] == {
        "request_timeout": 41.0,
        "connect_timeout": 7.0,
        "legacy_tls": False,
    }
    assert captured["fetcher"] == {"max_retries": 2, "retry_backoff_base": 0.25}

    result = runner.invoke(cli, ["--config", str(config_path), "crawl", "dlink-us"])

    assert result.exit_code == 0, result.output
    assert captured["client"]["legacy_tls"] is True
