"""Local portfolio, fill accounting, and daily PnL."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import structlog

from kalshi_pbot.config import Settings
from kalshi_pbot.types import (
    Fill,
    OrderBook,
    Outcome,
    PortfolioSnapshot,
    Position,
    RestingOrder,
)

log = structlog.get_logger(__name__)


class Portfolio:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.positions: dict[str, Position] = {}
        self.resting: dict[str, RestingOrder] = {}
        self.fills: list[Fill] = []
        self.realized_pnl = Decimal("0")
        self.fees = Decimal("0")
        self.orders_submitted = 0
        self.day = date.today()
        self.kill_active = False
        self.kill_reason = ""

    def reset_day_if_needed(self, now: datetime | None = None) -> None:
        today = (now or datetime.now(UTC)).date()
        if today != self.day:
            self.day = today
            self.realized_pnl = Decimal("0")
            self.fees = Decimal("0")
            for pos in self.positions.values():
                pos.realized_pnl = Decimal("0")
                pos.fees = Decimal("0")

    def upsert_resting(self, order: RestingOrder) -> None:
        self.resting[order.order_id] = order

    def drop_resting(self, order_id: str) -> RestingOrder | None:
        return self.resting.pop(order_id, None)

    def resting_for(self, ticker: str) -> list[RestingOrder]:
        return [o for o in self.resting.values() if o.market_ticker == ticker]

    def apply_fill(self, fill: Fill) -> None:
        self.reset_day_if_needed()
        self.fills.append(fill)
        pos = self.positions.get(fill.market_ticker)
        if pos is None:
            pos = Position(market_ticker=fill.market_ticker, event_ticker=fill.event_ticker)
            self.positions[fill.market_ticker] = pos

        notional = fill.price * fill.count
        if fill.outcome is Outcome.YES:
            pos.yes_qty += fill.count
            pos.yes_cost += notional
        else:
            pos.no_qty += fill.count
            pos.no_cost += notional

        pos.fees += fill.fee
        self.fees += fill.fee

        # Realize locked pair as soon as both legs exist.
        paired = pos.paired_qty
        if paired > 0:
            yes_px = pos.avg_yes() or Decimal("0")
            no_px = pos.avg_no() or Decimal("0")
            locked = paired * (Decimal("1") - yes_px - no_px)
            pos.realized_pnl += locked
            self.realized_pnl += locked
            pos.yes_qty -= paired
            pos.no_qty -= paired
            pos.yes_cost -= yes_px * paired
            pos.no_cost -= no_px * paired
            if pos.yes_qty <= 0:
                pos.yes_qty = Decimal("0")
                pos.yes_cost = Decimal("0")
            if pos.no_qty <= 0:
                pos.no_qty = Decimal("0")
                pos.no_cost = Decimal("0")

        leftover = self.resting.get(fill.order_id)
        if leftover:
            remaining = leftover.remaining - fill.count
            if remaining <= 0:
                self.drop_resting(fill.order_id)
            else:
                self.resting[fill.order_id] = RestingOrder(
                    order_id=leftover.order_id,
                    client_order_id=leftover.client_order_id,
                    market_ticker=leftover.market_ticker,
                    event_ticker=leftover.event_ticker,
                    outcome=leftover.outcome,
                    price=leftover.price,
                    remaining=remaining,
                    post_only=leftover.post_only,
                )

        log.info(
            "fill_applied",
            ticker=fill.market_ticker,
            outcome=fill.outcome.value,
            price=str(fill.price),
            count=str(fill.count),
            fee=str(fill.fee),
            taker=fill.is_taker,
            unpaired=str(pos.unpaired_qty),
        )

    def apply_settlement(
        self, ticker: str, result: Outcome, payout: Decimal = Decimal("1")
    ) -> None:
        pos = self.positions.get(ticker)
        if pos is None:
            return
        if result is Outcome.YES:
            pnl = pos.yes_qty * payout - pos.yes_cost - pos.no_cost
        else:
            pnl = pos.no_qty * payout - pos.yes_cost - pos.no_cost
        pos.realized_pnl += pnl
        self.realized_pnl += pnl
        pos.yes_qty = pos.no_qty = Decimal("0")
        pos.yes_cost = pos.no_cost = Decimal("0")

    def snapshot(self, books: dict[str, OrderBook] | None = None) -> PortfolioSnapshot:
        books = books or {}
        unrealized = Decimal("0")
        unpaired = Decimal("0")
        windows: set[str] = set()
        for ticker, pos in self.positions.items():
            mid = books[ticker].mid_yes() if ticker in books else None
            unrealized += pos.mark_unrealized(mid)
            unpaired += pos.unpaired_notional()
            if pos.yes_qty > 0 or pos.no_qty > 0:
                windows.add(pos.event_ticker)

        reserved = sum((o.reserved_notional for o in self.resting.values()), Decimal("0"))
        cost = sum((p.cost_basis() for p in self.positions.values()), Decimal("0"))
        open_notional = reserved + cost
        for order in self.resting.values():
            windows.add(order.event_ticker)

        daily = self.realized_pnl - self.fees + unrealized
        return PortfolioSnapshot(
            bankroll=self.settings.bankroll,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            fees=self.fees,
            daily_pnl=daily,
            open_notional=open_notional,
            unpaired_notional=unpaired,
            window_ids=frozenset(windows),
            positions=dict(self.positions),
            resting=tuple(self.resting.values()),
            kill_active=self.kill_active,
            kill_reason=self.kill_reason,
        )
