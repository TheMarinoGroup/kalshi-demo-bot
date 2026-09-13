from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kalshi_pbot.config import Settings
from kalshi_pbot.risk_engine import RiskEngine, in_last_seconds
from kalshi_pbot.types import IntentKind, Liquidity, Outcome, QuoteIntent, RejectReason, TimeInForce
from tests.conftest import empty_snapshot, yes_position


def _intent(
    *,
    price: str = "0.50",
    count: str = "40",
    kind: IntentKind = IntentKind.ENTRY,
    outcome: Outcome = Outcome.YES,
    event: str = "KXBTC15M-MOCK",
    reduce_only: bool = False,
) -> QuoteIntent:
    return QuoteIntent(
        market_ticker=event,
        event_ticker=event,
        outcome=outcome,
        price=Decimal(price),
        count=Decimal(count),
        liquidity=Liquidity.MAKER,
        tif=TimeInForce.GTC,
        post_only=True,
        reduce_only=reduce_only,
        kind=kind,
        reason="test",
    )


def test_last_60s_blocks_new_entries(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(seconds=30)
    assert in_last_seconds(close, 60, now)
    snap = empty_snapshot(settings)
    decision = engine.evaluate(_intent(), snap, close_time=close, now=now)
    assert decision.allowed is False
    assert decision.reason is RejectReason.LAST_SECONDS


def test_last_60s_allows_flatten_and_cancel(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(seconds=15)
    snap = empty_snapshot(settings)
    flatten = _intent(kind=IntentKind.FLATTEN, reduce_only=True)
    cancel = _intent(kind=IntentKind.CANCEL, reduce_only=True)
    assert engine.evaluate(flatten, snap, close_time=close, now=now).allowed
    assert engine.evaluate(cancel, snap, close_time=close, now=now).allowed


def test_outside_last_60s_allows_entry(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    snap = empty_snapshot(settings)
    decision = engine.evaluate(_intent(), snap, close_time=close, now=now)
    assert decision.allowed
    assert decision.reason is RejectReason.OK


def test_daily_loss_kill_includes_fees_and_unsettled(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    # -$12 realized, -$3 fees, -$6 unsettled = -$21 vs $20 kill
    snap = empty_snapshot(
        settings,
        realized_pnl=Decimal("-12"),
        fees=Decimal("3"),
        unrealized_pnl=Decimal("-6"),
        daily_pnl=Decimal("-21"),
    )
    decision = engine.evaluate(_intent(), snap, close_time=close, now=now)
    assert decision.allowed is False
    assert decision.reason is RejectReason.KILL_SWITCH
    assert engine.kill_active
    # Flatten still allowed after kill
    flatten = engine.evaluate(
        _intent(kind=IntentKind.FLATTEN, reduce_only=True),
        snap,
        close_time=close,
        now=now,
    )
    assert flatten.allowed


def test_daily_kill_not_tripped_above_floor(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    snap = empty_snapshot(settings, daily_pnl=Decimal("-19.99"))
    assert engine.evaluate(_intent(), snap, close_time=close, now=now).allowed


def test_onesided_cap_blocks_more_same_side(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    pos = yes_position(qty="60", px="0.50")  # $30 already at the cap
    snap = empty_snapshot(
        settings,
        positions={pos.market_ticker: pos},
        unpaired_notional=pos.unpaired_notional(),
        window_ids=frozenset({pos.event_ticker}),
    )
    more_yes = _intent(price="0.50", count="20", outcome=Outcome.YES)
    decision = engine.evaluate(more_yes, snap, close_time=close, now=now)
    assert decision.allowed is False
    assert decision.reason is RejectReason.ONESIDED_CAP


def test_onesided_cap_allows_completing_other_side(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    pos = yes_position(qty="60", px="0.50")
    snap = empty_snapshot(
        settings,
        positions={pos.market_ticker: pos},
        window_ids=frozenset({pos.event_ticker}),
    )
    complete = _intent(
        price="0.48",
        count="40",
        outcome=Outcome.NO,
        kind=IntentKind.COMPLETE_PAIR,
    )
    decision = engine.evaluate(complete, snap, close_time=close, now=now)
    assert decision.allowed
    assert decision.reason is RejectReason.OK


def test_open_notional_cap(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    snap = empty_snapshot(settings, open_notional=Decimal("40"), window_ids=frozenset({"W1"}))
    # $20 clip would make $60 > $50
    decision = engine.evaluate(_intent(price="0.50", count="40"), snap, close_time=close, now=now)
    assert decision.allowed is False
    assert decision.reason is RejectReason.OPEN_NOTIONAL


def test_max_concurrent_windows(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    snap = empty_snapshot(settings, window_ids=frozenset({"W1", "W2"}))
    decision = engine.evaluate(
        _intent(event="W3"),
        snap,
        close_time=close,
        now=now,
    )
    assert decision.allowed is False
    assert decision.reason is RejectReason.CONCURRENT_WINDOWS
    # Existing window can still quote
    same = engine.evaluate(_intent(event="W1"), snap, close_time=close, now=now)
    assert same.allowed


def test_per_fill_clip_band(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    snap = empty_snapshot(settings)
    tiny = engine.evaluate(_intent(price="0.50", count="4"), snap, close_time=close, now=now)
    huge = engine.evaluate(_intent(price="0.50", count="80"), snap, close_time=close, now=now)
    ok = engine.evaluate(_intent(price="0.50", count="40"), snap, close_time=close, now=now)
    assert tiny.reason is RejectReason.PER_FILL
    assert huge.reason is RejectReason.PER_FILL
    assert ok.allowed


def test_kill_switch_blocks_until_reset(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    engine.trip("manual")
    close = now + timedelta(minutes=10)
    snap = empty_snapshot(settings)
    blocked = engine.evaluate(_intent(), snap, close_time=close, now=now)
    assert blocked.reason is RejectReason.KILL_SWITCH
    engine.reset_kill()
    assert engine.evaluate(_intent(), snap, close_time=close, now=now).allowed


def test_limits_rescale_with_bankroll(now: datetime) -> None:
    fat = Settings(bankroll=Decimal("2000"), dry_run=True)
    assert fat.max_open_notional == Decimal("100.00")
    assert fat.daily_loss_limit == Decimal("40.00")
    assert fat.max_onesided == Decimal("60.00")
    engine = RiskEngine(fat)
    close = now + timedelta(minutes=10)
    snap = empty_snapshot(fat, daily_pnl=Decimal("-21"))
    # -$21 is fatal at $1000 but not at $2000
    assert engine.evaluate(_intent(), snap, close_time=close, now=now).allowed


def test_naive_close_time_is_treated_as_utc(settings: Settings) -> None:
    now = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)
    naive_close = datetime(2026, 9, 13, 20, 0, 30)  # 30s left if UTC
    assert in_last_seconds(naive_close, 60, now)
