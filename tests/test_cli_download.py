"""CLI download / downloads 命令测试。

数据经 crawl 命令（假适配器）入库，download 命令通过 monkeypatch
替换 cli.main.Downloader 注入脚本化下载器，不发任何真实请求。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from firmatlas.adapters.events import DiscoveredProduct, DiscoveryCompleted
from firmatlas.app import registry
from firmatlas.cli import main as cli_main
from firmatlas.cli.main import cli
from firmatlas.domain.model import DownloadErrorCode, DownloadFailed, DownloadSucceeded

CONTENT = b"cli-firmware" * 64
CONTENT_SHA256 = hashlib.sha256(CONTENT).hexdigest()


@pytest.fixture(autouse=True)
def no_proxy_env(monkeypatch):
    for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)


class FakeAdapter:
    source_key = "tp-link-cn"

    def __init__(self, events):
        self._events = events

    async def discover(self):
        for event in self._events:
            yield event


class ScriptedDownloader:
    """替换 cli.main.Downloader 的假下载器类（构造参数与真类一致）。"""

    outcomes: list = []  # 类属性：测试逐例预设
    options: dict = {}

    def __init__(self, client, *, read_timeout=60, connect_timeout=10):
        ScriptedDownloader.options = {
            "read_timeout": read_timeout,
            "connect_timeout": connect_timeout,
        }

    async def download(
        self, *, url, dest: Path, expected_size=None, on_progress=None, referer=None,
        size_tolerance=0,
    ):
        outcome = ScriptedDownloader.outcomes.pop(0)
        if isinstance(outcome, DownloadSucceeded):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(CONTENT)
        return outcome


def succeeded() -> DownloadSucceeded:
    return DownloadSucceeded(
        bytes_received=len(CONTENT), sha256=CONTENT_SHA256, etag=None, last_modified=None
    )


@pytest.fixture
def seeded_cli(tmp_path, monkeypatch, make_product_candidate):
    """init + 假适配器 crawl，返回 (runner, data_dir, artifact_id)。"""
    runner = CliRunner()
    data = str(tmp_path / "data")
    runner.invoke(cli, ["--data-dir", data, "init"])

    events = [
        DiscoveredProduct(product=make_product_candidate()),
        DiscoveryCompleted(is_complete=True, incomplete_reason=None, issues=()),
    ]

    def fake_builder(source_key, http):
        return FakeAdapter(list(events))

    monkeypatch.setattr(registry, "build_adapter", fake_builder)
    result = runner.invoke(cli, ["--data-dir", data, "crawl", "tp-link-cn"])
    assert result.exit_code == 0, result.output

    # 从 list --format json 取 release，再 show 取 artifact_id
    import json

    result = runner.invoke(cli, ["--data-dir", data, "list", "--format", "json"])
    release_id = json.loads(result.output)["rows"][0]["release_id"]
    result = runner.invoke(cli, ["--data-dir", data, "show", release_id, "--format", "json"])
    artifact_id = json.loads(result.output)["release"]["artifacts"][0]["artifact_id"]

    monkeypatch.setattr(cli_main, "Downloader", ScriptedDownloader)
    return runner, data, artifact_id


def test_download_success_and_history(seeded_cli):
    runner, data, artifact_id = seeded_cli
    ScriptedDownloader.outcomes = [succeeded()]

    result = runner.invoke(cli, ["--data-dir", data, "download", artifact_id])
    assert result.exit_code == 0, result.output
    assert "下载完成" in result.output
    assert CONTENT_SHA256 in result.output
    assert "firmware/tp-link/CN" in result.output

    # 归档文件真实存在
    line = next(ln for ln in result.output.splitlines() if "归档位置" in ln)
    rel = line.split("：", 1)[1].strip()
    assert (Path(data) / rel).read_bytes() == CONTENT

    # downloads 历史可见
    result = runner.invoke(cli, ["--data-dir", data, "downloads"])
    assert result.exit_code == 0, result.output
    assert "completed" in result.output
    assert artifact_id[:8] in result.output


def test_download_receives_effective_timeout_config(seeded_cli, tmp_path):
    runner, data, artifact_id = seeded_cli
    config_path = tmp_path / "firmatlas.toml"
    config_path.write_text(
        f"""
