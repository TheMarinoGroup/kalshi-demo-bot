"""Maker-first quoting for 15-minute crypto Up/Down markets.

Paper-v2 Option B is one-sided and is driven by ``classify_paper_v2``:
quote one clip while flat; after any fill/touch, complete the other side
only; flatten on soft onesided ($10), unpaired age (45s), or if the
completing quote cannot post; never open a new clip while unpaired exists.
Two-sided mode is a legacy/test path only (``quote_mode=two_sided``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from kalshi_pbot.config import CLIP_MAX, CLIP_MIN, Settings
from kalshi_pbot.execution import flatten_intent
from kalshi_pbot.strategy.paper_v2 import PaperV2State, classify_paper_v2
from kalshi_pbot.types import (
    IntentKind,
    Liquidity,
    MarketWindow,
    OrderBook,
    Outcome,
    PortfolioSnapshot,
    Position,
    QuoteIntent,
    TimeInForce,
)

TICK = Decimal("0.01")
ONE = Decimal("1")


def quantize_price(price: Decimal, tick: Decimal = TICK) -> Decimal:
    stepped = (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    if stepped < tick:
        return tick
    if stepped > ONE - tick:
        return ONE - tick
    return stepped


def clip_count(clip_dollars: Decimal, price: Decimal) -> Decimal:
    """Size a clip so notional stays inside the $10–$30 band when possible."""
    if price <= 0:
        return Decimal("0")
    want = (clip_dollars / price).to_integral_value(rounding=ROUND_DOWN)
    need = (CLIP_MIN / price).to_integral_value(rounding=ROUND_CEILING)
    cap = (CLIP_MAX / price).to_integral_value(rounding=ROUND_DOWN)
    count = max(need, want, Decimal("1"))
    if cap > 0:
        count = min(count, cap)
    return count


def paired_clip_count(
    clip_dollars: Decimal,
    yes_price: Decimal,
    no_price: Decimal,
    *,
    clip_min: Decimal,
    clip_max: Decimal,
) -> Decimal | None:
    """Same contract count on both legs, each notional inside the $10–$30 band."""
    if yes_price <= 0 or no_price <= 0:
        return None
    cheap = min(yes_price, no_price)
    rich = max(yes_price, no_price)
    # Need count * cheap >= clip_min and count * rich <= clip_max
    need = (clip_min / cheap).to_integral_value(rounding=ROUND_CEILING)
    cap = (clip_max / rich).to_integral_value(rounding=ROUND_DOWN)
    want = (clip_dollars / rich).to_integral_value(rounding=ROUND_DOWN)
    count = min(cap, max(need, want))
    if count < need or count <= 0:
        return None
    if count * cheap < clip_min or count * rich > clip_max:
        return None
    return count


def join_bid(
    best_bid: Decimal | None,
    implied_ask: Decimal | None,
    *,
    tick: Decimal,
    improve_ticks: int = 0,
) -> Decimal | None:
    """Return a post-only bid that does not cross the implied ask."""
    if best_bid is None:
        raw = tick
    else:
        raw = best_bid + tick * improve_ticks
    px = quantize_price(raw, tick)
    if implied_ask is not None and px >= implied_ask:
        px = quantize_price(implied_ask - tick, tick)
        if implied_ask is not None and px >= implied_ask:
            return None
    if px <= 0 or px >= 1:
        return None
    return px


def _position(snapshot: PortfolioSnapshot, ticker: str) -> Position | None:
    return snapshot.positions.get(ticker)


def is_underround(book: OrderBook, min_edge: Decimal) -> bool:
    """True when yes_bid + no_bid is strictly inside 1 − min_edge (Regime B)."""
    bid_sum = book.bid_sum()
    return bid_sum is not None and bid_sum <= Decimal("1") - min_edge


@dataclass
class MakerStrategy:
    settings: Settings

    def evaluate(
        self,
        market: MarketWindow,
        book: OrderBook,
        snapshot: PortfolioSnapshot,
        *,
        now: datetime | None = None,
    ) -> list[QuoteIntent]:
        now = now or datetime.now(UTC)
        pos = _position(snapshot, market.ticker)
        unpaired = pos.unpaired_outcome if pos else None

        complete = None
        can_complete = True
        if unpaired is not None and pos is not None:
            completing = Outcome.NO if unpaired is Outcome.YES else Outcome.YES
            complete = self._quote_side(
                market,
                book,
                completing,
                kind=IntentKind.COMPLETE_PAIR,
                reason="complete_incomplete_pair",
                improve_ticks=self.settings.improve_ticks,
            )
            can_complete = complete is not None

        decision = classify_paper_v2(
            snapshot,
            market.ticker,
            self.settings,
            now=now,
            close_time=market.close_time,
            can_complete=can_complete,
        )
        if decision.state is PaperV2State.HARD_KILL:
            return []
        if decision.flatten and pos is not None and unpaired is not None:
            flatten = self._flatten_unpaired(
                market, book, pos, unpaired, reason=decision.reason
            )
            return [flatten] if flatten else []
        if decision.complete_other_side:
            return [complete] if complete else []
        if not decision.allow_new_onesided:
            return []

        if self.settings.only_quote_underround and not is_underround(book, self.settings.min_edge):
            return []

        if self.settings.quote_mode == "two_sided":
            quotes = []
            for outcome in (Outcome.YES, Outcome.NO):
                intent = self._quote_side(
                    market, book, outcome, kind=IntentKind.ENTRY, reason="two_sided_mm"
                )
                if intent:
                    quotes.append(intent)
            if len(quotes) == 2 and quotes[0].price + quotes[1].price >= Decimal("1"):
                # Do not warehouse both sides without pair edge; fall back to one side.
                quotes = quotes[:1]
            return quotes

        # One-sided default: quote the wider / more conservative side.
        yes_spread = None
        no_spread = None
        if book.best_yes_bid() is not None and book.implied_yes_ask() is not None:
            yes_spread = book.implied_yes_ask() - book.best_yes_bid()  # type: ignore[operator]
        if book.best_no_bid() is not None and book.implied_no_ask() is not None:
            no_spread = book.implied_no_ask() - book.best_no_bid()  # type: ignore[operator]

        if yes_spread is None and no_spread is None:
            # Empty book: seed a yes bid at one tick.
            intent = self._quote_side(
                market, book, Outcome.YES, kind=IntentKind.ENTRY, reason="seed_empty_book"
            )
            return [intent] if intent else []

        if no_spread is None or (yes_spread is not None and yes_spread >= no_spread):
            side = Outcome.YES
        else:
            side = Outcome.NO
        intent = self._quote_side(
            market, book, side, kind=IntentKind.ENTRY, reason="one_sided_mm"
        )
        return [intent] if intent else []

    def _flatten_unpaired(
        self,
        market: MarketWindow,
        book: OrderBook,
        pos: Position,
        unpaired: Outcome,
        *,
        reason: str,
    ) -> QuoteIntent | None:
        bid = book.best_yes_bid() if unpaired is Outcome.YES else book.best_no_bid()
        if bid is None or pos.unpaired_qty <= 0:
            return None
        intent = flatten_intent(
            market.ticker,
            market.event_ticker,
            unpaired,
            pos.unpaired_qty,
            bid,
        )
        return QuoteIntent(
            market_ticker=intent.market_ticker,
            event_ticker=intent.event_ticker,
            outcome=intent.outcome,
            price=intent.price,
            count=intent.count,
            liquidity=intent.liquidity,
            tif=intent.tif,
            post_only=False,
            reduce_only=True,
            sell=True,
            kind=IntentKind.FLATTEN,
            reason=reason,
        )

    def _quote_side(
        self,
        market: MarketWindow,
        book: OrderBook,
        outcome: Outcome,
        *,
        kind: IntentKind,
        reason: str,
        improve_ticks: int | None = None,
    ) -> QuoteIntent | None:
        if outcome is Outcome.YES:
            best = book.best_yes_bid()
            ask = book.implied_yes_ask()
        else:
            best = book.best_no_bid()
            ask = book.implied_no_ask()
        price = join_bid(
            best,
            ask,
            tick=self.settings.tick_size,
            improve_ticks=self.settings.improve_ticks if improve_ticks is None else improve_ticks,
        )
        if price is None:
            return None
        count = clip_count(self.settings.clip, price)
        return QuoteIntent(
            market_ticker=market.ticker,
            event_ticker=market.event_ticker,
            outcome=outcome,
            price=price,
            count=count,
            liquidity=Liquidity.MAKER,
            tif=TimeInForce.GTC,
            post_only=True,
            reduce_only=False,
            kind=kind,
            reason=reason,
        )
