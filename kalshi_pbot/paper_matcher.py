"""Local paper matcher — conservative queue + latency buckets.

This is a sampler / paper tape, not the exchange. Rules:

* Join the **back** of the book at our price (queue_ahead = size already there).
* Fills require a public **trade** that prints at or through our price.
* Size drops without a matching trade are **ambiguous wipes → no fill**.
  Queue ahead may shrink (cancels in front); our remaining never fills.
* If the level vanishes and trades did not eat our queue, cancel without fill.
* Latency L ∈ {50, 150, 500} ms: market events apply at ts + L.

Risk Desk v1 still gates which quotes are even registered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

from kalshi_pbot.fees import fee_drag
from kalshi_pbot.types import (
    Fill,
    OrderBook,
    Outcome,
    PaperFill,
    PublicTrade,
    QuoteIntent,
)

log = structlog.get_logger(__name__)


@dataclass
class PaperResting:
    order_id: str
    market_ticker: str
    event_ticker: str
    outcome: Outcome
    price: Decimal
    remaining: Decimal
    queue_ahead: Decimal
    latency_ms: int
    placed_ts_ms: int
    visible_at_ms: int


@dataclass
class _Delayed:
    apply_at_ms: int
    kind: str
    payload: Any


@dataclass
class PaperMatcher:
    latency_ms: int
    fee_type: str = "quadratic"
    fee_multiplier: Decimal = Decimal("1")
    orders: dict[str, PaperResting] = field(default_factory=dict)
    _q: list[_Delayed] = field(default_factory=list)
    _seq: int = 0
    fills: list[PaperFill] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    ambiguous_wipes: int = 0
    wipe_events: list[dict[str, Any]] = field(default_factory=list)

    def place(self, intent: QuoteIntent, book: OrderBook, now_ms: int) -> PaperResting:
        self._seq += 1
        order_id = intent.client_order_id or f"paper-{self._seq}"
        queue = book.size_at(intent.outcome, intent.price)
        resting = PaperResting(
            order_id=order_id,
            market_ticker=intent.market_ticker,
            event_ticker=intent.event_ticker,
            outcome=intent.outcome,
            price=intent.price,
            remaining=intent.count,
            queue_ahead=queue,
            latency_ms=self.latency_ms,
            placed_ts_ms=now_ms,
            visible_at_ms=now_ms + self.latency_ms,
        )
        self.orders[order_id] = resting
        log.info(
            "paper_join_back",
            order_id=order_id,
            ticker=intent.market_ticker,
            outcome=intent.outcome.value,
            price=str(intent.price),
            count=str(intent.count),
            queue_ahead=str(queue),
            latency_ms=self.latency_ms,
        )
        return resting

    def cancel(self, order_id: str, *, reason: str = "cancel") -> PaperResting | None:
        order = self.orders.pop(order_id, None)
        if order:
            self.cancelled.append(order_id)
            log.info(
                "paper_cancel",
                order_id=order_id,
                reason=reason,
                remaining=str(order.remaining),
            )
        return order

    def cancel_ticker(self, ticker: str, *, reason: str = "cancel") -> None:
        for oid in [o.order_id for o in self.orders.values() if o.market_ticker == ticker]:
            self.cancel(oid, reason=reason)

    def enqueue_trade(self, trade: PublicTrade) -> None:
        self._q.append(_Delayed(trade.ts_ms + self.latency_ms, "trade", trade))

    def enqueue_size_change(
        self,
        *,
        ticker: str,
        outcome: Outcome,
        price: Decimal,
        old_size: Decimal,
        new_size: Decimal,
        ts_ms: int,
    ) -> None:
        self._q.append(
            _Delayed(
                ts_ms + self.latency_ms,
                "size",
                {
                    "ticker": ticker,
                    "outcome": outcome,
                    "price": price,
                    "old": old_size,
                    "new": new_size,
                },
            )
        )

    def drain(self, now_ms: int) -> list[PaperFill]:
        due = [e for e in self._q if e.apply_at_ms <= now_ms]
        self._q = [e for e in self._q if e.apply_at_ms > now_ms]
        due.sort(key=lambda e: (e.apply_at_ms, 0 if e.kind == "trade" else 1))
        produced: list[PaperFill] = []
        for event in due:
            if event.kind == "trade":
                produced.extend(self._on_trade(event.payload, event.apply_at_ms))
            elif event.kind == "size":
                self._on_size(event.payload)
        return produced

    def _visible(self, order: PaperResting, ts_ms: int) -> bool:
        return ts_ms >= order.visible_at_ms and order.remaining > 0

    def _on_trade(self, trade: PublicTrade, apply_ms: int) -> list[PaperFill]:
        out: list[PaperFill] = []
        for order in list(self.orders.values()):
            if order.market_ticker != trade.market_ticker:
                continue
            if not self._visible(order, apply_ms):
                continue
            if not _trade_hits(order, trade):
                continue
            through = _trade_through(order, trade)
            eaten = trade.count
            if order.queue_ahead > 0:
                take_q = min(order.queue_ahead, eaten)
                order.queue_ahead -= take_q
                eaten -= take_q
            if through and order.queue_ahead > 0:
                # Print beyond our price implies the level was swept.
                order.queue_ahead = Decimal("0")
            if eaten <= 0 and not through:
                continue
            fill_qty = order.remaining if through else min(order.remaining, eaten)
            if fill_qty <= 0:
                continue
            drag = fee_drag(
                order.price,
                fill_qty,
                is_taker=False,
                fee_type=self.fee_type,
                multiplier=self.fee_multiplier,
            )
            fill = PaperFill(
                order_id=order.order_id,
                market_ticker=order.market_ticker,
                event_ticker=order.event_ticker,
                outcome=order.outcome,
                price=order.price,
                count=fill_qty,
                fee=drag.charged,
                ts_ms=apply_ms,
                latency_ms=order.latency_ms,
                reason="trade_at_or_through" if through else "trade_at_level",
            )
            order.remaining -= fill_qty
            if order.remaining <= 0:
                self.orders.pop(order.order_id, None)
            self.fills.append(fill)
            out.append(fill)
            log.info(
                "paper_fill",
                order_id=fill.order_id,
                ticker=fill.market_ticker,
                outcome=fill.outcome.value,
                price=str(fill.price),
                count=str(fill.count),
                fee=str(fill.fee),
                maker_fee_assumed=str(drag.assumed_maker),
                pending_demo_fill_confirm=drag.pending_demo_confirm,
                latency_ms=fill.latency_ms,
                reason=fill.reason,
            )
        return out

    def _on_size(self, payload: dict[str, Any]) -> None:
        ticker = payload["ticker"]
        outcome: Outcome = payload["outcome"]
        price: Decimal = payload["price"]
        old: Decimal = payload["old"]
        new: Decimal = payload["new"]
        if new >= old:
            return
        decrease = old - new
        for order in list(self.orders.values()):
            if (
                order.market_ticker != ticker
                or order.outcome is not outcome
                or order.price != price
            ):
                continue
            # Ambiguous wipe: never fill from a size drop.
            self.ambiguous_wipes += 1
            order.queue_ahead = max(Decimal("0"), order.queue_ahead - decrease)
            event = {
                "order_id": order.order_id,
                "ticker": ticker,
                "price": str(price),
                "decrease": str(decrease),
                "queue_ahead": str(order.queue_ahead),
                "remaining": str(order.remaining),
            }
            self.wipe_events.append(event)
            log.info("paper_ambiguous_wipe", **event)
            if new <= 0 and order.queue_ahead <= 0:
                self.cancel(order.order_id, reason="level_wiped_no_trade")

    def to_portfolio_fill(self, fill: PaperFill) -> Fill:
        return Fill(
            fill_id=f"paper-{fill.order_id}-{fill.ts_ms}",
            order_id=fill.order_id,
            market_ticker=fill.market_ticker,
            event_ticker=fill.event_ticker,
            outcome=fill.outcome,
            price=fill.price,
            count=fill.count,
            fee=fill.fee,
            is_taker=False,
            ts_ms=fill.ts_ms,
        )


def _trade_hits(order: PaperResting, trade: PublicTrade) -> bool:
    if order.outcome is Outcome.YES:
        return trade.taker_outcome is Outcome.NO and trade.yes_price <= order.price
    return trade.taker_outcome is Outcome.YES and trade.no_price <= order.price


def _trade_through(order: PaperResting, trade: PublicTrade) -> bool:
    if order.outcome is Outcome.YES:
        return trade.yes_price < order.price
    return trade.no_price < order.price
