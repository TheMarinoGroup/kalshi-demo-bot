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
    assert settings.discover_seconds == 15.0
    assert settings.discover_series_delay == 0.4
    assert settings.effective_settle_recycle_seconds == 75
    assert settings.settle_rare_tail is False
    assert settings.max_windows == 2
    assert settings.max_unpaired_age_seconds == 45
    assert settings.soft_onesided == Decimal("10")
    assert settings.last_seconds == 120
    assert settings.min_edge == Decimal("0.04")
    assert settings.quote_mode == "one_sided"
    assert settings.improve_ticks == 0
    assert settings.taker_pair_arb is False
    assert settings.only_quote_underround is False


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


def test_unpaired_age_and_windows_validation() -> None:
    assert Settings(max_unpaired_age_seconds=0).max_unpaired_age_seconds == 0
    with pytest.raises(ValueError, match="max_unpaired_age_seconds"):
        Settings(max_unpaired_age_seconds=-1)
    with pytest.raises(ValueError, match="soft_onesided"):
        Settings(soft_onesided=Decimal("-1"))
    with pytest.raises(ValueError, match="last_seconds"):
        Settings(last_seconds=59)
    with pytest.raises(ValueError, match="max_windows"):
        Settings(max_windows=0)
    assert Settings(max_windows=1, series="KXBTC15M").max_windows == 1
    assert Settings(last_seconds=60).last_seconds == 60


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
