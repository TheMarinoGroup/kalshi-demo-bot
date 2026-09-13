from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_pbot.config import DEMO_REST, DEMO_WS, PROD_REST, PROD_WS, Settings
from kalshi_pbot.execution import build_order_body
from kalshi_pbot.types import Liquidity, Outcome, QuoteIntent, TimeInForce


def test_defaults_are_paper_data_plane() -> None:
    settings = Settings()
    assert settings.env == "demo"
    assert settings.dry_run is True
    assert settings.paper_tape is True
    assert settings.live_submit is False
    assert settings.resolved_data_rest == PROD_REST
    assert settings.resolved_rest_base == PROD_REST
    assert settings.resolved_order_rest == DEMO_REST
    assert settings.resolved_ws_url == DEMO_WS
    assert settings.latency_ms == 150


def test_production_trading_refused_without_override() -> None:
    with pytest.raises(ValueError, match="Production \\*trading\\* is disabled"):
        Settings(env="production")


def test_prod_data_rest_ok_without_allow_production() -> None:
    settings = Settings(rest_base=PROD_REST)
    assert settings.resolved_data_rest == PROD_REST


def test_prod_order_rest_refused() -> None:
    with pytest.raises(ValueError, match="production order host"):
        Settings(order_rest=PROD_REST)


def test_prod_ws_refused_without_flag() -> None:
    with pytest.raises(ValueError, match="production WebSocket"):
        Settings(ws_url=PROD_WS)


def test_prod_ws_allowed_readonly() -> None:
    settings = Settings(ws_url=PROD_WS, allow_prod_ws=True)
    assert settings.resolved_ws_url == PROD_WS
    assert settings.live_submit is False


def test_latency_must_be_bucket() -> None:
    with pytest.raises(ValueError, match="latency_ms"):
        Settings(latency_ms=100)
    assert Settings(latency_ms=50).latency_ms == 50
    assert Settings(latency_ms=500).latency_ms == 500


def test_clip_clamped() -> None:
    assert Settings(clip_dollars=Decimal("5")).clip == Decimal("10")
    assert Settings(clip_dollars=Decimal("99")).clip == Decimal("30")
    assert Settings(clip_dollars=Decimal("20")).clip == Decimal("20")


def test_execution_yes_leg_mapping() -> None:
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
