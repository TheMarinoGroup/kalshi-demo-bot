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
        self.day_high_pnl = Decimal("0")
        self.locked_pair_realized = Decimal("0")
        self.directional_settled = Decimal("0")
        self.paired_qty_realized = Decimal("0")
        self.directional_qty_settled = Decimal("0")
        self.maker_fees = Decimal("0")
        self.taker_fees = Decimal("0")
        self.maker_fill_count = 0
        self.taker_fill_count = 0
        self.maker_fill_notional = Decimal("0")
        self.taker_fill_notional = Decimal("0")
        # Startup reconcile gate. False until PaperBot marks an exchange/local snapshot ok.
        self.ready_to_trade = True

    def reset_day_if_needed(self, now: datetime | None = None) -> None:
        today = (now or datetime.now(UTC)).date()
        if today != self.day:
            self.day = today
            self.realized_pnl = Decimal("0")
            self.fees = Decimal("0")
            self.day_high_pnl = Decimal("0")
            self.locked_pair_realized = Decimal("0")
            self.directional_settled = Decimal("0")
            self.paired_qty_realized = Decimal("0")
            self.directional_qty_settled = Decimal("0")
            self.maker_fees = Decimal("0")
            self.taker_fees = Decimal("0")
            self.maker_fill_count = 0
            self.taker_fill_count = 0
            self.maker_fill_notional = Decimal("0")
            self.taker_fill_notional = Decimal("0")
            for pos in self.positions.values():
                pos.realized_pnl = Decimal("0")
                pos.fees = Decimal("0")

    def upsert_resting(self, order: RestingOrder, *, enforce_open_cap: bool = True) -> bool:
        """Register working size. Refuses when reserved+cost would exceed max_open."""
        if enforce_open_cap:
            current = self.snapshot().open_notional
            existing = self.resting.get(order.order_id)
            if existing is not None:
                current -= existing.reserved_notional
            if current + order.reserved_notional > self.settings.max_open_notional:
                log.info(
                    "resting_rejected_open_cap",
                    order_id=order.order_id,
                    ticker=order.market_ticker,
                    projected=str(current + order.reserved_notional),
                    max_open=str(self.settings.max_open_notional),
                )
                return False
        self.resting[order.order_id] = order
        return True

    def drop_resting(self, order_id: str) -> RestingOrder | None:
        return self.resting.pop(order_id, None)

    def resting_for(self, ticker: str) -> list[RestingOrder]:
        return [o for o in self.resting.values() if o.market_ticker == ticker]

    def clear_inventory(self) -> None:
        """Drop positions, resting, and session fills. Used before a tape rebuild."""
        self.positions.clear()
        self.resting.clear()
        self.fills.clear()
        self.realized_pnl = Decimal("0")
        self.fees = Decimal("0")
        self.locked_pair_realized = Decimal("0")
        self.directional_settled = Decimal("0")
        self.paired_qty_realized = Decimal("0")
        self.directional_qty_settled = Decimal("0")
        self.maker_fees = Decimal("0")
        self.taker_fees = Decimal("0")
        self.maker_fill_count = 0
        self.taker_fill_count = 0
        self.maker_fill_notional = Decimal("0")
        self.taker_fill_notional = Decimal("0")

    def replace_exchange_inventory(
        self,
        positions: list[Position],
        resting: list[RestingOrder],
    ) -> None:
        """Replace in-memory book with an exchange snapshot. Does not enforce caps.

        Daily PnL inputs come from the current-market position records
        (``realized_pnl`` + ``fees``). This is not a full account-lifetime
        blotter. Does **not** touch kill flags or the persisted kill latch.
        """
        self.positions = {
            pos.market_ticker: pos
            for pos in positions
            if pos.yes_qty > 0 or pos.no_qty > 0
        }
        self.resting = {}
        for order in resting:
            self.upsert_resting(order, enforce_open_cap=False)
        self.realized_pnl = sum(
            (pos.realized_pnl for pos in self.positions.values()), Decimal("0")
        )
        self.fees = sum((pos.fees for pos in self.positions.values()), Decimal("0"))

    def apply_fill(self, fill: Fill, *, enforce_open_cap: bool = True) -> bool:
        """Apply a paper/demo fill. Refuses before mutation if open would exceed max_open."""
        self.reset_day_if_needed()
        if fill.count <= 0 or fill.price <= 0:
            return False
        if enforce_open_cap:
            projected = self.projected_open_after_fill(fill)
            if projected > self.settings.max_open_notional:
                log.info(
                    "fill_refused_open_cap",
                    ticker=fill.market_ticker,
                    order_id=fill.order_id,
                    projected=str(projected),
                    max_open=str(self.settings.max_open_notional),
                    notional=str(fill.price * fill.count),
                )
                leftover = self.resting.get(fill.order_id)
                if leftover:
                    self.drop_resting(fill.order_id)
                return False
        self.fills.append(fill)
        pos = self.positions.get(fill.market_ticker)
        if pos is None:
            pos = Position(market_ticker=fill.market_ticker, event_ticker=fill.event_ticker)
            self.positions[fill.market_ticker] = pos

        prev_unpaired = pos.unpaired_outcome
        notional = fill.price * fill.count
        if fill.outcome is Outcome.YES:
            pos.yes_qty += fill.count
            pos.yes_cost += notional
        else:
            pos.no_qty += fill.count
            pos.no_cost += notional

        pos.fees += fill.fee
        self.fees += fill.fee
        if fill.is_taker:
            self.taker_fees += fill.fee
            self.taker_fill_count += 1
            self.taker_fill_notional += notional
        else:
            self.maker_fees += fill.fee
            self.maker_fill_count += 1
            self.maker_fill_notional += notional

        # Realize locked pair as soon as both legs exist.
        paired = pos.paired_qty
        if paired > 0:
            yes_px = pos.avg_yes() or Decimal("0")
            no_px = pos.avg_no() or Decimal("0")
            locked = paired * (Decimal("1") - yes_px - no_px)
            pos.realized_pnl += locked
            self.realized_pnl += locked
            self.locked_pair_realized += locked
            self.paired_qty_realized += paired
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

        if pos.unpaired_qty <= 0:
            pos.unpaired_since = None
        elif pos.unpaired_outcome != prev_unpaired or pos.unpaired_since is None:
            filled_at = (
                datetime.fromtimestamp(fill.ts_ms / 1000, tz=UTC)
                if fill.ts_ms
                else datetime.now(UTC)
            )
            pos.unpaired_since = filled_at

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
        return True

    def projected_open_after_fill(self, fill: Fill) -> Decimal:
        """Gross open (cost + reserved) if this fill is applied, including pairing."""
        costs = {ticker: pos.cost_basis() for ticker, pos in self.positions.items()}
        yes_qty = fill.count if fill.outcome is Outcome.YES else Decimal("0")
        no_qty = fill.count if fill.outcome is Outcome.NO else Decimal("0")
        yes_cost = fill.price * fill.count if fill.outcome is Outcome.YES else Decimal("0")
        no_cost = fill.price * fill.count if fill.outcome is Outcome.NO else Decimal("0")
        pos = self.positions.get(fill.market_ticker)
        if pos is not None:
            yes_qty += pos.yes_qty
            no_qty += pos.no_qty
            yes_cost += pos.yes_cost
            no_cost += pos.no_cost
        paired = min(yes_qty, no_qty)
        if paired > 0:
            yes_px = (yes_cost / yes_qty) if yes_qty else Decimal("0")
            no_px = (no_cost / no_qty) if no_qty else Decimal("0")
            yes_qty -= paired
            no_qty -= paired
            yes_cost -= yes_px * paired
            no_cost -= no_px * paired
            if yes_qty <= 0:
                yes_qty = yes_cost = Decimal("0")
            if no_qty <= 0:
                no_qty = no_cost = Decimal("0")
        costs[fill.market_ticker] = yes_cost + no_cost
        cost = sum(costs.values(), Decimal("0"))
        reserved = Decimal("0")
        for oid, order in self.resting.items():
            remaining = order.remaining - fill.count if oid == fill.order_id else order.remaining
            if remaining > 0:
                reserved += order.price * remaining
        return reserved + cost

    def apply_settlement(
        self,
        ticker: str,
        result: Outcome,
        payout: Decimal = Decimal("1"),
        *,
        close_time: datetime | None = None,
        now: datetime | None = None,
        settlement_ts: datetime | None = None,
    ) -> None:
        """Realize leftover inventory and free the window for recycle.

        Recycle is gated by close+settle_recycle_seconds (caller). This
        method does not read expected_expiration.
        """
        now = now or datetime.now(UTC)
        for order in list(self.resting_for(ticker)):
            self.drop_resting(order.order_id)
        pos = self.positions.get(ticker)
        pnl = Decimal("0")
        leftover_qty = Decimal("0")
        if pos is not None:
            leftover_qty = pos.yes_qty + pos.no_qty
            if result is Outcome.YES:
                pnl = pos.yes_qty * payout - pos.yes_cost - pos.no_cost
            else:
                pnl = pos.no_qty * payout - pos.yes_cost - pos.no_cost
            pos.realized_pnl += pnl
            self.realized_pnl += pnl
            self.directional_settled += pnl
            self.directional_qty_settled += leftover_qty
            pos.yes_qty = pos.no_qty = Decimal("0")
            pos.yes_cost = pos.no_cost = Decimal("0")
            pos.unpaired_since = None
        recycle_s = None
        settle_lag_s = None
        if close_time is not None:
            close = close_time if close_time.tzinfo else close_time.replace(tzinfo=UTC)
            recycle_s = (now - close).total_seconds()
        if close_time is not None and settlement_ts is not None:
            close = close_time if close_time.tzinfo else close_time.replace(tzinfo=UTC)
            settled = settlement_ts if settlement_ts.tzinfo else settlement_ts.replace(tzinfo=UTC)
            settle_lag_s = (settled - close).total_seconds()
        log.info(
            "settlement_applied",
            ticker=ticker,
            result=result.value,
            pnl=str(pnl),
            recycle_s=recycle_s,
            close_to_settlement_s=settle_lag_s,
            settle_lock="close+recycle_seconds",
        )

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
        if daily > self.day_high_pnl:
            self.day_high_pnl = daily
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
            ready_to_trade=self.ready_to_trade,
        )
