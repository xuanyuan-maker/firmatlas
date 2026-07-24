"""ruijie-cn token 管理测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from firmatlas.adapters.ruijie_cn.auth import (
    TokenNotConfiguredError,
    load_token,
    save_token,
)


class TestLoadToken:
    """测试 token 加载优先级。"""

    def test_load_from_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """环境变量优先级最高。"""
        monkeypatch.setenv("RUIJIE_TOKEN", "env-token-abc")
        info = load_token(tmp_path)
        assert info.token == "env-token-abc"
        assert info.source == "env"

    def test_load_from_file(self, tmp_path: Path) -> None:
        """环境变量未设置时从文件加载。"""
        # 确保环境变量未设置
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.delenv("RUIJIE_TOKEN", raising=False)
        try:
            save_token("file-token-xyz", tmp_path)
            info = load_token(tmp_path)
            assert info.token == "file-token-xyz"
            assert info.source == "file"
        finally:
            monkeypatch.undo()

    def test_env_overrides_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """环境变量优先于文件。"""
        save_token("file-token", tmp_path)
        monkeypatch.setenv("RUIJIE_TOKEN", "env-token")
        info = load_token(tmp_path)
        assert info.token == "env-token"
        assert info.source == "env"

    def test_no_token_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """未配置 token 时抛出 TokenNotConfiguredError。"""
        monkeypatch.delenv("RUIJIE_TOKEN", raising=False)
        # 确保文件不存在
        token_file = tmp_path / "auth" / "ruijie-cn.token"
        if token_file.exists():
            token_file.unlink()
        with pytest.raises(TokenNotConfiguredError, match="未配置锐捷登录 token"):
            load_token(tmp_path)

    def test_load_without_data_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不传 data_dir 时只检查环境变量。"""
        monkeypatch.setenv("RUIJIE_TOKEN", "env-only")
        info = load_token()
        assert info.token == "env-only"

    def test_load_without_data_dir_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不传 data_dir 且无环境变量时报错。"""
        monkeypatch.delenv("RUIJIE_TOKEN", raising=False)
        with pytest.raises(TokenNotConfiguredError):
            load_token()

    def test_token_strip_whitespace(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """token 首尾空白被去除。"""
        monkeypatch.setenv("RUIJIE_TOKEN", "  token-with-spaces  ")
        info = load_token(tmp_path)
        assert info.token == "token-with-spaces"


class TestSaveToken:
    """测试 token 保存。"""

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        """保存时自动创建 auth 目录。"""
        token_path = save_token("my-token", tmp_path)
        assert token_path.exists()
        assert token_path.read_text() == "my-token"
        assert token_path.parent.name == "auth"

    def test_save_strips_whitespace(self, tmp_path: Path) -> None:
        """保存时去除首尾空白。"""
        token_path = save_token("  my-token\n  ", tmp_path)
        assert token_path.read_text() == "my-token"

    def test_overwrite_existing(self, tmp_path: Path) -> None:
        """覆盖已有 token。"""
        save_token("old-token", tmp_path)
        save_token("new-token", tmp_path)
        info = load_token(tmp_path)
        assert info.token == "new-token"
