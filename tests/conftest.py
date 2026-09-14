from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_pbot.config import Settings
from kalshi_pbot.portfolio import Portfolio
from kalshi_pbot.types import (
    MarketWindow,
    OrderBook,
    PortfolioSnapshot,
    Position,
    PriceLevel,
)


@pytest.fixture(autouse=True)
def _research_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KALSHI_WINDOWS_PATH", str(tmp_path / "windows.json"))
    monkeypatch.setenv("KALSHI_TAPE_PATH", str(tmp_path / "tape.jsonl"))
    monkeypatch.setenv("KALSHI_KILL_LATCH_PATH", str(tmp_path / "kill-latch.json"))


@pytest.fixture
def settings() -> Settings:
    return Settings(
        bankroll=Decimal("1000"),
        dry_run=True,
        mock=True,
        paper_tape=True,
        max_windows=2,
    )


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 13, 20, 0, tzinfo=UTC)


@pytest.fixture
def market(now: datetime) -> MarketWindow:
    return MarketWindow(
        ticker="KXBTC15M-MOCK",
        event_ticker="KXBTC15M-MOCK",
        series_ticker="KXBTC15M",
        title="BTC 15m Up/Down",
        status="active",
        open_time=now - timedelta(minutes=5),
        close_time=now + timedelta(minutes=10),
    )


@pytest.fixture
def closing_market(now: datetime) -> MarketWindow:
    return MarketWindow(
        ticker="KXBTC15M-CLOSE",
        event_ticker="KXBTC15M-CLOSE",
        series_ticker="KXBTC15M",
        title="BTC 15m closing",
        status="active",
        open_time=now - timedelta(minutes=14),
        close_time=now + timedelta(seconds=30),
    )


def book(yes_bid: str = "0.4800", no_bid: str = "0.4900") -> OrderBook:
    return OrderBook(
        ticker="KXBTC15M-MOCK",
        yes_bids=[PriceLevel(Decimal(yes_bid), Decimal("40"))],
        no_bids=[PriceLevel(Decimal(no_bid), Decimal("25"))],
    )


def empty_snapshot(settings: Settings, **kwargs: object) -> PortfolioSnapshot:
    portfolio = Portfolio(settings)
    snap = portfolio.snapshot()
    if not kwargs:
        return snap
    return PortfolioSnapshot(
        bankroll=kwargs.get("bankroll", snap.bankroll),  # type: ignore[arg-type]
        realized_pnl=kwargs.get("realized_pnl", snap.realized_pnl),  # type: ignore[arg-type]
        unrealized_pnl=kwargs.get("unrealized_pnl", snap.unrealized_pnl),  # type: ignore[arg-type]
        fees=kwargs.get("fees", snap.fees),  # type: ignore[arg-type]
        daily_pnl=kwargs.get("daily_pnl", snap.daily_pnl),  # type: ignore[arg-type]
        open_notional=kwargs.get("open_notional", snap.open_notional),  # type: ignore[arg-type]
        unpaired_notional=kwargs.get("unpaired_notional", snap.unpaired_notional),  # type: ignore[arg-type]
        window_ids=kwargs.get("window_ids", snap.window_ids),  # type: ignore[arg-type]
        positions=kwargs.get("positions", snap.positions),  # type: ignore[arg-type]
        resting=kwargs.get("resting", snap.resting),  # type: ignore[arg-type]
        kill_active=kwargs.get("kill_active", snap.kill_active),  # type: ignore[arg-type]
        kill_reason=kwargs.get("kill_reason", snap.kill_reason),  # type: ignore[arg-type]
    )


def yes_position(
    ticker: str = "KXBTC15M-MOCK",
    qty: str = "40",
    px: str = "0.50",
    *,
    unpaired_since: datetime | None = None,
) -> Position:
    q = Decimal(qty)
    p = Decimal(px)
    return Position(
        market_ticker=ticker,
        event_ticker=ticker,
        yes_qty=q,
        yes_cost=q * p,
        unpaired_since=unpaired_since,
    )
