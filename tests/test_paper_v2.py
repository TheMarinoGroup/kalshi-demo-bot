from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from kalshi_pbot.config import Settings
from kalshi_pbot.strategy.maker import MakerStrategy
from kalshi_pbot.strategy.paper_v2 import PaperV2State, classify_paper_v2
from kalshi_pbot.types import IntentKind
from tests.conftest import book, empty_snapshot, yes_position


def option_b() -> Settings:
    """Bare Option B defaults ($500 / clip $10 / 1 window / BTC-only)."""
    return Settings(dry_run=True, mock=True, paper_tape=True)


def test_option_b_hard_caps() -> None:
    settings = option_b()
    assert settings.bankroll == Decimal("500")
    assert settings.clip == Decimal("10")
    assert settings.clip_dollars == Decimal("10")
    assert settings.max_open_notional == Decimal("25.00")
    assert settings.max_onesided == Decimal("15.00")
    assert settings.daily_loss_limit == Decimal("10.00")
    assert settings.max_windows == 1
    assert settings.series_tickers == ("KXBTC15M",)
    assert settings.min_edge == Decimal("0.04")
    assert settings.last_seconds == 120
    assert settings.quote_mode == "one_sided"
    assert settings.improve_ticks == 0
    assert settings.taker_pair_arb is False
    assert settings.soft_onesided == Decimal("10")
    assert settings.max_unpaired_age_seconds == 45


def test_option_b_requires_soft_abort_when_daily_equals_clip() -> None:
    with pytest.raises(ValueError, match="soft abort is mandatory"):
        Settings(max_unpaired_age_seconds=0)
    with pytest.raises(ValueError, match="soft abort is mandatory"):
        Settings(soft_onesided=Decimal("0"))
    # Larger bankroll: daily kill $20 > clip $10, age=0 is allowed.
    ok = Settings(bankroll=Decimal("1000"), max_unpaired_age_seconds=0)
    assert ok.max_unpaired_age_seconds == 0


def test_flat_allows_one_sided_only(now) -> None:
    settings = option_b()
    snap = empty_snapshot(settings)
    decision = classify_paper_v2(snap, "KXBTC15M-MOCK", settings, now=now)
    assert decision.state is PaperV2State.FLAT
    assert decision.allow_new_onesided is True
    assert decision.complete_other_side is False
    assert decision.flatten is False


def test_complete_after_touch_at_soft_cap(now) -> None:
    settings = option_b()
    pos = yes_position(qty="20", px="0.50", unpaired_since=now)  # $10
    snap = empty_snapshot(settings, positions={pos.market_ticker: pos})
    decision = classify_paper_v2(snap, pos.market_ticker, settings, now=now)
    assert decision.state is PaperV2State.COMPLETE
    assert decision.complete_other_side is True
    assert decision.allow_new_onesided is False


def test_soft_abort_above_ten_before_hard_fifteen(now) -> None:
    settings = option_b()
    pos = yes_position(qty="22", px="0.50", unpaired_since=now)  # $11 > $10, < $15
    snap = empty_snapshot(
        settings,
        positions={pos.market_ticker: pos},
        unpaired_notional=pos.unpaired_notional(),
        open_notional=pos.unpaired_notional(),
    )
    decision = classify_paper_v2(snap, pos.market_ticker, settings, now=now)
    assert decision.state is PaperV2State.SOFT_ABORT
    assert decision.flatten is True
    assert decision.reason == "unpaired_soft_abort"


def test_age_abort_at_45s(now) -> None:
    settings = option_b()
    pos = yes_position(qty="20", px="0.50", unpaired_since=now - timedelta(seconds=45))
    snap = empty_snapshot(settings, positions={pos.market_ticker: pos})
    decision = classify_paper_v2(snap, pos.market_ticker, settings, now=now)
    assert decision.state is PaperV2State.SOFT_ABORT
    assert decision.reason == "unpaired_age_abort"


def test_cannot_complete_is_soft_abort(now) -> None:
    settings = option_b()
    pos = yes_position(qty="20", px="0.50", unpaired_since=now)
    snap = empty_snapshot(settings, positions={pos.market_ticker: pos})
    decision = classify_paper_v2(
        snap, pos.market_ticker, settings, now=now, can_complete=False
    )
    assert decision.state is PaperV2State.SOFT_ABORT
    assert decision.reason == "unpaired_cannot_complete"


def test_block_new_when_unpaired_exists_elsewhere(now) -> None:
    settings = option_b()
    other = yes_position("KXETH15M-OTHER", qty="20", px="0.50", unpaired_since=now)
    snap = empty_snapshot(settings, positions={other.market_ticker: other})
    decision = classify_paper_v2(snap, "KXBTC15M-MOCK", settings, now=now)
    assert decision.state is PaperV2State.BLOCK_NEW
    assert decision.allow_new_onesided is False
    assert decision.complete_other_side is False


def test_last_seconds_blocks_new_and_flatten_only(now) -> None:
    settings = option_b()
    close = now + timedelta(seconds=90)
    snap = empty_snapshot(settings)
    decision = classify_paper_v2(
        snap, "KXBTC15M-MOCK", settings, now=now, close_time=close
    )
    assert decision.state is PaperV2State.LAST_SECONDS
    assert decision.allow_new_onesided is False
    assert decision.flatten is True


def test_hard_kill_onesided_at_fifteen(now) -> None:
    settings = option_b()
    pos = yes_position(qty="30", px="0.50", unpaired_since=now)  # $15
    snap = empty_snapshot(
        settings,
        positions={pos.market_ticker: pos},
        unpaired_notional=Decimal("15"),
        open_notional=Decimal("15"),
    )
    decision = classify_paper_v2(snap, pos.market_ticker, settings, now=now)
    assert decision.state is PaperV2State.HARD_KILL
    assert decision.reason == "onesided_hard"


def test_hard_kill_daily_at_ten(now) -> None:
    settings = option_b()
    snap = empty_snapshot(settings, daily_pnl=Decimal("-10"))
    decision = classify_paper_v2(snap, "KXBTC15M-MOCK", settings, now=now)
    assert decision.state is PaperV2State.HARD_KILL
    assert decision.reason == "daily_loss"


def test_hard_kill_open_at_twenty_five(now) -> None:
    settings = option_b()
    snap = empty_snapshot(settings, open_notional=Decimal("25"))
    decision = classify_paper_v2(snap, "KXBTC15M-MOCK", settings, now=now)
    assert decision.state is PaperV2State.HARD_KILL
    assert decision.reason == "open_notional"


def test_maker_follows_complete_then_soft_abort(market, now) -> None:
    settings = option_b()
    strategy = MakerStrategy(settings)
    pos = yes_position(qty="20", px="0.50", unpaired_since=now)
    snap = empty_snapshot(settings, positions={pos.market_ticker: pos})
    quotes = strategy.evaluate(market, book("0.4700", "0.4800"), snap, now=now)
    assert len(quotes) == 1
    assert quotes[0].kind is IntentKind.COMPLETE_PAIR

    fat = yes_position(qty="22", px="0.50", unpaired_since=now)
    fat_snap = empty_snapshot(
        settings,
        positions={fat.market_ticker: fat},
        unpaired_notional=fat.unpaired_notional(),
    )
    abort = strategy.evaluate(market, book("0.4700", "0.4800"), fat_snap, now=now)
    assert len(abort) == 1
    assert abort[0].kind is IntentKind.FLATTEN
    assert abort[0].reason == "unpaired_soft_abort"
