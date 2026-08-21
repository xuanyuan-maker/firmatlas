"""有效配置的默认值、TOML 加载、覆盖优先级与校验测试。"""

from pathlib import Path

import pytest

import firmatlas.app.config as config_module
from firmatlas.app.config import load_config
from firmatlas.domain.errors import ConfigError


def test_load_config_uses_linux_platform_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("FIRMATLAS_CONFIG", raising=False)
    monkeypatch.delenv("FIRMATLAS_DATA_DIR", raising=False)

    config = load_config()

    assert config.data_dir == tmp_path / "home" / ".local" / "share" / "firmatlas"
    assert config.database_dir == config.data_dir
    assert config.download_dir == config.data_dir
    assert config.verbose is False
    assert config.no_color is False
    assert config.http.request_timeout == 30.0
    assert config.http.connect_timeout == 10.0
    assert config.http.max_retries == 3
    assert config.http.retry_backoff_base == 1.0
    assert config.download.read_timeout == 60.0
    assert config.download.connect_timeout == 10.0
    assert config.catalog.mode == "standalone"
    assert config.catalog.manifest_url is None
    assert config.catalog.backup_count == 2
    assert config.catalog.allow_insecure_http is False
    assert config.config_path is None


def test_load_config_uses_xdg_platform_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.sys, "platform", "linux")
    config_home = tmp_path / "config-home"
    data_home = tmp_path / "data-home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.delenv("FIRMATLAS_CONFIG", raising=False)
    monkeypatch.delenv("FIRMATLAS_DATA_DIR", raising=False)

    default_config = config_home / "firmatlas" / "config.toml"
    default_config.parent.mkdir(parents=True)
    default_config.write_text('data_dir = "from-default-file"', encoding="utf-8")

    config = load_config()

    assert config.config_path == default_config
    assert config.data_dir == Path("from-default-file")


def test_load_config_uses_macos_platform_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FIRMATLAS_CONFIG", raising=False)
    monkeypatch.delenv("FIRMATLAS_DATA_DIR", raising=False)

    config = load_config()

    app_support = tmp_path / "home" / "Library" / "Application Support" / "FirmAtlas"
    assert config.config_path is None
    assert config.data_dir == app_support / "data"


def test_load_config_prioritizes_cli_over_environment_over_toml(tmp_path, monkeypatch):
    toml_data_dir = tmp_path / "from-toml"
    environment_data_dir = tmp_path / "from-environment"
    cli_data_dir = tmp_path / "from-cli"
    config_path = tmp_path / "firmatlas.toml"
    config_path.write_text(f'data_dir = "{toml_data_dir}"', encoding="utf-8")
    monkeypatch.setenv("FIRMATLAS_CONFIG", str(config_path))
    monkeypatch.setenv("FIRMATLAS_DATA_DIR", str(environment_data_dir))

    environment_config = load_config()
    cli_config = load_config(data_dir=cli_data_dir)

    assert environment_config.data_dir == environment_data_dir
    assert cli_config.data_dir == cli_data_dir
    assert environment_config.database_dir == environment_data_dir
    assert environment_config.download_dir == environment_data_dir
    assert cli_config.database_dir == cli_data_dir
    assert cli_config.download_dir == cli_data_dir


def test_load_config_parses_separate_database_and_download_directories(tmp_path):
    data_dir = tmp_path / "data"
    database_dir = tmp_path / "database"
    download_dir = tmp_path / "downloads"
    path = tmp_path / "firmatlas.toml"
    path.write_text(
        (
            f'data_dir = "{data_dir}"\n'
            f'database_dir = "{database_dir}"\n'
            f'download_dir = "{download_dir}"'
        ),
        encoding="utf-8",
    )

    config = load_config(config_path=path)

    assert config.data_dir == data_dir
    assert config.database_dir == database_dir
    assert config.download_dir == download_dir


def test_load_config_uses_environment_config_path(tmp_path, monkeypatch):
    config_path = tmp_path / "from-environment.toml"
    config_path.write_text("verbose = true", encoding="utf-8")
    monkeypatch.setenv("FIRMATLAS_CONFIG", str(config_path))

    config = load_config()

    assert config.config_path == config_path
    assert config.verbose is True


