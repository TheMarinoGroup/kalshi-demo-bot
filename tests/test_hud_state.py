from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from kalshi_pbot.config import CLIP_MAX, CLIP_MIN, Settings
from kalshi_pbot.hud_server import HudHub, create_app
from kalshi_pbot.hud_state import MidHistory, _book_depth, _mode_block, build_snapshot
from kalshi_pbot.risk_engine import classify_kill
from kalshi_pbot.runner import PaperBot
from kalshi_pbot.types import Fill, MarketWindow, Outcome, RestingOrder
from tests.conftest import book


def _fill(
    *,
    ticker: str = "KXBTC15M-MOCK",
    outcome: Outcome = Outcome.YES,
    price: str = "0.50",
    count: str = "20",
    fee: str = "0",
    taker: bool = False,
    fill_id: str = "1",
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=fill_id,
        market_ticker=ticker,
        event_ticker=ticker,
        outcome=outcome,
        price=Decimal(price),
        count=Decimal(count),
        fee=Decimal(fee),
        is_taker=taker,
        ts_ms=1,
    )


def test_snapshot_must_show_panels_from_mock_bot() -> None:
    bot = PaperBot(Settings(mock=True, dry_run=True, paper_tape=True, series="KXBTC15M"))
    bot.universe.refresh()
    bot.universe.hydrate_books(bot.books)
    bot.step()
    snap = build_snapshot(bot, MidHistory())

    assert snap["mode"]["badge"] == "PAPER"
    assert snap["mode"]["paper_only"] is True
    assert snap["mode"]["hard_stop"] is False
    assert snap["mode"]["live_submit"] is False
    assert snap["risk"]["bankroll"] == 500
    assert snap["risk"]["clip"] == 10
    assert snap["risk"]["clip_min"] == float(CLIP_MIN)
    assert snap["risk"]["clip_max"] == float(CLIP_MAX)
    assert snap["risk"]["max_open"] == 25
    assert snap["risk"]["max_onesided"] == 15
    assert snap["risk"]["daily_kill"] == 10
    assert snap["risk"]["max_windows"] == 1
    assert snap["risk"]["unsettled_until"] == "settlement_ts"
    assert snap["risk"]["settle_band"] == [60, 90]
    assert snap["fees"]["maker_pending_confirm"] is True
    assert snap["fees"]["maker"] is None
    assert snap["settle"]["not_expected_expiration"] is True
    assert snap["kill"]["state"] == "ARMED"
    assert snap["kill"]["active"] is False
    assert snap["reconcile"]["ready_to_trade"] is True
    assert snap["reconcile"]["status"] == "READY"
    assert snap["reconcile"]["book_verified"] is True
    assert snap["reconcile"]["hard_hold"] is False
    assert snap["gate"]["ready_to_trade"] is True
    assert set(snap["util"]) == {"fill", "open", "windows", "onesided", "daily_loss"}
    assert snap["util"]["open"]["label"] == "util_open"
    assert snap["util"]["onesided"]["label"] == "util_onesided"
    assert snap["util"]["windows"]["label"] == "util_windows"
    assert "max_daily_notional" not in snap
    assert "max_daily_notional" not in snap["risk"]
    live = [w for w in snap["windows"] if w["live"]]
    assert live
    win = live[0]
    assert win["ttc_zone"] in {"GREEN", "AMBER", "RED"}
    assert win["floor_strike"] == 65000
    assert win["bid_sum"] == 0.95  # 0.47 + 0.48 mock underround book
    assert win["ask_sum"] == 1.05
    assert win["underround"] is True  # 0.95 ≤ 1 − 0.04 paper-v2 min_edge
    assert win["arb_taker_eligible"] is False
    assert win["arb"] is False  # must not flash green ARB for underround
    assert win["yes_bid_sz"] == 40
    assert win["cfb"]["lag_ms"] is None
    assert win["cfb"]["oracle"] is False
    assert win["cfb"]["label"] == "chart≠settle"
    assert win["capital_free_at"]
    assert all(w["series"].endswith("15M") or "H" in w["series"] for w in snap["windows"])


def test_mode_live_without_approval_is_hard_stop() -> None:
    from types import SimpleNamespace

    block = _mode_block(
        SimpleNamespace(  # type: ignore[arg-type]
            env="production",
            dry_run=False,
            paper_tape=False,
            live_submit=True,
            allow_production=False,
            mock=False,
            latency_ms=150,
        )
    )
    assert block["badge"] == "LIVE"
    assert block["hard_stop"] is True
    assert block["paper_only"] is False
    paper = _mode_block(Settings(mock=True, dry_run=True, paper_tape=True))
    assert paper["badge"] == "PAPER"
    assert paper["hard_stop"] is False


