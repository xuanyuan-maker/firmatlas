"""FirmAtlas 有效配置：默认值、TOML 文件和 CLI 覆盖合并。"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Any

from firmatlas.domain.errors import ConfigError


@dataclass(frozen=True)
class HttpConfig:
    request_timeout: float = 30.0
    connect_timeout: float = 10.0
    max_retries: int = 3
    retry_backoff_base: float = 1.0


@dataclass(frozen=True)
class DownloadConfig:
    read_timeout: float = 60.0
    connect_timeout: float = 10.0


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path = field(default_factory=lambda: _platform_data_dir())
    verbose: bool = False
    no_color: bool = False
    http: HttpConfig = field(default_factory=HttpConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    config_path: Path | None = None


_ROOT_KEYS = frozenset({"data_dir", "verbose", "no_color", "http", "download"})
_HTTP_KEYS = frozenset({"request_timeout", "connect_timeout", "max_retries", "retry_backoff_base"})
_DOWNLOAD_KEYS = frozenset({"read_timeout", "connect_timeout"})


def load_config(
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
    verbose: bool | None = None,
    no_color: bool | None = None,
) -> AppConfig:
    """按“平台默认值 → TOML → 环境变量 → CLI 参数”生成有效配置。"""
    selected_config_path, is_explicit_config = _select_config_path(config_path)
    if is_explicit_config or selected_config_path.exists():
        raw = _load_toml(selected_config_path)
        loaded_config_path: Path | None = selected_config_path
    else:
        raw = {}
        loaded_config_path = None
    _reject_unknown(raw, _ROOT_KEYS, "根配置")

    default = AppConfig()
    file_data_dir = _path_value(raw, "data_dir", default.data_dir)
    env_data_dir = _environment_path("FIRMATLAS_DATA_DIR")
    file_verbose = _bool_value(raw, "verbose", default.verbose)
    file_no_color = _bool_value(raw, "no_color", default.no_color)

    http_raw = _section(raw, "http")
    _reject_unknown(http_raw, _HTTP_KEYS, "http")
    http = HttpConfig(
        request_timeout=_positive_number(http_raw, "request_timeout", default.http.request_timeout),
        connect_timeout=_positive_number(http_raw, "connect_timeout", default.http.connect_timeout),
        max_retries=_non_negative_int(http_raw, "max_retries", default.http.max_retries),
        retry_backoff_base=_non_negative_number(
            http_raw, "retry_backoff_base", default.http.retry_backoff_base
        ),
    )

    download_raw = _section(raw, "download")
    _reject_unknown(download_raw, _DOWNLOAD_KEYS, "download")
    download = DownloadConfig(
        read_timeout=_positive_number(download_raw, "read_timeout", default.download.read_timeout),
        connect_timeout=_positive_number(
            download_raw, "connect_timeout", default.download.connect_timeout
        ),
    )

    return AppConfig(
        data_dir=(
            data_dir
            if data_dir is not None
            else env_data_dir
            if env_data_dir is not None
            else file_data_dir
        ),
        verbose=verbose if verbose is not None else file_verbose,
        no_color=no_color if no_color is not None else file_no_color,
        http=http,
        download=download,
        config_path=loaded_config_path,
    )


def _select_config_path(config_path: Path | None) -> tuple[Path, bool]:
    """选择配置文件，并标记该路径是否由用户显式指定。"""
    if config_path is not None:
        return config_path, True

    environment_path = _environment_path("FIRMATLAS_CONFIG")
    if environment_path is not None:
        return environment_path, True
    return _platform_config_path(), False


def _environment_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if value is None:
        return None
    if not value.strip():
        raise ConfigError(f"环境变量 {name} 必须是非空路径。")
    return Path(value)


def _platform_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FirmAtlas" / "config.toml"
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home and config_home.strip() else Path.home() / ".config"
    return base / "firmatlas" / "config.toml"


def _platform_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FirmAtlas" / "data"
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home and data_home.strip() else Path.home() / ".local" / "share"
    return base / "firmatlas"


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件 {path} 不存在。") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件 {path} 不是有效 TOML：{exc}") from exc


def _reject_unknown(values: dict[str, Any], allowed: frozenset[str], section: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigError(f"{section}包含未知字段：{', '.join(unknown)}")


def _section(values: dict[str, Any], key: str) -> dict[str, Any]:
    value = values.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"配置项 {key} 必须是 TOML 表。")
    return value


def _path_value(values: dict[str, Any], key: str, default: Path) -> Path:
    value = values.get(key, str(default))
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"配置项 {key} 必须是非空路径字符串。")
    return Path(value)


def _bool_value(values: dict[str, Any], key: str, default: bool) -> bool:
    value = values.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"配置项 {key} 必须是布尔值。")
    return value


def _positive_number(values: dict[str, Any], key: str, default: float) -> float:
    value = _number(values, key, default)
    if value <= 0:
        raise ConfigError(f"配置项 {key} 必须大于 0。")
    return value


def _non_negative_number(values: dict[str, Any], key: str, default: float) -> float:
    value = _number(values, key, default)
    if value < 0:
        raise ConfigError(f"配置项 {key} 不能小于 0。")
    return value


def _number(values: dict[str, Any], key: str, default: float) -> float:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"配置项 {key} 必须是数字。")
    number = float(value)
    if not isfinite(number):
        raise ConfigError(f"配置项 {key} 必须是有限数字。")
    return number


def _non_negative_int(values: dict[str, Any], key: str, default: int) -> int:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"配置项 {key} 必须是整数。")
    if value < 0:
        raise ConfigError(f"配置项 {key} 不能小于 0。")
    if value > 10:
        raise ConfigError(f"配置项 {key} 不能大于 10。")
    return value
