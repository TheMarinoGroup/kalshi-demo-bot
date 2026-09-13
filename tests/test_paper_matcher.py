from __future__ import annotations

from decimal import Decimal

from kalshi_pbot.paper_matcher import PaperMatcher
from kalshi_pbot.types import Liquidity, OrderBook, Outcome, PriceLevel, PublicTrade, QuoteIntent


def _book(yes_bid: str = "0.48", yes_sz: str = "10") -> OrderBook:
    return OrderBook(
        ticker="KXBTC15M-T",
        yes_bids=[PriceLevel(Decimal(yes_bid), Decimal(yes_sz))],
        no_bids=[PriceLevel(Decimal("0.50"), Decimal("8"))],
    )


def _intent(price: str = "0.48", count: str = "5", oid: str = "p1") -> QuoteIntent:
    return QuoteIntent(
        market_ticker="KXBTC15M-T",
        event_ticker="KXBTC15M-T",
        outcome=Outcome.YES,
        price=Decimal(price),
        count=Decimal(count),
        liquidity=Liquidity.MAKER,
        client_order_id=oid,
    )


def test_joins_back_of_book() -> None:
    matcher = PaperMatcher(latency_ms=50)
    resting = matcher.place(_intent(), _book(yes_sz="40"), now_ms=0)
    assert resting.queue_ahead == Decimal("40")
    assert resting.visible_at_ms == 50


def test_trade_at_level_fills_after_queue() -> None:
    matcher = PaperMatcher(latency_ms=50)
    matcher.place(_intent(count="5"), _book(yes_sz="10"), now_ms=0)
    matcher.enqueue_trade(
        PublicTrade(
            trade_id="t1",
            market_ticker="KXBTC15M-T",
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.52"),
            count=Decimal("12"),
            taker_outcome=Outcome.NO,
            ts_ms=100,
        )
    )
    fills = matcher.drain(200)
    assert len(fills) == 1
    assert fills[0].count == Decimal("2")
    assert fills[0].reason == "trade_at_level"


def test_through_print_sweeps_queue() -> None:
    matcher = PaperMatcher(latency_ms=50)
    matcher.place(_intent(count="5"), _book(yes_sz="10"), now_ms=0)
    matcher.enqueue_trade(
        PublicTrade(
            trade_id="t2",
            market_ticker="KXBTC15M-T",
            yes_price=Decimal("0.47"),
            no_price=Decimal("0.53"),
            count=Decimal("1"),
            taker_outcome=Outcome.NO,
            ts_ms=100,
        )
    )
    fills = matcher.drain(200)
    assert len(fills) == 1
    assert fills[0].count == Decimal("5")
    assert fills[0].reason == "trade_at_or_through"


def test_ambiguous_wipe_never_fills() -> None:
    matcher = PaperMatcher(latency_ms=50)
    matcher.place(_intent(count="5"), _book(yes_sz="10"), now_ms=0)
    matcher.enqueue_size_change(
        ticker="KXBTC15M-T",
        outcome=Outcome.YES,
        price=Decimal("0.48"),
        old_size=Decimal("10"),
        new_size=Decimal("2"),
        ts_ms=100,
    )
    fills = matcher.drain(200)
    assert fills == []
    assert matcher.ambiguous_wipes == 1
    remaining = next(iter(matcher.orders.values()))
    assert remaining.remaining == Decimal("5")
    assert remaining.queue_ahead == Decimal("2")


def test_level_wipe_to_zero_cancels_without_fill() -> None:
    matcher = PaperMatcher(latency_ms=50)
    matcher.place(_intent(count="5"), _book(yes_sz="0"), now_ms=0)
    matcher.enqueue_size_change(
        ticker="KXBTC15M-T",
        outcome=Outcome.YES,
        price=Decimal("0.48"),
        old_size=Decimal("5"),
        new_size=Decimal("0"),
        ts_ms=100,
    )
    assert matcher.drain(200) == []
    assert matcher.orders == {}
    assert "p1" in matcher.cancelled


def test_latency_delays_application() -> None:
    matcher = PaperMatcher(latency_ms=150)
    matcher.place(_intent(count="5"), _book(yes_sz="0"), now_ms=0)
    matcher.enqueue_trade(
        PublicTrade(
            trade_id="t3",
            market_ticker="KXBTC15M-T",
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.52"),
            count=Decimal("5"),
            taker_outcome=Outcome.NO,
            ts_ms=0,
        )
    )
    assert matcher.drain(100) == []
    fills = matcher.drain(150)
    assert len(fills) == 1
    assert fills[0].latency_ms == 150
