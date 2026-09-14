from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from kalshi_pbot.config import Settings
from kalshi_pbot.portfolio import Portfolio
from kalshi_pbot.types import Fill, Outcome


def test_pair_fill_realizes_locked_pnl(settings: Settings) -> None:
    port = Portfolio(settings)
    port.apply_fill(
        Fill(
            fill_id="1",
            order_id="a",
            market_ticker="T",
            event_ticker="E",
            outcome=Outcome.YES,
            price=Decimal("0.48"),
            count=Decimal("10"),
            fee=Decimal("0"),
            is_taker=False,
            ts_ms=1,
        )
    )
    assert port.positions["T"].unpaired_qty == Decimal("10")
    port.apply_fill(
        Fill(
            fill_id="2",
            order_id="b",
            market_ticker="T",
            event_ticker="E",
            outcome=Outcome.NO,
            price=Decimal("0.49"),
            count=Decimal("10"),
            fee=Decimal("0.01"),
            is_taker=False,
            ts_ms=2,
        )
    )
    pos = port.positions["T"]
    assert pos.unpaired_qty == Decimal("0")
    assert port.realized_pnl == Decimal("0.30")  # 10 * (1 - 0.48 - 0.49)
    assert port.fees == Decimal("0.01")
    snap = port.snapshot()
    assert snap.daily_pnl == Decimal("0.29")
    assert snap.unpaired_notional == Decimal("0")
    assert pos.unpaired_since is None


def test_unpaired_since_set_on_first_fill_and_cleared_on_pair(settings: Settings) -> None:
    port = Portfolio(settings)
    port.apply_fill(
        Fill(
            fill_id="1",
            order_id="a",
            market_ticker="T",
            event_ticker="E",
            outcome=Outcome.YES,
            price=Decimal("0.48"),
            count=Decimal("10"),
            fee=Decimal("0"),
            is_taker=False,
            ts_ms=1_700_000_000_000,
        )
    )
    pos = port.positions["T"]
    assert pos.unpaired_qty == Decimal("10")
    assert pos.unpaired_since == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    first = pos.unpaired_since
    port.apply_fill(
        Fill(
            fill_id="2",
            order_id="b",
            market_ticker="T",
            event_ticker="E",
            outcome=Outcome.YES,
            price=Decimal("0.49"),
            count=Decimal("5"),
            fee=Decimal("0"),
            is_taker=False,
            ts_ms=1_700_000_100_000,
        )
    )
    assert port.positions["T"].unpaired_since == first
    port.apply_fill(
        Fill(
            fill_id="3",
            order_id="c",
            market_ticker="T",
            event_ticker="E",
            outcome=Outcome.NO,
            price=Decimal("0.50"),
            count=Decimal("15"),
            fee=Decimal("0"),
            is_taker=False,
            ts_ms=1_700_000_200_000,
        )
    )
    assert port.positions["T"].unpaired_qty == Decimal("0")
    assert port.positions["T"].unpaired_since is None


def test_sell_yes_maps_to_ask() -> None:
    from kalshi_pbot.execution import build_order_body, flatten_intent
    from kalshi_pbot.types import Outcome

    intent = flatten_intent("T", "E", Outcome.YES, Decimal("10"), Decimal("0.47"))
    body = build_order_body(intent)
    assert intent.sell is True
    assert body["side"] == "ask"
    assert body["price"] == "0.4700"
    assert body["reduce_only"] is True
    assert body["post_only"] is False
