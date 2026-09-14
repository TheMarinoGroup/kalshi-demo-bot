from __future__ import annotations

from decimal import Decimal

from kalshi_pbot.config import Settings
from kalshi_pbot.fees import pair_edge, taker_fee, taker_pair_viable
from kalshi_pbot.strategy.pair_arb import PairArbStrategy
from kalshi_pbot.types import Liquidity, Outcome
from tests.conftest import book, empty_snapshot, yes_position


def test_taker_fee_peaks_near_mid() -> None:
    mid = taker_fee(Decimal("0.50"), Decimal("1"))
    wing = taker_fee(Decimal("0.10"), Decimal("1"))
    assert mid == Decimal("0.017500")
    assert mid > wing
    # Two-leg taker drag ~3.5¢ at the mid
    assert mid * 2 == Decimal("0.035000")


def test_taker_taker_rejected_on_normal_book(settings: Settings, market) -> None:
    settings = settings.model_copy(update={"taker_pair_arb": True, "min_edge": Decimal("0.02")})
    strategy = PairArbStrategy(settings)
    # yes bid 0.48 → no ask 0.52; no bid 0.49 → yes ask 0.51; sum of asks = 1.03
    snap = empty_snapshot(settings)
    quotes = strategy.evaluate(market, book("0.4800", "0.4900"), snap)
    taker = [q for q in quotes if q.liquidity is Liquidity.TAKER]
    assert taker == []


def test_taker_taker_only_when_asks_sum_below_one_after_fees(settings: Settings, market) -> None:
    settings = settings.model_copy(update={"taker_pair_arb": True, "min_edge": Decimal("0.01")})
    # Crossed / inverted book: yes bid 0.70 → no ask 0.30; no bid 0.70 → yes ask 0.30
    # Combined taker cost 0.60 + fees still << 1
    assert taker_pair_viable(
        Decimal("0.30"),
        Decimal("0.30"),
        Decimal("40"),
        min_edge=Decimal("0.01"),
    )
    strategy = PairArbStrategy(settings)
    quotes = strategy.evaluate(market, book("0.7000", "0.7000"), empty_snapshot(settings))
    taker = [q for q in quotes if q.reason == "taker_taker_pair"]
    assert len(taker) == 2
    assert {q.outcome for q in taker} == {Outcome.YES, Outcome.NO}


def test_maker_maker_when_join_prices_sum_below_one(settings: Settings, market) -> None:
    strategy = PairArbStrategy(settings)
    quotes = strategy.evaluate(market, book("0.4700", "0.4800"), empty_snapshot(settings))
    assert len(quotes) == 2
    assert all(q.post_only and q.liquidity is Liquidity.MAKER for q in quotes)
    assert quotes[0].price + quotes[1].price < Decimal("1")
    edge = pair_edge(
        quotes[0].price,
        quotes[1].price,
        quotes[0].count,
        yes_is_taker=False,
        no_is_taker=False,
    )
    assert edge >= settings.min_edge * quotes[0].count


def test_maker_maker_shades_when_join_sum_is_too_tight(settings: Settings, market) -> None:
    settings = settings.model_copy(update={"min_edge": Decimal("0.04")})
    strategy = PairArbStrategy(settings)
    # Join would be 0.51+0.50 = 1.01; shade until Py+Pn <= 0.96
    quotes = strategy.evaluate(market, book("0.5100", "0.5000"), empty_snapshot(settings))
    assert len(quotes) == 2
    assert quotes[0].price + quotes[1].price <= Decimal("1") - settings.min_edge


def test_maker_maker_skips_when_edge_impossible(settings: Settings, market) -> None:
    settings = settings.model_copy(update={"min_edge": Decimal("0.99")})
    strategy = PairArbStrategy(settings)
    quotes = strategy.evaluate(market, book("0.4800", "0.4900"), empty_snapshot(settings))
    assert quotes == []


def test_pair_arb_skips_when_unpaired_exists(settings: Settings, market) -> None:
    strategy = PairArbStrategy(settings)
    pos = yes_position("KXETH15M-OTHER")
    snap = empty_snapshot(settings, positions={pos.market_ticker: pos})
    assert strategy.evaluate(market, book("0.4700", "0.4800"), snap) == []


def test_pair_arb_respects_only_quote_underround(settings: Settings, market) -> None:
    settings = settings.model_copy(
        update={"only_quote_underround": True, "min_edge": Decimal("0.02")}
    )
    strategy = PairArbStrategy(settings)
    assert strategy.evaluate(market, book("0.5100", "0.5000"), empty_snapshot(settings)) == []
    quotes = strategy.evaluate(market, book("0.4700", "0.4800"), empty_snapshot(settings))
    assert len(quotes) == 2


def test_taker_pair_disabled_by_default(settings: Settings, market) -> None:
    strategy = PairArbStrategy(settings)
    # Even a hugely crossed book should not emit taker intents when the flag is off.
    quotes = strategy.evaluate(market, book("0.9000", "0.9000"), empty_snapshot(settings))
    assert all(q.liquidity is Liquidity.MAKER for q in quotes)
