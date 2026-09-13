from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kalshi_pbot.config import SETTLE_RARE_TAIL_SECONDS, SETTLE_RECYCLE_SECONDS, Settings
from kalshi_pbot.portfolio import Portfolio
from kalshi_pbot.risk_engine import recycle_ready, seconds_since_close
from kalshi_pbot.runner import PaperBot
from kalshi_pbot.types import Fill, MarketWindow, Outcome


def _closed(
    *,
    now: datetime,
    since: int,
    result: Outcome | None = Outcome.YES,
    expected_horizon: int = 300,
) -> MarketWindow:
    close = now - timedelta(seconds=since)
    return MarketWindow(
        ticker="KXBTC15M-SETTLE",
        event_ticker="KXBTC15M-SETTLE",
        series_ticker="KXBTC15M",
        title="settled",
        status="determined",
        open_time=close - timedelta(minutes=15),
        close_time=close,
        expected_expiration=close + timedelta(seconds=expected_horizon),
        settlement_ts=close + timedelta(seconds=7) if result else None,
        result=result,
    )


def test_recycle_defaults_are_75s_not_expected_expiration() -> None:
    settings = Settings()
    assert settings.settle_recycle_seconds == SETTLE_RECYCLE_SECONDS == 75
    assert settings.effective_settle_recycle_seconds == 75
    rare = Settings(settle_rare_tail=True)
    assert rare.effective_settle_recycle_seconds == SETTLE_RARE_TAIL_SECONDS


def test_recycle_ready_ignores_expected_expiration(now: datetime) -> None:
    close = now - timedelta(seconds=10)
    expired = now - timedelta(seconds=1)
    assert seconds_since_close(close, now) == 10
    assert not recycle_ready(close, 75, now, expected_expiration=expired)
    assert recycle_ready(close - timedelta(seconds=65), 75, now, expected_expiration=expired)


def test_settlement_frees_window_slot(settings: Settings, now: datetime) -> None:
    port = Portfolio(settings)
    port.apply_fill(
        Fill(
            fill_id="1",
            order_id="a",
            market_ticker="KXBTC15M-SETTLE",
            event_ticker="KXBTC15M-SETTLE",
            outcome=Outcome.YES,
            price=Decimal("0.50"),
            count=Decimal("10"),
            fee=Decimal("0"),
            is_taker=False,
            ts_ms=1,
        )
    )
    assert port.snapshot().window_ids == frozenset({"KXBTC15M-SETTLE"})
    close = now - timedelta(seconds=75)
    port.apply_settlement(
        "KXBTC15M-SETTLE",
        Outcome.YES,
        close_time=close,
        now=now,
        settlement_ts=close + timedelta(seconds=7),
    )
    snap = port.snapshot()
    assert snap.window_ids == frozenset()
    assert port.realized_pnl == Decimal("5.00")
    assert port.positions["KXBTC15M-SETTLE"].yes_qty == Decimal("0")


def test_runner_recycles_after_75s_not_five_minutes() -> None:
    settings = Settings(mock=True, dry_run=True, paper_tape=True, series="KXBTC15M")
    bot = PaperBot(settings)
    now = datetime(2026, 9, 13, 21, 0, tzinfo=UTC)
    market = _closed(now=now, since=75)
    bot.universe.settling[market.ticker] = market
    bot.portfolio.apply_fill(
        Fill(
            fill_id="1",
            order_id="a",
            market_ticker=market.ticker,
            event_ticker=market.event_ticker,
            outcome=Outcome.YES,
            price=Decimal("0.40"),
            count=Decimal("10"),
            fee=Decimal("0"),
            is_taker=False,
            ts_ms=1,
        )
    )
    bot.step(now=now)
    assert market.ticker not in bot.universe.settling
    assert bot.portfolio.positions[market.ticker].yes_qty == Decimal("0")


def test_rare_tail_holds_recycle_until_300s() -> None:
    settings = Settings(
        mock=True,
        dry_run=True,
        paper_tape=True,
        series="KXBTC15M",
        settle_rare_tail=True,
    )
    bot = PaperBot(settings)
    now = datetime(2026, 9, 13, 21, 0, tzinfo=UTC)
    early = _closed(now=now, since=75)
    bot.universe.settling[early.ticker] = early
    bot.step(now=now)
    assert early.ticker in bot.universe.settling
    later = now + timedelta(seconds=230)
    bot.step(now=later)
    assert early.ticker not in bot.universe.settling
