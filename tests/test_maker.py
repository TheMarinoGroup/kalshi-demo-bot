from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from kalshi_pbot.config import Settings
from kalshi_pbot.strategy.maker import (
    MakerStrategy,
    clip_count,
    is_underround,
    join_bid,
    paired_clip_count,
)
from kalshi_pbot.types import IntentKind, Liquidity, Outcome
from tests.conftest import book, empty_snapshot, yes_position


def test_join_bid_does_not_cross_implied_ask() -> None:
    px = join_bid(Decimal("0.48"), Decimal("0.49"), tick=Decimal("0.01"), improve_ticks=5)
    assert px is not None
    assert px < Decimal("0.49")
    assert px == Decimal("0.48")


def test_join_bid_empty_book_seeds_one_tick() -> None:
    assert join_bid(None, None, tick=Decimal("0.01")) == Decimal("0.01")


def test_clip_count_uses_bankroll_clip() -> None:
    assert clip_count(Decimal("20"), Decimal("0.50")) == Decimal("40")
    assert clip_count(Decimal("20"), Decimal("0.80")) == Decimal("25")


def test_paired_clip_keeps_both_legs_in_band() -> None:
    count = paired_clip_count(
        Decimal("20"),
        Decimal("0.69"),
        Decimal("0.27"),
        clip_min=Decimal("10"),
        clip_max=Decimal("30"),
    )
    assert count is not None
    assert Decimal("10") <= count * Decimal("0.69") <= Decimal("30")
    assert Decimal("10") <= count * Decimal("0.27") <= Decimal("30")
    assert paired_clip_count(
        Decimal("20"),
        Decimal("0.01"),
        Decimal("0.90"),
        clip_min=Decimal("10"),
        clip_max=Decimal("30"),
    ) is None


def test_one_sided_quotes_wider_spread(settings: Settings, market) -> None:
    strategy = MakerStrategy(settings)
    # YES spread 0.52-0.47=0.05; NO spread 0.53-0.50=0.03 → quote YES
    snap = empty_snapshot(settings)
    quotes = strategy.evaluate(market, book("0.4700", "0.4800"), snap)
    assert len(quotes) == 1
    assert quotes[0].outcome is Outcome.YES
    assert quotes[0].post_only
    assert quotes[0].kind is IntentKind.ENTRY


def test_prefers_completing_incomplete_pair(settings: Settings, market) -> None:
    strategy = MakerStrategy(settings)
    pos = yes_position()
    snap = empty_snapshot(settings, positions={pos.market_ticker: pos})
    quotes = strategy.evaluate(market, book("0.4700", "0.4800"), snap)
    assert len(quotes) == 1
    assert quotes[0].outcome is Outcome.NO
    assert quotes[0].kind is IntentKind.COMPLETE_PAIR


def test_two_sided_emits_both_when_join_sum_below_one(settings: Settings, market) -> None:
    settings = settings.model_copy(update={"quote_mode": "two_sided"})
    strategy = MakerStrategy(settings)
    quotes = strategy.evaluate(market, book("0.4700", "0.4800"), empty_snapshot(settings))
    assert len(quotes) == 2
    assert quotes[0].price + quotes[1].price < Decimal("1")


def test_two_sided_seeds_missing_side_at_tick(settings: Settings, market) -> None:
    from kalshi_pbot.types import OrderBook, PriceLevel

    settings = settings.model_copy(update={"quote_mode": "two_sided"})
    strategy = MakerStrategy(settings)
    one_sided_book = OrderBook(
        ticker=market.ticker,
        yes_bids=[PriceLevel(Decimal("0.4700"), Decimal("10"))],
        no_bids=[],
    )
    quotes = strategy.evaluate(market, one_sided_book, empty_snapshot(settings))
    by_outcome = {q.outcome: q for q in quotes}
    assert Outcome.YES in by_outcome and Outcome.NO in by_outcome
    assert by_outcome[Outcome.NO].price == settings.tick_size


