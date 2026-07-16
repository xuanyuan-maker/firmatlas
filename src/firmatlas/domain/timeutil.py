"""UTC RFC 3339 时间文本与 datetime 的互转（README 0x0B 通用规则）。

约定：
- 数据库中的时间一律为 'YYYY-MM-DDTHH:MM:SSZ'，精确到秒；
- Python 侧一律使用带 UTC 时区的 datetime，禁止"裸"时间进入存储路径。
"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """当前 UTC 时间，截断到秒（与存储精度一致，往返转换不丢信息）。"""
    return datetime.now(UTC).replace(microsecond=0)


def format_rfc3339(value: datetime) -> str:
    """datetime → 'YYYY-MM-DDTHH:MM:SSZ'。

    只接受带时区的 datetime；裸时间无法判断属于哪个时区，直接报错
    比猜测后存错更安全。非 UTC 时区会先换算成 UTC。
    """
    if value.tzinfo is None:
        raise ValueError(f"拒绝无时区信息的时间：{value!r}，请使用 timeutil.utc_now() 生成")
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc3339(text: str) -> datetime:
    """'YYYY-MM-DDTHH:MM:SSZ' → 带 UTC 时区的 datetime。"""
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError(f"时间文本缺少时区信息：{text!r}")
    return value.astimezone(UTC)
