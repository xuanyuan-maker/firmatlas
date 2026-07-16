"""应用生成的不透明 TEXT ID（README 0x0B 通用规则）。

采用标准库 UUIDv4：README 只要求 ID 不透明且唯一，按时间排序的需求
由各表 created_at 列满足，因此不引入 UUIDv7 实现。
"""

import uuid

# 归档文件名前缀取的 ID 长度（README 0x0D：a19f23cd__firmware.bin）
SHORT_ID_LENGTH = 8


def new_id() -> str:
    """生成 32 位十六进制小写 ID，如 'a19f23cd0b6e4f2c9d18e37a5c40b912'。"""
    return uuid.uuid4().hex


def short_id(entity_id: str) -> str:
    """取 ID 前 8 位，用作归档文件名前缀。"""
    return entity_id[:SHORT_ID_LENGTH]
