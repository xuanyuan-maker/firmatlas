"""UTC RFC 3339 时间工具测试。"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from firmatlas.domain import timeutil


def test_utc_now_is_aware_utc_with_second_precision():
    now = timeutil.utc_now()
    assert now.tzinfo == UTC
    assert now.microsecond == 0


def test_format_produces_z_suffixed_text():
    value = datetime(2026, 7, 16, 8, 30, 5, tzinfo=UTC)
    assert timeutil.format_rfc3339(value) == "2026-07-16T08:30:05Z"


def test_format_converts_other_timezone_to_utc():
    beijing = timezone(timedelta(hours=8))
    value = datetime(2026, 7, 16, 16, 30, 5, tzinfo=beijing)
    assert timeutil.format_rfc3339(value) == "2026-07-16T08:30:05Z"


def test_format_rejects_naive_datetime():
    with pytest.raises(ValueError, match="无时区"):
        timeutil.format_rfc3339(datetime(2026, 7, 16, 8, 30, 5))


def test_parse_returns_aware_utc():
    value = timeutil.parse_rfc3339("2026-07-16T08:30:05Z")
    assert value == datetime(2026, 7, 16, 8, 30, 5, tzinfo=UTC)
    assert value.tzinfo == UTC


def test_parse_rejects_text_without_timezone():
    with pytest.raises(ValueError, match="缺少时区"):
        timeutil.parse_rfc3339("2026-07-16T08:30:05")


def test_round_trip_is_lossless():
    now = timeutil.utc_now()
    assert timeutil.parse_rfc3339(timeutil.format_rfc3339(now)) == now
