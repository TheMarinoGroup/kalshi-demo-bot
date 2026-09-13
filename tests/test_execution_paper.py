from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_pbot.config import Settings
from kalshi_pbot.execution import ExecutionEngine
from kalshi_pbot.kalshi_client import KalshiRestClient
from kalshi_pbot.portfolio import Portfolio
from kalshi_pbot.types import Liquidity, OrderBook, Outcome, PriceLevel, QuoteIntent


class ForbiddenRest:
    def create_order(self, body: dict) -> None:
        raise AssertionError(f"POST /portfolio/events/orders must not run: {body}")


def _intent() -> QuoteIntent:
    return QuoteIntent(
        market_ticker="KXBTC15M-T",
        event_ticker="KXBTC15M-T",
        outcome=Outcome.YES,
        price=Decimal("0.48"),
        count=Decimal("10"),
        liquidity=Liquidity.MAKER,
    )


def test_paper_tape_never_posts() -> None:
    settings = Settings(paper_tape=True, dry_run=True)
    engine = ExecutionEngine(settings, Portfolio(settings), rest=ForbiddenRest())  # type: ignore[arg-type]
    book = OrderBook(
        ticker="KXBTC15M-T",
        yes_bids=[PriceLevel(Decimal("0.48"), Decimal("20"))],
    )
    result = engine.submit(_intent(), book=book)
    assert result["dry_run"] is True
    assert engine.matcher.orders
    assert engine.dry_run_orders


def test_create_order_raises_in_paper_tape() -> None:
    settings = Settings(paper_tape=True, dry_run=True)
    client = KalshiRestClient(settings, purpose="order")
    with pytest.raises(RuntimeError, match="POST /portfolio/events/orders is disabled"):
        client.create_order({"ticker": "X"})


def test_create_order_raises_on_data_client() -> None:
    settings = Settings(paper_tape=False, dry_run=False)
    client = KalshiRestClient(settings, purpose="data")
    with pytest.raises(RuntimeError, match="demo order REST"):
        client.create_order({"ticker": "X"})
