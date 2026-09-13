from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshi_pbot.config import Settings
from kalshi_pbot.series import (
    is_supported_series,
    is_supported_window,
    series_duration_minutes,
)
from kalshi_pbot.types import MarketWindow


def test_parses_15m_and_longer() -> None:
    assert series_duration_minutes("KXBTC15M") == 15
    assert series_duration_minutes("KXETH15M") == 15
    assert series_duration_minutes("KXBTC1H") == 60
    assert series_duration_minutes("KXBTC4H") == 240
    assert is_supported_series("KXBTC15M")
    assert is_supported_series("KXBTC1H")


def test_rejects_five_minute_series() -> None:
    assert series_duration_minutes("KXBTC5M") == 5
    assert not is_supported_series("KXBTC5M")
    assert not is_supported_series("KXETH5M")


def test_settings_drops_short_series() -> None:
    settings = Settings(series="KXBTC5M,KXBTC15M")
    assert settings.series_tickers == ("KXBTC15M",)


def test_cannot_lower_min_window_below_15() -> None:
    with pytest.raises(ValueError, match="15m"):
        Settings(min_window_minutes=5)


def test_window_duration_gate() -> None:
    now = datetime(2026, 9, 13, 21, 0, tzinfo=UTC)
    short = MarketWindow(
        ticker="KXBTC5M-X",
        event_ticker="KXBTC5M-X",
        series_ticker="KXBTC5M",
        title="nope",
        status="active",
        open_time=now,
        close_time=now + timedelta(minutes=5),
    )
    ok = MarketWindow(
        ticker="KXBTC15M-X",
        event_ticker="KXBTC15M-X",
        series_ticker="KXBTC15M",
        title="ok",
        status="active",
        open_time=now,
        close_time=now + timedelta(minutes=15),
    )
    assert not is_supported_window(short)
    assert is_supported_window(ok)