def test_load_config_parses_catalog_settings(tmp_path):
    path = tmp_path / "firmatlas.toml"
    path.write_text(
        """
[catalog]
mode = "managed"
manifest_url = "https://catalog.example.com/manifest.json"
backup_count = 5
allow_insecure_http = false
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path=path)

    assert config.catalog.mode == "managed"
    assert config.catalog.manifest_url == "https://catalog.example.com/manifest.json"
    assert config.catalog.backup_count == 5
    assert config.catalog.allow_insecure_http is False


def test_load_config_allows_http_only_when_explicitly_enabled(tmp_path):
    path = tmp_path / "firmatlas.toml"
    path.write_text(
        """
[catalog]
mode = "managed"
manifest_url = "http://catalog.example.com/manifest.json"
allow_insecure_http = true
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path=path)

    assert config.catalog.manifest_url == "http://catalog.example.com/manifest.json"
    assert config.catalog.allow_insecure_http is True


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('[catalog]\nmode = "remote"', "只能是 standalone 或 managed"),
        ('[catalog]\nmode = "managed"', "必须配置 catalog.manifest_url"),
        ("[catalog]\nbackup_count = 0", "必须在 1 到 10 之间"),
        ("[catalog]\nbackup_count = 11", "必须在 1 到 10 之间"),
        ("[catalog]\nbackup_count = true", "必须是整数"),
        ('[catalog]\nallow_insecure_http = "yes"', "必须是布尔值"),
        (
            '[catalog]\nmanifest_url = "http://catalog.example.com/manifest.json"',
            "必须同时开启",
        ),
        ('[catalog]\nmanifest_url = "ftp://catalog.example.com/manifest.json"', "只允许使用"),
        ('[catalog]\nmanifest_url = "https://"', "不是有效的 HTTP URL"),
        ('[catalog]\nmanifest_url = "file://"', "不是有效的 file URL"),
    ],
)
def test_load_config_rejects_invalid_catalog_settings(tmp_path, content, message):
    path = tmp_path / "invalid-catalog.toml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(config_path=path)


def test_load_config_allows_file_manifest_url(tmp_path):
    path = tmp_path / "firmatlas.toml"
    path.write_text(
        """
[catalog]
mode = "managed"
manifest_url = "file:///srv/firmatlas-catalog/manifest.json"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path=path)

    assert config.catalog.manifest_url == "file:///srv/firmatlas-catalog/manifest.json"


@pytest.mark.parametrize("environment_name", ["FIRMATLAS_CONFIG", "FIRMATLAS_DATA_DIR"])
def test_load_config_rejects_empty_path_environment(environment_name, monkeypatch):
    monkeypatch.setenv(environment_name, " ")

    with pytest.raises(ConfigError, match="非空路径"):
        load_config()


def test_load_config_ignores_missing_default_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing-config-home"))
    monkeypatch.delenv("FIRMATLAS_CONFIG", raising=False)
    monkeypatch.delenv("FIRMATLAS_DATA_DIR", raising=False)

    config = load_config()

    assert config.config_path is None


@pytest.mark.parametrize("config_source", ["cli", "environment"])
def test_load_config_reports_missing_explicit_config(tmp_path, monkeypatch, config_source):
    missing = tmp_path / "missing.toml"
    if config_source == "environment":
        monkeypatch.setenv("FIRMATLAS_CONFIG", str(missing))
        with pytest.raises(ConfigError, match="不存在"):
            load_config()
    else:
        with pytest.raises(ConfigError, match="不存在"):
            load_config(config_path=missing)


def test_load_config_merges_toml_then_cli_overrides(tmp_path):
    path = tmp_path / "firmatlas.toml"
    path.write_text(
        """
data_dir = "from-file"
verbose = true
no_color = true

[http]
request_timeout = 45
connect_timeout = 12.5
max_retries = 5
retry_backoff_base = 0.5

[download]
read_timeout = 90
connect_timeout = 8
""".strip(),
        encoding="utf-8",
    )

    config = load_config(
        config_path=path,
        data_dir=Path("from-cli"),
        verbose=False,
    )

    assert config.data_dir == Path("from-cli")
    assert config.database_dir == Path("from-cli")
    assert config.download_dir == Path("from-cli")
    assert config.verbose is False
    assert config.no_color is True
    assert config.http.request_timeout == 45.0
    assert config.http.connect_timeout == 12.5
    assert config.http.max_retries == 5
    assert config.http.retry_backoff_base == 0.5
    assert config.download.read_timeout == 90.0
    assert config.download.connect_timeout == 8.0
    assert config.config_path == path


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("unknown = 1", "未知字段"),
        ("verbose = 1", "必须是布尔值"),
        ("[http]\nmax_retries = -1", "不能小于 0"),
        ("[http]\nmax_retries = 11", "不能大于 10"),
        ("[http]\nrequest_timeout = 0", "必须大于 0"),
        ("[http]\nretry_backoff_base = inf", "必须是有限数字"),
        ("[download]\nread_timeout = 'slow'", "必须是数字"),
    ],
)
def test_load_config_rejects_invalid_values(tmp_path, content, message):
    path = tmp_path / "invalid.toml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(config_path=path)


def test_load_config_rejects_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="不存在"):
        load_config(config_path=tmp_path / "missing.toml")
