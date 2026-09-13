from __future__ import annotations

from decimal import Decimal

from kalshi_pbot.config import Settings
from kalshi_pbot.strategy.maker import MakerStrategy, clip_count, join_bid, paired_clip_count
from kalshi_pbot.types import IntentKind, Outcome
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


def test_quote_stays_inside_implied_ask(settings: Settings, market) -> None:
    strategy = MakerStrategy(settings)
    quotes = strategy.evaluate(market, book("0.4800", "0.5100"), empty_snapshot(settings))
    assert quotes
    q = quotes[0]
    if q.outcome is Outcome.YES:
        assert q.price < Decimal("1") - Decimal("0.5100")
    else:
        assert q.price < Decimal("1") - Decimal("0.4800")
