from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_pbot.config import DEMO_REST, DEMO_WS, Settings


def test_defaults_are_demo() -> None:
    settings = Settings()
    assert settings.env == "demo"
    assert settings.dry_run is True
    assert settings.resolved_rest_base == DEMO_REST
    assert settings.resolved_ws_url == DEMO_WS
    assert "demo" in settings.resolved_rest_base


def test_production_refused_without_override() -> None:
    with pytest.raises(ValueError, match="Production trading is disabled"):
        Settings(env="production")


def test_production_url_override_refused() -> None:
    with pytest.raises(ValueError, match="Refusing production host"):
        Settings(rest_base="https://external-api.kalshi.com/trade-api/v2")


def test_clip_clamped() -> None:
    assert Settings(clip_dollars=Decimal("5")).clip == Decimal("10")
    assert Settings(clip_dollars=Decimal("99")).clip == Decimal("30")
    assert Settings(clip_dollars=Decimal("20")).clip == Decimal("20")


def test_execution_yes_leg_mapping() -> None:
    from kalshi_pbot.execution import build_order_body
    from kalshi_pbot.types import Liquidity, Outcome, QuoteIntent, TimeInForce

    buy_no = QuoteIntent(
        market_ticker="T",
        event_ticker="T",
        outcome=Outcome.NO,
        price=Decimal("0.4000"),
        count=Decimal("10"),
        liquidity=Liquidity.MAKER,
        tif=TimeInForce.GTC,
        post_only=True,
    )
    body = build_order_body(buy_no)
    assert body["side"] == "ask"
    assert body["price"] == "0.6000"
    assert body["post_only"] is True
    assert "client_order_id" in body