def test_no_new_onesided_when_unpaired_exists_other_ticker(
    settings: Settings, market, now
) -> None:
    strategy = MakerStrategy(settings)
    other = yes_position("KXETH15M-OTHER", qty="20", px="0.50")
    snap = empty_snapshot(
        settings,
        positions={other.market_ticker: other},
        unpaired_notional=other.unpaired_notional(),
    )
    quotes = strategy.evaluate(market, book("0.4700", "0.4800"), snap, now=now)
    assert quotes == []


def test_unpaired_age_abort_flattens(settings: Settings, market, now) -> None:
    settings = settings.model_copy(update={"max_unpaired_age_seconds": 90})
    strategy = MakerStrategy(settings)
    pos = yes_position(unpaired_since=now - timedelta(seconds=120))
    snap = empty_snapshot(settings, positions={pos.market_ticker: pos})
    quotes = strategy.evaluate(market, book("0.4700", "0.4800"), snap, now=now)
    assert len(quotes) == 1
    assert quotes[0].kind is IntentKind.FLATTEN
    assert quotes[0].sell is True
    assert quotes[0].outcome is Outcome.YES
    assert quotes[0].reason == "unpaired_age_abort"
    assert quotes[0].liquidity is Liquidity.TAKER


def test_unpaired_cannot_complete_flattens(settings: Settings, market, now) -> None:
    strategy = MakerStrategy(settings)
    pos = yes_position(unpaired_since=now)
    snap = empty_snapshot(settings, positions={pos.market_ticker: pos})
    # Completing NO cannot post: implied NO ask is 0.01 (yes bid 0.99).
    quotes = strategy.evaluate(market, book("0.9900", "0.0100"), snap, now=now)
    assert len(quotes) == 1
    assert quotes[0].kind is IntentKind.FLATTEN
    assert quotes[0].reason == "unpaired_cannot_complete"
    assert quotes[0].outcome is Outcome.YES


def test_fresh_unpaired_still_prefers_complete(settings: Settings, market, now) -> None:
    settings = settings.model_copy(update={"max_unpaired_age_seconds": 90})
    strategy = MakerStrategy(settings)
    pos = yes_position(unpaired_since=now - timedelta(seconds=10))
    snap = empty_snapshot(settings, positions={pos.market_ticker: pos})
    quotes = strategy.evaluate(market, book("0.4700", "0.4800"), snap, now=now)
    assert len(quotes) == 1
    assert quotes[0].kind is IntentKind.COMPLETE_PAIR
    assert quotes[0].outcome is Outcome.NO


def test_only_quote_underround_skips_overround(settings: Settings, market, now) -> None:
    settings = settings.model_copy(
        update={"only_quote_underround": True, "min_edge": Decimal("0.02")}
    )
    strategy = MakerStrategy(settings)
    overround = book("0.5100", "0.5000")
    assert overround.bid_sum() == Decimal("1.01")
    assert not is_underround(overround, Decimal("0.02"))
    assert strategy.evaluate(market, overround, empty_snapshot(settings), now=now) == []

    under = book("0.4700", "0.4800")
    assert is_underround(under, Decimal("0.02"))
    quotes = strategy.evaluate(market, under, empty_snapshot(settings), now=now)
    assert quotes


def test_only_quote_underround_still_completes(settings: Settings, market, now) -> None:
    settings = settings.model_copy(update={"only_quote_underround": True})
    strategy = MakerStrategy(settings)
    pos = yes_position(unpaired_since=now)
    snap = empty_snapshot(settings, positions={pos.market_ticker: pos})
    overround = book("0.5100", "0.5000")
    quotes = strategy.evaluate(market, overround, snap, now=now)
    assert quotes
    assert quotes[0].kind is IntentKind.COMPLETE_PAIR


def test_quote_stays_inside_implied_ask(settings: Settings, market) -> None:
    strategy = MakerStrategy(settings)
    quotes = strategy.evaluate(market, book("0.4800", "0.5100"), empty_snapshot(settings))
    assert quotes
    q = quotes[0]
    if q.outcome is Outcome.YES:
        assert q.price < Decimal("1") - Decimal("0.5100")
    else:
        assert q.price < Decimal("1") - Decimal("0.4800")
