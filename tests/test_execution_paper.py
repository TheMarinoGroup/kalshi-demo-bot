from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_pbot.config import DEMO_REST, PROD_REST, Settings
from kalshi_pbot.execution import ExecutionEngine
from kalshi_pbot.kalshi_client import KalshiRestClient
from kalshi_pbot.portfolio import Portfolio
from kalshi_pbot.types import (
    Fill,
    IntentKind,
    Liquidity,
    OrderBook,
    Outcome,
    PriceLevel,
    QuoteIntent,
)


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
    with pytest.raises(RuntimeError, match="order-write REST"):
        client.create_order({"ticker": "X"})


def test_create_order_blocked_on_prod_without_allow_production() -> None:
    settings = Settings(
        ws_env="production",
        allow_prod_ws=True,
        paper_tape=False,
        dry_run=False,
    )
    assert settings.resolved_order_rest == DEMO_REST
    read = KalshiRestClient(
        settings, base_url=settings.resolved_portfolio_rest, purpose="portfolio"
    )
    with pytest.raises(RuntimeError, match="order-write REST"):
        read.create_order({"ticker": "X"})
    order = KalshiRestClient(settings, base_url=PROD_REST, purpose="order")
    with pytest.raises(RuntimeError, match="ALLOW_PRODUCTION"):
        order.create_order({"ticker": "X"})


def test_submit_rejects_when_projected_open_exceeds_cap() -> None:
    settings = Settings(paper_tape=True, dry_run=True, mock=True)
    port = Portfolio(settings)
    port.apply_fill(
        Fill(
            fill_id="1",
            order_id="a",
            market_ticker="KXBTC15M-T",
            event_ticker="KXBTC15M-T",
            outcome=Outcome.YES,
            price=Decimal("0.50"),
            count=Decimal("40"),
            fee=Decimal("0"),
            is_taker=False,
            ts_ms=1,
        )
    )
    engine = ExecutionEngine(settings, port, rest=ForbiddenRest())  # type: ignore[arg-type]
    fat = QuoteIntent(
        market_ticker="KXBTC15M-T",
        event_ticker="KXBTC15M-T",
        outcome=Outcome.NO,
        price=Decimal("0.70"),
        count=Decimal("40"),
        liquidity=Liquidity.MAKER,
        kind=IntentKind.COMPLETE_PAIR,
    )
    assert fat.notional + Decimal("20") > settings.max_open_notional
    result = engine.submit(fat)
    assert result["rejected"] == "open_notional"
    assert engine.matcher.orders == {}
    assert port.snapshot().open_notional == Decimal("20")
