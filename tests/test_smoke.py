"""工程环境冒烟测试：CLI 可调用、版本号一致。"""

from click.testing import CliRunner

from firmatlas import __version__
from firmatlas.cli.main import cli


def test_cli_help() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "firmatlas" in result.output


def test_cli_version() -> None:
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
