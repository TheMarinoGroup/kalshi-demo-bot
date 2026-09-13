"""Kalshi crypto Up/Down window length.

This desk is **15 minutes or greater only** (KXBTC15M, KXETH15M, and
later 1H/4H/1D clips). Polymarket-style 5-minute markets are rejected.
"""

from __future__ import annotations

import re
from datetime import datetime

from kalshi_pbot.types import MarketWindow

MIN_WINDOW_MINUTES = 15

# KXBTC15M, KXETH15M, KXBTC1H, KXBTC4H, KXBTC1D, …
_SERIES_WINDOW = re.compile(r"(\d+)([MHD])$", re.IGNORECASE)


def series_duration_minutes(series_ticker: str) -> int | None:
    match = _SERIES_WINDOW.search((series_ticker or "").strip().upper())
    if not match:
        return None
    count = int(match.group(1))
    unit = match.group(2).upper()
    if unit == "M":
        return count
    if unit == "H":
        return count * 60
    return count * 1440


def is_supported_series(series_ticker: str, min_minutes: int = MIN_WINDOW_MINUTES) -> bool:
    minutes = series_duration_minutes(series_ticker)
    if minutes is None:
        return False
    return minutes >= min_minutes


def window_duration_minutes(market: MarketWindow) -> float:
    return (market.close_time - market.open_time).total_seconds() / 60.0


def is_supported_window(
    market: MarketWindow,
    min_minutes: int = MIN_WINDOW_MINUTES,
    *,
    now: datetime | None = None,
) -> bool:
    del now
    if not is_supported_series(market.series_ticker, min_minutes):
        return False
    return window_duration_minutes(market) + 0.05 >= min_minutes


def filter_series(tickers: list[str], min_minutes: int = MIN_WINDOW_MINUTES) -> tuple[str, ...]:
    return tuple(t for t in tickers if is_supported_series(t, min_minutes))