def test_last_fill_clip_and_fee_split(settings: Settings) -> None:
    bot = PaperBot(settings)
    bot.portfolio.apply_fill(
        _fill(count="40", fee="0", taker=False, fill_id="m")
    )  # $20 maker
    bot.portfolio.apply_fill(
        _fill(
            ticker="KXETH15M-MOCK",
            count="10",
            price="0.40",
            fee="0.0175",
            taker=True,
            fill_id="t",
        )
    )
    snap = build_snapshot(bot, MidHistory())
    assert snap["risk"]["last_fill"]["notional"] == 4.0
    assert snap["risk"]["last_fill"]["liquidity"] == "taker"
    assert snap["fees"]["maker"] is None
    assert snap["fees"]["taker"] == 0.0175
    assert snap["fees"]["today"] == 0.0175
    assert snap["util"]["fill"]["value"] == 4.0
    assert snap["util"]["fill"]["max"] == 30
    assert snap["extras"]["mix"]["BTC"] == 20.0
    assert snap["extras"]["mix"]["ETH"] == 4.0
    assert snap["extras"]["maker_fills"] == 1
    assert snap["extras"]["taker_fills"] == 1


def test_session_pnl_curve_and_tape_notional(settings: Settings) -> None:
    bot = PaperBot(settings)
    hist = MidHistory()
    first = build_snapshot(bot, hist)
    assert first["pnl"]["curve"]
    assert first["pnl"]["curve"][0]["fill_count"] == 0
    assert first["pnl"]["curve"][0]["daily"] == 0
    bot.portfolio.apply_fill(_fill(count="20", price="0.40", fill_id="curve"))
    second = build_snapshot(bot, hist)
    assert len(second["pnl"]["curve"]) >= 2
    assert second["pnl"]["curve"][-1]["fill_count"] == 1
    assert second["fills"][-1]["notional"] == 8.0
    assert second["fills"][-1]["ticker"] == "KXBTC15M-MOCK"


def test_daily_pnl_includes_unsettled_until_settlement(settings: Settings) -> None:
    bot = PaperBot(settings)
    bot.portfolio.apply_fill(_fill(count="20", price="0.60"))  # $12 cost, unpaired
    snap = build_snapshot(bot, MidHistory())
    assert snap["risk"]["unsettled_pnl"] == snap["pnl"]["unrealized"]
    assert snap["pnl"]["daily"] == snap["risk"]["daily_pnl"]
    # Unsettled inventory still occupies the window and counts toward daily.
    assert snap["risk"]["windows"] == 1
    assert snap["risk"]["unpaired"] == 12.0


def test_onesided_panel_names_leg_and_ticker(settings: Settings) -> None:
    bot = PaperBot(settings)
    bot.portfolio.apply_fill(_fill(outcome=Outcome.NO, count="40", price="0.50"))
    snap = build_snapshot(bot, MidHistory())
    assert snap["risk"]["onesided_leg"] == "no"
    assert snap["risk"]["onesided_ticker"] == "KXBTC15M-MOCK"
    assert snap["risk"]["abort_unpaired"] is True
    assert snap["util"]["onesided"]["value"] == 20.0
    assert snap["util"]["onesided"]["tone"] == "green"


def test_util_tones_amber_at_80_red_at_cap(settings: Settings) -> None:
    bot = PaperBot(settings)
    bot.portfolio.apply_fill(_fill(count="80", price="0.50"))  # $40 / $50 = 80%
    snap = build_snapshot(bot, MidHistory())
    assert snap["util"]["open"]["tone"] == "amber"
    assert 0.79 < snap["util"]["open"]["util"] < 0.81
    bot.portfolio.apply_fill(_fill(count="20", price="0.50", fill_id="2"))  # +$10 = $50
    snap = build_snapshot(bot, MidHistory())
    assert snap["risk"]["open_notional"] == 50
    assert snap["util"]["open"]["tone"] == "red"


def test_last_60s_gate_and_violation(settings: Settings, now: datetime) -> None:
    bot = PaperBot(settings)
    close = now + timedelta(seconds=20)
    market = MarketWindow(
        ticker="KXBTC15M-GATE",
        event_ticker="KXBTC15M-GATE",
        series_ticker="KXBTC15M",
        title="gate",
        status="active",
        open_time=now - timedelta(minutes=14),
        close_time=close,
    )
    bot.universe.markets[market.ticker] = market
    snap = build_snapshot(bot, MidHistory(), now)
    gate = snap["gate"]["windows"][0]
    assert gate["last_60s"] is True
    assert gate["new_risk_allowed"] is False
    assert snap["gate"]["new_risk_allowed"] is False
    assert snap["gate"]["violation"] is False
    assert snap["gate"]["last60s_lock"] is True
    assert snap["gate"]["no_new_risk"] is True
    assert snap["gate"]["windows"][0]["ttc_zone"] == "RED"

    bot.portfolio.upsert_resting(
        RestingOrder(
            order_id="rest",
            client_order_id="rest",
            market_ticker=market.ticker,
            event_ticker=market.event_ticker,
            outcome=Outcome.YES,
            price=Decimal("0.48"),
            remaining=Decimal("20"),
            post_only=True,
        )
    )
    snap = build_snapshot(bot, MidHistory(), now)
    assert snap["gate"]["violation"] is True
    assert snap["gate"]["windows"][0]["violation"] is True


