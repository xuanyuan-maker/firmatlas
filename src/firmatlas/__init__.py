"""FirmAtlas：面向个人漏洞猎人的 IoT 固件发现与按需获取工具。"""

from importlib.metadata import PackageNotFoundError, version

try:
    # pyproject.toml 是发行版本的唯一来源；源码 checkout 未安装时保留可诊断值。
    __version__ = version("firmatlas")
except PackageNotFoundError:
    __version__ = "0+unknown"
