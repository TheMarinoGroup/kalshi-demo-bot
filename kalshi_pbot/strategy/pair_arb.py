"""YES+NO pair arbitrage.

Maker–maker is the realistic path: post both bids only when
Py + Pn + maker_fees + min_edge < 1.

Taker–taker (hit both implied asks) is disabled by default. At the mid,
quadratic taker fees peak at ~1.75¢/contract per leg (~3.5¢ combined),
so you need Py + Pn ≲ 0.965 after fees — almost never available on a
healthy book, because taking both asks costs 2 − (yes_bid + no_bid).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from kalshi_pbot.config import CLIP_MAX, CLIP_MIN, Settings
from kalshi_pbot.fees import maker_pair_viable, pair_edge, taker_pair_viable
from kalshi_pbot.strategy.maker import clip_count, join_bid, paired_clip_count, quantize_price
from kalshi_pbot.types import (
    IntentKind,
    Liquidity,
    MarketWindow,
    OrderBook,
    Outcome,
    PortfolioSnapshot,
    QuoteIntent,
    TimeInForce,
)


@dataclass
class PairArbStrategy:
    settings: Settings

    def evaluate(
        self,
        market: MarketWindow,
        book: OrderBook,
        snapshot: PortfolioSnapshot,
    ) -> list[QuoteIntent]:
        del snapshot  # pairing is book-driven; inventory completion lives in maker
        count_hint = clip_count(self.settings.clip, Decimal("0.50"))

        if self.settings.taker_pair_arb:
            taker = self._taker_taker(market, book, count_hint)
            if taker:
                return taker

        return self._maker_maker(market, book)

    def _taker_taker(
        self,
        market: MarketWindow,
        book: OrderBook,
        count: Decimal,
    ) -> list[QuoteIntent]:
        yes_ask = book.implied_yes_ask()
        no_ask = book.implied_no_ask()
        if yes_ask is None or no_ask is None:
            return []
        if not taker_pair_viable(
            yes_ask,
            no_ask,
            count,
            min_edge=self.settings.min_edge,
            fee_type=market.fee_type,
            multiplier=market.fee_multiplier,
        ):
            return []
        return [
            QuoteIntent(
                market_ticker=market.ticker,
                event_ticker=market.event_ticker,
                outcome=Outcome.YES,
                price=yes_ask,
                count=count,
                liquidity=Liquidity.TAKER,
                tif=TimeInForce.IOC,
                post_only=False,
                kind=IntentKind.ENTRY,
                reason="taker_taker_pair",
            ),
            QuoteIntent(
                market_ticker=market.ticker,
                event_ticker=market.event_ticker,
                outcome=Outcome.NO,
                price=no_ask,
                count=count,
                liquidity=Liquidity.TAKER,
                tif=TimeInForce.IOC,
                post_only=False,
                kind=IntentKind.ENTRY,
                reason="taker_taker_pair",
            ),
        ]

    def _maker_maker(self, market: MarketWindow, book: OrderBook) -> list[QuoteIntent]:
        if book.best_yes_bid() is None or book.best_no_bid() is None:
            return []
        yes_px = join_bid(
            book.best_yes_bid(),
            book.implied_yes_ask(),
            tick=self.settings.tick_size,
            improve_ticks=self.settings.improve_ticks,
        )
        no_px = join_bid(
            book.best_no_bid(),
            book.implied_no_ask(),
            tick=self.settings.tick_size,
            improve_ticks=self.settings.improve_ticks,
        )
        if yes_px is None or no_px is None:
            return []

        # If the join pair is too expensive, shade both sides equally until edge appears.
        yes_px, no_px = self._shade_to_edge(yes_px, no_px)
        if yes_px is None or no_px is None:
            return []

        count = paired_clip_count(
            self.settings.clip,
            yes_px,
            no_px,
            clip_min=CLIP_MIN,
            clip_max=CLIP_MAX,
        )
        if count is None:
            return []
        if not maker_pair_viable(
            yes_px,
            no_px,
            count,
            min_edge=self.settings.min_edge,
            fee_type=market.fee_type,
            multiplier=market.fee_multiplier,
        ):
            return []

        edge = pair_edge(
            yes_px,
            no_px,
            count,
            yes_is_taker=False,
            no_is_taker=False,
            fee_type=market.fee_type,
            multiplier=market.fee_multiplier,
        )
        reason = f"maker_maker_pair edge={edge}"
        return [
            QuoteIntent(
                market_ticker=market.ticker,
                event_ticker=market.event_ticker,
                outcome=Outcome.YES,
                price=yes_px,
                count=count,
                liquidity=Liquidity.MAKER,
                tif=TimeInForce.GTC,
                post_only=True,
                kind=IntentKind.ENTRY,
                reason=reason,
            ),
            QuoteIntent(
                market_ticker=market.ticker,
                event_ticker=market.event_ticker,
                outcome=Outcome.NO,
                price=no_px,
                count=count,
                liquidity=Liquidity.MAKER,
                tif=TimeInForce.GTC,
                post_only=True,
                kind=IntentKind.ENTRY,
                reason=reason,
            ),
        ]

    def _shade_to_edge(
        self, yes_px: Decimal, no_px: Decimal
    ) -> tuple[Decimal | None, Decimal | None]:
        tick = self.settings.tick_size
        min_edge = self.settings.min_edge
        for _ in range(50):
            if yes_px + no_px <= Decimal("1") - min_edge:
                return yes_px, no_px
            # Shade the more expensive leg first.
            if yes_px >= no_px:
                nxt = quantize_price(yes_px - tick, tick)
                if nxt >= yes_px:
                    return None, None
                yes_px = nxt
            else:
                nxt = quantize_price(no_px - tick, tick)
                if nxt >= no_px:
                    return None, None
                no_px = nxt
            if yes_px < tick or no_px < tick:
                return None, None
        return None, None