def test_settle_buffer_ignores_expected_expiration(settings: Settings, now: datetime) -> None:
    bot = PaperBot(settings)
    close = now - timedelta(seconds=10)
    market = MarketWindow(
        ticker="KXBTC15M-SETTLE",
        event_ticker="KXBTC15M-SETTLE",
        series_ticker="KXBTC15M",
        title="settling",
        status="determined",
        open_time=close - timedelta(minutes=15),
        close_time=close,
        expected_expiration=now - timedelta(seconds=1),
        settlement_ts=close + timedelta(seconds=7),
        result=Outcome.YES,
    )
    bot.universe.settling[market.ticker] = market
    snap = build_snapshot(bot, MidHistory(), now)
    buf = snap["settle"]["buffers"][0]
    assert buf["expected_expiration_is_lock"] is False
    assert buf["unlocked"] is False
    assert 64 <= buf["seconds_to_unlock"] <= 66
    assert buf["capital_free_at"]
    assert snap["settle"]["not_expected_expiration"] is True
    from kalshi_pbot.risk_engine import capital_free_at

    free = capital_free_at(
        market.close_time,
        75,
        settlement_ts=market.settlement_ts,
        expected_expiration=market.expected_expiration,
    )
    assert (free - market.close_time).total_seconds() == 75


def test_kill_codes_and_manual_endpoint(settings: Settings) -> None:
    assert classify_kill("daily_loss -21 <= -20") == "loss"
    assert classify_kill("open_notional 50 >= 50") == "open"
    assert classify_kill("onesided 30 >= 30") == "one-sided"
    assert classify_kill("manual") == "manual"

    bot = PaperBot(settings)
    hub = HudHub()
    app = create_app(bot, hub, MidHistory())
    client = TestClient(app)
    res = client.post("/api/kill", json={"reason": "manual"})
    assert res.status_code == 200
    body = res.json()
    assert body["kill"]["state"] == "TRIPPED"
    assert body["kill"]["code"] == "manual"
    assert bot.risk.kill_active is True
    snap = client.get("/api/snapshot").json()
    assert snap["kill"]["state"] == "TRIPPED"


def test_drawdown_and_settled_directional(settings: Settings, now: datetime) -> None:
    bot = PaperBot(settings)
    bot.portfolio.apply_fill(_fill(count="10", price="0.40", fill_id="y"))
    bot.portfolio.apply_fill(
        _fill(count="10", price="0.40", outcome=Outcome.NO, fill_id="n")
    )  # locked $2
    high = build_snapshot(bot, MidHistory())
    assert high["extras"]["day_high"] == 2.0
    bot.portfolio.apply_fill(_fill(count="10", price="0.90", fill_id="dir", ticker="KXETH15M-X"))
    bot.portfolio.apply_settlement("KXETH15M-X", Outcome.NO, now=now)
    snap = build_snapshot(bot, MidHistory())
    assert snap["extras"]["settled_directional_pct"] > 0
    assert snap["extras"]["drawdown"] >= 0


def test_book_depth_splits_underround_from_taker_arb() -> None:
    under = _book_depth(book("0.4800", "0.4900"), Decimal("0.02"))
    assert under["underround"] is True
    assert under["arb_taker_eligible"] is False
    assert under["arb"] is False
    assert under["ask_sum"] == 1.03
    assert under["ask_sum_plus_fees"] > under["ask_sum"]
    paper = _book_depth(book("0.4800", "0.4900"), Decimal("0.04"))
    assert paper["underround"] is False
    assert paper["bid_sum"] == 0.97

    # Crossed book: bid_sum 1.40, ask_sum 0.60 + taker fees still < 1.
    regime_a = _book_depth(book("0.7000", "0.7000"), Decimal("0.02"))
    assert regime_a["underround"] is False
    assert regime_a["arb_taker_eligible"] is True
    assert regime_a["arb"] is True
    assert regime_a["ask_sum"] == 0.60
    assert regime_a["ask_sum_plus_fees"] < 1.0
