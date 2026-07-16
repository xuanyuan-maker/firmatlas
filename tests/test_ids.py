"""ID 生成工具测试。"""

from firmatlas.domain import ids


def test_new_id_is_32_hex_chars():
    value = ids.new_id()
    assert len(value) == 32
    assert value == value.lower()
    int(value, 16)  # 全部为十六进制字符，否则抛 ValueError


def test_new_id_is_unique_across_calls():
    values = {ids.new_id() for _ in range(1000)}
    assert len(values) == 1000


def test_short_id_takes_first_eight_chars():
    assert ids.short_id("a19f23cd0b6e4f2c9d18e37a5c40b912") == "a19f23cd"
