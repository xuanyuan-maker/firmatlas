"""有效配置的默认值、TOML 加载、覆盖优先级与校验测试。"""

from pathlib import Path

import pytest

from firmatlas.app.config import load_config
from firmatlas.domain.errors import ConfigError


def test_load_config_uses_defaults_without_file():
    config = load_config()

    assert config.data_dir == Path("data")
    assert config.verbose is False
    assert config.no_color is False
    assert config.http.request_timeout == 30.0
    assert config.http.connect_timeout == 10.0
    assert config.http.max_retries == 3
    assert config.http.retry_backoff_base == 1.0
    assert config.download.read_timeout == 60.0
    assert config.download.connect_timeout == 10.0
    assert config.config_path is None


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