data_dir = "{data}"
[download]
read_timeout = 75
connect_timeout = 6
""".strip(),
        encoding="utf-8",
    )
    ScriptedDownloader.outcomes = [succeeded()]

    result = runner.invoke(cli, ["--config", str(config_path), "download", artifact_id])

    assert result.exit_code == 0, result.output
    assert ScriptedDownloader.options == {"read_timeout": 75.0, "connect_timeout": 6.0}


def test_download_uses_source_tls_profile(seeded_cli, monkeypatch):
    """下载客户端按 Artifact 所属来源选择 TLS 配置。"""
    runner, data, artifact_id = seeded_cli
    captured = {}

    class DummyClientContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

    def make_client(**options):
        captured.update(options)
        return DummyClientContext()

    monkeypatch.setattr(cli_main, "make_http_client", make_client)
    monkeypatch.setattr(registry, "requires_legacy_tls", lambda source_key: True)
    ScriptedDownloader.outcomes = [succeeded()]

    result = runner.invoke(cli, ["--data-dir", data, "download", artifact_id])

    assert result.exit_code == 0, result.output
    assert captured["legacy_tls"] is True


def test_download_accepts_id_prefix(seeded_cli):
    runner, data, artifact_id = seeded_cli
    ScriptedDownloader.outcomes = [succeeded()]

    result = runner.invoke(cli, ["--data-dir", data, "download", artifact_id[:8]])
    assert result.exit_code == 0, result.output
    assert "下载完成" in result.output


def test_download_accepts_release_id(seeded_cli):
    """list 输出的发布 ID（单 Artifact 发布）可直接下载。"""
    import json

    runner, data, _ = seeded_cli
    result = runner.invoke(cli, ["--data-dir", data, "list", "--format", "json"])
    release_id = json.loads(result.output)["rows"][0]["release_id"]

    ScriptedDownloader.outcomes = [succeeded()]
    result = runner.invoke(cli, ["--data-dir", data, "download", release_id[:8]])
    assert result.exit_code == 0, result.output
    assert "下载完成" in result.output


def test_download_release_with_multiple_artifacts_lists_choices(
    tmp_path, monkeypatch, make_product_candidate, make_revision_candidate,
    make_release_candidate, make_artifact_candidate,
):
    """发布下有多个 Artifact：列出候选并要求指定，退出码 1。"""
    runner = CliRunner()
    data = str(tmp_path / "data")
    runner.invoke(cli, ["--data-dir", data, "init"])

    release = make_release_candidate(
        artifacts=(
            make_artifact_candidate(source_key="artifact-1", original_filename="fw-a.zip"),
            make_artifact_candidate(source_key="artifact-2", original_filename="fw-b.zip"),
        )
    )
    product = make_product_candidate(
        hardware_revisions=(make_revision_candidate(releases=(release,)),)
    )
    events = [
        DiscoveredProduct(product=product),
        DiscoveryCompleted(is_complete=True, incomplete_reason=None, issues=()),
    ]
    monkeypatch.setattr(registry, "build_adapter", lambda key, http: FakeAdapter(list(events)))
    result = runner.invoke(cli, ["--data-dir", data, "crawl", "tp-link-cn"])
    assert result.exit_code == 0, result.output

    import json

    result = runner.invoke(cli, ["--data-dir", data, "list", "--format", "json"])
    release_id = json.loads(result.output)["rows"][0]["release_id"]

    monkeypatch.setattr(cli_main, "Downloader", ScriptedDownloader)
    ScriptedDownloader.outcomes = []
    result = runner.invoke(cli, ["--data-dir", data, "download", release_id[:8]])
    assert result.exit_code == 1
    assert "2 个 Artifact" in result.output
    assert "fw-a.zip" in result.output
    assert "fw-b.zip" in result.output


def test_download_failure_exits_nonzero(seeded_cli):
    runner, data, artifact_id = seeded_cli
    ScriptedDownloader.outcomes = [
        DownloadFailed(
            error_code=DownloadErrorCode.HTTP_5XX, http_status=500,
            detail="HTTP 500", bytes_received=0,
        )
    ]

    result = runner.invoke(cli, ["--data-dir", data, "download", artifact_id])
    assert result.exit_code == 1
    assert "下载失败" in result.output

    result = runner.invoke(cli, ["--data-dir", data, "downloads", "--status", "failed"])
    assert "failed" in result.output


def test_download_unknown_artifact(seeded_cli):
    runner, data, _ = seeded_cli

    result = runner.invoke(cli, ["--data-dir", data, "download", "ffffffff"])
    assert result.exit_code == 1
    assert "未找到" in result.output


def test_downloads_empty(tmp_path):
    runner = CliRunner()
    data = str(tmp_path / "data")
    runner.invoke(cli, ["--data-dir", data, "init"])

    result = runner.invoke(cli, ["--data-dir", data, "downloads"])
    assert result.exit_code == 0
    assert "暂无下载记录" in result.output


def test_download_requires_init(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["--data-dir", str(tmp_path / "data"), "download", "abcd1234"])
    assert result.exit_code != 0
    assert "init" in result.output
