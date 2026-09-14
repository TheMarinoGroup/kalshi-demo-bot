from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kalshi_pbot.config import Settings
from kalshi_pbot.risk_engine import (
    RiskEngine,
    classify_kill,
    in_last_seconds,
    over_soft_onesided,
    should_abort_unpaired,
)
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
    # At the $30 cap the kill latch trips; reject still blocks growth past cap.
    assert decision.reason is RejectReason.KILL_SWITCH
    assert engine.kill_active


def test_onesided_cap_is_aggregate_across_windows(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    btc = yes_position("KXBTC15M-A", qty="50", px="0.50")  # $25 unpaired
    snap = empty_snapshot(
        settings,
        positions={btc.market_ticker: btc},
        unpaired_notional=btc.unpaired_notional(),
        window_ids=frozenset({btc.event_ticker}),
        open_notional=Decimal("25"),
    )
    eth = _intent(event="KXETH15M-B", price="0.50", count="40")  # new one-sided while unpaired
    decision = engine.evaluate(eth, snap, close_time=close, now=now)
    assert decision.allowed is False
    assert decision.reason is RejectReason.UNPAIRED_EXISTS
    assert not engine.kill_active

    # Hard $30 cap still applies to completing-style size on another window.
    complete_other = _intent(
        event="KXETH15M-B",
        price="0.50",
        count="40",
        kind=IntentKind.COMPLETE_PAIR,
    )
    hard = engine.evaluate(complete_other, snap, close_time=close, now=now)
    assert hard.allowed is False
    assert hard.reason is RejectReason.ONESIDED_CAP


def test_open_and_onesided_kill_latch_at_exact_cap(settings: Settings, now: datetime) -> None:
    close = now + timedelta(minutes=10)
    open_engine = RiskEngine(settings)
    at_open = empty_snapshot(settings, open_notional=Decimal("50"))
    open_engine.maybe_trip_limits(at_open)
    assert open_engine.kill_active
    assert classify_kill(open_engine.kill_reason) == "open"
    just_under_open = RiskEngine(settings)
    just_under_open.maybe_trip_limits(empty_snapshot(settings, open_notional=Decimal("49.99")))
    assert not just_under_open.kill_active

    side_engine = RiskEngine(settings)
    at_side = empty_snapshot(settings, unpaired_notional=Decimal("30"))
    side_engine.maybe_trip_limits(at_side)
    assert side_engine.kill_active
    assert classify_kill(side_engine.kill_reason) == "one-sided"
    just_under_side = RiskEngine(settings)
    just_under_side.maybe_trip_limits(empty_snapshot(settings, unpaired_notional=Decimal("29.99")))
    assert not just_under_side.kill_active
    flatten = open_engine.evaluate(
        _intent(kind=IntentKind.FLATTEN, reduce_only=True),
        at_open,
        close_time=close,
        now=now,
    )
    assert flatten.allowed


def test_open_breach_trips_kill_code(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    snap = empty_snapshot(settings, open_notional=Decimal("55"))
    decision = engine.evaluate(_intent(), snap, close_time=close, now=now)
    assert engine.kill_active
    assert decision.reason is RejectReason.KILL_SWITCH
    assert classify_kill(engine.kill_reason) == "open"


def test_soft_unpaired_blocks_new_entry_without_kill(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    pos = yes_position(qty="20", px="0.50")  # $10 unpaired — well under $30 kill
    snap = empty_snapshot(
        settings,
        positions={pos.market_ticker: pos},
        unpaired_notional=pos.unpaired_notional(),
        window_ids=frozenset({pos.event_ticker}),
        open_notional=Decimal("10"),
    )
    decision = engine.evaluate(
        _intent(event="KXETH15M-B", price="0.50", count="20"),
        snap,
        close_time=close,
        now=now,
    )
    assert decision.allowed is False
    assert decision.reason is RejectReason.UNPAIRED_EXISTS
    assert not engine.kill_active


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


def test_should_abort_unpaired_respects_age_knob(settings: Settings, now: datetime) -> None:
    pos = yes_position(qty="20", px="0.50", unpaired_since=now - timedelta(seconds=45))
    aged = settings.model_copy(
        update={"max_unpaired_age_seconds": 45, "soft_onesided": Decimal("30")}
    )
    off = settings.model_copy(
        update={"max_unpaired_age_seconds": 0, "soft_onesided": Decimal("30")}
    )
    fresh = yes_position(qty="20", px="0.50", unpaired_since=now - timedelta(seconds=10))
    assert should_abort_unpaired(pos, aged, now) is True
    assert should_abort_unpaired(pos, off, now) is False
    assert should_abort_unpaired(fresh, aged, now) is False
    assert should_abort_unpaired(yes_position(qty="20"), aged, now) is False


def test_soft_onesided_abort_is_not_a_kill(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    pos = yes_position(qty="30", px="0.50")  # $15 > $10 soft, < $30 hard
    assert over_soft_onesided(pos, settings) is True
    assert should_abort_unpaired(pos, settings, now) is True
    snap = empty_snapshot(
        settings,
        positions={pos.market_ticker: pos},
        unpaired_notional=pos.unpaired_notional(),
        open_notional=Decimal("15"),
    )
    engine.maybe_trip_limits(snap)
    assert not engine.kill_active
    flatten = engine.evaluate(
        _intent(kind=IntentKind.FLATTEN, reduce_only=True),
        snap,
        close_time=now + timedelta(minutes=10),
        now=now,
    )
    assert flatten.allowed


def test_last_120s_blocks_new_entry(settings: Settings, now: datetime) -> None:
    engine = RiskEngine(settings)
    assert settings.last_seconds == 120
    close = now + timedelta(seconds=90)
    snap = empty_snapshot(settings)
    decision = engine.evaluate(_intent(), snap, close_time=close, now=now)
    assert decision.allowed is False
    assert decision.reason is RejectReason.LAST_SECONDS


def test_naive_close_time_is_treated_as_utc(settings: Settings) -> None:
    now = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)
    naive_close = datetime(2026, 9, 13, 20, 0, 30)  # 30s left if UTC
    assert in_last_seconds(naive_close, 60, now)
