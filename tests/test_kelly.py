from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from kalshi_pbot.config import Settings
from kalshi_pbot.kelly import (
    DEFAULT_KELLY_MAX,
    count_within_kelly,
    kelly_cap_notional,
    kelly_fraction,
    over_kelly_max,
)
from kalshi_pbot.risk_engine import RiskEngine
from kalshi_pbot.strategy.maker import MakerStrategy
from kalshi_pbot.types import IntentKind, Outcome, RejectReason
from tests.conftest import book, empty_snapshot
from tests.test_risk_engine import _intent


def test_kelly_helpers_clamp_at_025() -> None:
    bankroll = Decimal("500")
    assert DEFAULT_KELLY_MAX == Decimal("0.25")
    assert kelly_cap_notional(bankroll, DEFAULT_KELLY_MAX) == Decimal("125.0000")
    assert kelly_fraction(Decimal("125"), bankroll) == Decimal("0.25")
    assert over_kelly_max(Decimal("125"), bankroll, DEFAULT_KELLY_MAX) is False
    assert over_kelly_max(Decimal("125.0001"), bankroll, DEFAULT_KELLY_MAX) is True
    clipped = count_within_kelly(Decimal("400"), Decimal("0.50"), bankroll, DEFAULT_KELLY_MAX)
    assert clipped * Decimal("0.50") <= Decimal("125")
    assert clipped == Decimal("250")


def test_risk_refuses_kelly_above_025(now) -> None:
    settings = Settings(dry_run=True, mock=True, paper_tape=True)
    assert settings.kelly_max == Decimal("0.25")
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    snap = empty_snapshot(settings)
    over = engine.evaluate(
        _intent(price="0.50", count="260"),  # $130 = 0.26 of $500
        snap,
        close_time=close,
        now=now,
    )
    assert over.allowed is False
    assert over.reason is RejectReason.KELLY_CAP
    at_cap = engine.evaluate(
        _intent(price="0.50", count="250"),  # $125 = 0.25
        snap,
        close_time=close,
        now=now,
    )
    assert at_cap.reason is not RejectReason.KELLY_CAP


def test_maker_clips_size_intent_to_kelly_max(market, now) -> None:
    settings = Settings(dry_run=True, mock=True, paper_tape=True)
    strategy = MakerStrategy(settings)
    intent = strategy._quote_side(
        market,
        book("0.4700", "0.4800"),
        Outcome.YES,
        kind=IntentKind.ENTRY,
        reason="kelly_clip",
        count=Decimal("400"),
        snapshot=None,
    )
    assert intent is not None
    cap = settings.bankroll * settings.kelly_max
    assert intent.notional <= cap
    assert over_kelly_max(intent.notional, settings.bankroll, settings.kelly_max) is False


def test_flatten_is_not_kelly_gated(settings, now) -> None:
    engine = RiskEngine(settings)
    close = now + timedelta(minutes=10)
    huge = _intent(price="0.50", count="600", kind=IntentKind.FLATTEN, reduce_only=True)
    decision = engine.evaluate(huge, empty_snapshot(settings), close_time=close, now=now)
    assert decision.allowed is True
    assert decision.reason is RejectReason.OK
