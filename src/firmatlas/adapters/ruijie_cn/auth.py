"""锐捷中国站 Token 管理。

Token 来源：浏览器登录锐捷官网后，从 Cookie 中获取 GW_ACCESS_TOKEN。
Token 有效期约 8 小时（expires_in: 28800），过期需重新获取。

加载优先级：
  1. 环境变量 RUIJIE_TOKEN
  2. data/auth/ruijie-cn.token 文件
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from firmatlas.domain.errors import FirmAtlasError

TOKEN_ENV_VAR = "RUIJIE_TOKEN"
TOKEN_FILENAME = "ruijie-cn.token"


class TokenNotConfiguredError(FirmAtlasError):
    """未配置锐捷 token。"""


@dataclass(frozen=True)
class TokenInfo:
    """token 及其来源的描述。"""

    token: str
    source: str  # "env" | "file"


def load_token(data_dir: Path | None = None) -> TokenInfo:
    """加载锐捷 token。

    优先级：环境变量 > data_dir/auth/ruijie-cn.token > 报错
    """
    # 1. 环境变量
    token = os.environ.get(TOKEN_ENV_VAR)
    if token:
        return TokenInfo(token=token.strip(), source="env")

    # 2. 文件
    if data_dir is not None:
        token_path = data_dir / "auth" / TOKEN_FILENAME
        if token_path.exists():
            return TokenInfo(token=token_path.read_text().strip(), source="file")

    raise TokenNotConfiguredError(
        "未配置锐捷登录 token。请通过以下方式之一设置：\n"
        f"  1. 环境变量: export {TOKEN_ENV_VAR}=\"你的token\"\n"
        f"  2. 命令保存: firmatlas auth ruijie-cn --save \"你的token\"\n"
        "获取 token: 浏览器登录 ruijie.com.cn 后，F12 控制台执行：\n"
        "  document.cookie.match(/GW_ACCESS_TOKEN=([^;]+)/)[1]"
    )


def save_token(token: str, data_dir: Path) -> Path:
    """保存 token 到 data/auth/ 目录。"""
    auth_dir = data_dir / "auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    token_path = auth_dir / TOKEN_FILENAME
    token_path.write_text(token.strip())
    return token_path
