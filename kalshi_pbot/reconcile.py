"""Startup reconcile: exchange snapshot before any new risk.

Watchdog restarts the process only. Windows, tape, and the daily-loss
kill latch persist on disk; paper positions, unpaired inventory, resting
quotes, and session PnL do not. For demo-submit / live, Kalshi is the
source of truth — the bot must rebuild risk from GET /portfolio/positions
and GET /portfolio/orders before quoting.

Fail-closed: ready_to_trade stays false on auth, network, or partial
snapshot errors. Retry with backoff. Never quote blind.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import structlog

from kalshi_pbot.config import Settings
from kalshi_pbot.execution import ExecutionEngine
from kalshi_pbot.portfolio import Portfolio
from kalshi_pbot.tape import JsonlTape
from kalshi_pbot.types import (
    D,
    Fill,
    Liquidity,
    OrderBook,
    Outcome,
    Position,
    QuoteIntent,
    RestingOrder,
)

log = structlog.get_logger(__name__)

SOURCE_PAPER_LOCAL = "PAPER LOCAL"
SOURCE_EXCHANGE_SYNC = "EXCHANGE SYNC"
STATUS_RECONCILING = "RECONCILING"
STATUS_NOT_READY = "NOT READY"
STATUS_READY = "READY"

RECONCILE_RETRY_BASE_SECONDS = 2.0
RECONCILE_RETRY_CAP_SECONDS = 60.0
PAGINATE_MAX_PAGES = 50


class PortfolioRest(Protocol):
    def list_market_positions(self) -> list[dict[str, Any]]: ...
    def list_resting_orders(self) -> list[dict[str, Any]]: ...
    def cancel_order(self, order_id: str, market_ticker: str) -> Any: ...


@dataclass
class ExchangeSnapshot:
    positions: list[dict[str, Any]]
    orders: list[dict[str, Any]]


@dataclass
class ReconcileState:
    ready_to_trade: bool = False
    status: str = STATUS_RECONCILING
    source: str = ""
    error: str = ""
    attempts: int = 0
    next_retry_ts: datetime | None = None
    last_ok_ts: datetime | None = None
    position_count: int = 0
    resting_count: int = 0
    orphan_count: int = 0
    cancelled_orphans: int = 0
    paper_fills_restored: int = 0
    paper_quotes_restored: int = 0

    def as_hud(self, *, cancel_orphans: bool) -> dict[str, Any]:
        return {
            "ready_to_trade": self.ready_to_trade,
            "status": self.status,
            "source": self.source,
            "error": self.error,
            "attempts": self.attempts,
            "position_count": self.position_count,
            "resting_count": self.resting_count,
            "orphan_count": self.orphan_count,
            "cancelled_orphans": self.cancelled_orphans,
            "cancel_orphans": cancel_orphans,
            "paper_fills_restored": self.paper_fills_restored,
            "paper_quotes_restored": self.paper_quotes_restored,
            "next_retry_ts": self.next_retry_ts.isoformat() if self.next_retry_ts else None,
        }


@dataclass
class TapeRestore:
    fills: int = 0
    resting: int = 0
    settlements: int = 0
    records: int = 0
    owned_ids: set[str] = field(default_factory=set)


def retry_delay_seconds(attempts: int) -> float:
    """Exponential backoff with jitter. attempts is 1-based."""
    spread = RECONCILE_RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1))
    jittered = spread * (0.5 + random.random() * 0.5)
    return min(RECONCILE_RETRY_CAP_SECONDS, jittered)


def may_cancel_orphans(settings: Settings) -> bool:
    """Default safe: never auto-cancel. Demo-submit may opt in. Production never."""
    if not settings.cancel_orphans:
        return False
    if settings.env == "production" or settings.allow_production:
        log.warning(
            "cancel_orphans_refused_production",
            env=settings.env,
            allow_production=settings.allow_production,
        )
        return False
    if not settings.live_submit:
        log.info("cancel_orphans_ignored_not_demo_submit")
        return False
    return True


def is_watched_ticker(
    ticker: str,
    series_tickers: tuple[str, ...],
    live_tickers: set[str] | None = None,
) -> bool:
    if live_tickers and ticker in live_tickers:
        return True
    upper = ticker.upper()
    return any(upper.startswith(series.upper()) for series in series_tickers)


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _record_date(record: dict[str, Any]) -> date | None:
    parsed = _parse_ts(record.get("ts"))
    return parsed.date() if parsed else None


def outcome_from_order(raw: dict[str, Any]) -> Outcome:
    side = str(raw.get("outcome_side") or "").lower()
    if side == "yes":
        return Outcome.YES
    if side == "no":
        return Outcome.NO
    legacy = str(raw.get("side") or "").lower()
    if legacy in {"yes", "bid"}:
        return Outcome.YES
    if legacy in {"no", "ask"}:
        return Outcome.NO
    book = str(raw.get("book_side") or "").lower()
    if book == "bid":
        return Outcome.YES
    if book == "ask":
        return Outcome.NO
    log.warning(
        "reconcile_order_outcome_unknown",
        order_id=raw.get("order_id"),
        body=str(raw)[:200],
    )
    return Outcome.YES


def price_from_order(raw: dict[str, Any], outcome: Outcome) -> Decimal | None:
    if outcome is Outcome.YES:
        value = raw.get("yes_price_dollars") or raw.get("yes_price") or raw.get("price")
        return D(value) if value not in (None, "") else None
    value = raw.get("no_price_dollars") or raw.get("no_price")
    if value not in (None, ""):
        return D(value)
    yes = raw.get("yes_price_dollars") or raw.get("price")
    if yes not in (None, ""):
        return (Decimal("1") - D(yes)).quantize(Decimal("0.0001"))
    return None


def position_from_api(
    raw: dict[str, Any],
    *,
    event_lookup: dict[str, str],
    now: datetime,
) -> Position | None:
    ticker = str(raw.get("ticker") or "")
    if not ticker:
        return None
    raw_qty = raw.get("position_fp")
    if raw_qty in (None, ""):
        raw_qty = raw.get("position") or 0
    qty = D(raw_qty)
    exposure = D(
        raw.get("market_exposure_dollars")
        if raw.get("market_exposure_dollars") not in (None, "")
        else raw.get("market_exposure") or 0
    )
    yes_qty = no_qty = yes_cost = no_cost = Decimal("0")
    if qty > 0:
        yes_qty = qty
        yes_cost = exposure
    elif qty < 0:
        no_qty = abs(qty)
        no_cost = exposure
    else:
        return None
    event_ticker = str(raw.get("event_ticker") or event_lookup.get(ticker) or ticker)
    unpaired_since = _parse_ts(raw.get("last_updated_ts")) or now
    return Position(
        market_ticker=ticker,
        event_ticker=event_ticker,
        yes_qty=yes_qty,
        no_qty=no_qty,
        yes_cost=yes_cost,
        no_cost=no_cost,
        realized_pnl=D(raw.get("realized_pnl_dollars") or 0),
        fees=D(raw.get("fees_paid_dollars") or 0),
        unpaired_since=unpaired_since if (yes_qty != no_qty) else None,
    )


def resting_from_api(
    raw: dict[str, Any],
    *,
    event_lookup: dict[str, str],
) -> RestingOrder | None:
    status = str(raw.get("status") or "resting").lower()
    if status not in {"resting", "open", "pending"}:
        return None
    remaining = D(
        raw.get("remaining_count_fp")
        if raw.get("remaining_count_fp") not in (None, "")
        else raw.get("remaining_count") or raw.get("remaining") or 0
    )
    if remaining <= 0:
        return None
    ticker = str(raw.get("ticker") or "")
    if not ticker:
        return None
    outcome = outcome_from_order(raw)
    price = price_from_order(raw, outcome)
    if price is None or price <= 0 or price >= 1:
        log.warning("reconcile_skip_order_bad_price", order_id=raw.get("order_id"), ticker=ticker)
        return None
    order_id = str(raw.get("order_id") or raw.get("client_order_id") or "")
    if not order_id:
        return None
    return RestingOrder(
        order_id=order_id,
        client_order_id=str(raw.get("client_order_id") or order_id),
        market_ticker=ticker,
        event_ticker=str(raw.get("event_ticker") or event_lookup.get(ticker) or ticker),
        outcome=outcome,
        price=price,
        remaining=remaining,
        post_only=bool(raw.get("post_only", True)),
    )


def _fill_from_tape(record: dict[str, Any]) -> Fill | None:
    ticker = str(record.get("ticker") or "")
    if not ticker:
        return None
    outcome_raw = str(record.get("outcome") or "yes").lower()
    outcome = Outcome.YES if outcome_raw == "yes" else Outcome.NO
    ts = _parse_ts(record.get("ts"))
    ts_ms = int(ts.timestamp() * 1000) if ts else int(record.get("ts_ms") or 0)
    return Fill(
        fill_id=str(record.get("fill_id") or record.get("order_id") or f"tape-{ts_ms}"),
        order_id=str(record.get("order_id") or ""),
        market_ticker=ticker,
        event_ticker=str(record.get("event_ticker") or ticker),
        outcome=outcome,
        price=D(record.get("price") or 0),
        count=D(record.get("count") or 0),
        fee=D(record.get("fee") or 0),
        is_taker=str(record.get("liquidity") or "").lower() == "taker",
        ts_ms=ts_ms,
    )


def restore_from_tape(
    portfolio: Portfolio,
    tape: JsonlTape | None,
    *,
    day: date,
    live_tickers: set[str] | None = None,
) -> TapeRestore:
    """Best-effort paper rebuild from today's tape. Not exchange truth."""
    result = TapeRestore()
    if tape is None:
        return result
    working: dict[str, dict[str, Any]] = {}
    owned: set[str] = set()
    for record in tape.iter_records():
        if _record_date(record) != day:
            continue
        result.records += 1
        kind = str(record.get("kind") or "")
        for key in ("order_id", "client_order_id"):
            if record.get(key):
                owned.add(str(record[key]))
        if kind == "paper_fill":
            fill = _fill_from_tape(record)
            if fill is None:
                continue
            portfolio.apply_fill(fill, enforce_open_cap=False)
            result.fills += 1
            leftover = working.get(fill.order_id)
            if leftover is not None:
                leftover["remaining"] = D(leftover.get("remaining") or 0) - fill.count
                if leftover["remaining"] <= 0:
                    working.pop(fill.order_id, None)
        elif kind == "quote":
            oid = str(record.get("order_id") or "")
            if not oid:
                continue
            working[oid] = {
                **record,
                "remaining": D(record.get("count") or 0),
            }
        elif kind == "cancel":
            working.pop(str(record.get("order_id") or ""), None)
        elif kind == "cancel_all":
            working.clear()
        elif kind == "settlement":
            ticker = str(record.get("ticker") or "")
            raw_result = str(record.get("result") or "").lower()
            settled = (
                Outcome.YES if raw_result == "yes" else Outcome.NO if raw_result == "no" else None
            )
            if ticker and settled is not None:
                portfolio.apply_settlement(ticker, settled)
                result.settlements += 1
                for oid in [k for k, rec in working.items() if str(rec.get("ticker")) == ticker]:
                    working.pop(oid, None)
    for oid, rec in working.items():
        ticker = str(rec.get("ticker") or "")
        remaining = D(rec.get("remaining") or 0)
        if remaining <= 0 or not ticker:
            continue
        if live_tickers and ticker not in live_tickers:
            continue
        outcome_raw = str(rec.get("outcome") or "yes").lower()
        registered = portfolio.upsert_resting(
            RestingOrder(
                order_id=oid,
                client_order_id=str(rec.get("client_order_id") or oid),
                market_ticker=ticker,
                event_ticker=str(rec.get("event_ticker") or ticker),
                outcome=Outcome.YES if outcome_raw == "yes" else Outcome.NO,
                price=D(rec.get("price") or 0),
                remaining=remaining,
                post_only=bool(rec.get("post_only", True)),
            ),
            enforce_open_cap=False,
        )
        if registered:
            result.resting += 1
    result.owned_ids = owned
    return result


def restore_matcher(
    execution: ExecutionEngine | None,
    portfolio: Portfolio,
    books: dict[str, OrderBook] | None,
    now_ms: int,
) -> int:
    """Re-join paper resting quotes in the local matcher after a tape restore."""
    if execution is None or not execution.settings.paper_tape:
        return 0
    placed = 0
    books = books or {}
    for order in portfolio.resting.values():
        if not str(order.order_id).startswith("paper-"):
            continue
        if order.order_id in execution.matcher.orders:
            continue
        book = books.get(order.market_ticker) or OrderBook(ticker=order.market_ticker)
        intent = QuoteIntent(
            market_ticker=order.market_ticker,
            event_ticker=order.event_ticker,
            outcome=order.outcome,
            price=order.price,
            count=order.remaining,
            liquidity=Liquidity.MAKER,
            post_only=order.post_only,
            client_order_id=order.order_id,
        )
        execution.matcher.place(intent, book, now_ms)
        placed += 1
    return placed


class Reconciler:
    def __init__(
        self,
        settings: Settings,
        *,
        rest: PortfolioRest | None,
        portfolio: Portfolio,
        tape: JsonlTape | None = None,
        execution: ExecutionEngine | None = None,
        universe: Any | None = None,
        mock: bool = False,
    ) -> None:
        self.settings = settings
        self.rest = rest
        self.portfolio = portfolio
        self.tape = tape
        self.execution = execution
        self.universe = universe
        self.mock = mock
        self.order_books: Any | None = None
        self.state = ReconcileState()
        self.portfolio.ready_to_trade = False
        if self.execution is not None:
            self.execution.ready_to_trade = False

    def maybe_attempt(self, now: datetime | None = None) -> ReconcileState:
        now = now or datetime.now(UTC)
        if self.state.ready_to_trade:
            return self.state
        if self.state.next_retry_ts is not None and now < self.state.next_retry_ts:
            return self.state
        return self.attempt(now)

    def attempt(self, now: datetime | None = None) -> ReconcileState:
        now = now or datetime.now(UTC)
        self.state.attempts += 1
        self.state.status = STATUS_RECONCILING
        self.state.ready_to_trade = False
        self.portfolio.ready_to_trade = False
        if self.execution is not None:
            self.execution.ready_to_trade = False
        log.info(
            "reconcile_start",
            attempt=self.state.attempts,
            mock=self.mock,
            paper_tape=self.settings.paper_tape,
            live_submit=self.settings.live_submit,
            has_rest=self.rest is not None,
        )
        try:
            snapshot, skipped = self._fetch_snapshot()
        except Exception as exc:
            return self._fail(now, f"{type(exc).__name__}: {exc}")

        event_lookup = self._event_lookup()
        live = self._live_tickers()
        try:
            parsed_positions = [
                pos
                for raw in snapshot.positions
                if (pos := position_from_api(raw, event_lookup=event_lookup, now=now)) is not None
            ]
            parsed_orders = [
                order
                for raw in snapshot.orders
                if (order := resting_from_api(raw, event_lookup=event_lookup)) is not None
            ]
        except Exception as exc:
            return self._fail(now, f"parse {type(exc).__name__}: {exc}")

        owned = self._tape_owned_ids(now.date())
        orphans = [
            order
            for order in parsed_orders
            if is_watched_ticker(order.market_ticker, self.settings.series_tickers, live)
            and order.order_id not in owned
            and order.client_order_id not in owned
        ]
        cancelled = 0
        if orphans:
            log.warning(
                "reconcile_unexpected_orders",
                count=len(orphans),
                order_ids=[o.order_id for o in orphans[:12]],
                cancel=may_cancel_orphans(self.settings),
            )
        if orphans and may_cancel_orphans(self.settings):
            cancelled, parsed_orders = self._cancel_orphans(orphans, parsed_orders)

        exchange_occupied = bool(parsed_positions or parsed_orders)
        tape_restore = TapeRestore()
        # Startup is an empty book. Do not wipe in-process fills/PnL that tests
        # (or a same-process HUD) already applied before the first step().
        already = bool(
            self.portfolio.positions
            or self.portfolio.resting
            or self.portfolio.fills
            or self.portfolio.realized_pnl
            or self.portfolio.fees
        )
        if skipped or not exchange_occupied:
            if self.settings.paper_tape or self.mock:
                if not already:
                    self.portfolio.clear_inventory()
                    tape_restore = restore_from_tape(
                        self.portfolio,
                        self.tape,
                        day=now.date(),
                        live_tickers=live or None,
                    )
                    restore_matcher(
                        self.execution,
                        self.portfolio,
                        self._books(),
                        int(now.timestamp() * 1000),
                    )
                source = SOURCE_PAPER_LOCAL
            else:
                self.portfolio.replace_exchange_inventory(parsed_positions, parsed_orders)
                source = SOURCE_EXCHANGE_SYNC
        else:
            self.portfolio.replace_exchange_inventory(parsed_positions, parsed_orders)
            source = SOURCE_EXCHANGE_SYNC
            if self.settings.paper_tape:
                log.info(
                    "reconcile_tape_skipped_exchange_occupied",
                    positions=len(parsed_positions),
                    resting=len(parsed_orders),
                )

        self._mark_ready(
            now,
            source=source,
            positions=len(self.portfolio.positions),
            resting=len(self.portfolio.resting),
            orphans=len(orphans),
            cancelled=cancelled,
            tape_restore=tape_restore,
        )
        return self.state

    def _fetch_snapshot(self) -> tuple[ExchangeSnapshot, bool]:
        """Return (snapshot, skipped). skipped=True means no exchange read was required."""
        if self.mock:
            return ExchangeSnapshot(positions=[], orders=[]), True
        if self.rest is None:
            if self.settings.live_submit:
                raise RuntimeError("demo-submit reconcile needs an authenticated REST client")
            return ExchangeSnapshot(positions=[], orders=[]), True
        positions = self.rest.list_market_positions()
        orders = self.rest.list_resting_orders()
        if positions is None or orders is None:
            raise RuntimeError("partial exchange snapshot (positions or orders missing)")
        return ExchangeSnapshot(positions=list(positions), orders=list(orders)), False

    def _cancel_orphans(
        self,
        orphans: list[RestingOrder],
        parsed_orders: list[RestingOrder],
    ) -> tuple[int, list[RestingOrder]]:
        assert self.rest is not None
        cancelled_ids: set[str] = set()
        for order in orphans:
            try:
                response = self.rest.cancel_order(order.order_id, order.market_ticker)
                status = getattr(response, "status_code", 200)
                if status in {200, 204}:
                    cancelled_ids.add(order.order_id)
                    log.info(
                        "reconcile_cancel_orphan",
                        order_id=order.order_id,
                        ticker=order.market_ticker,
                    )
                else:
                    log.warning(
                        "reconcile_cancel_orphan_failed",
                        order_id=order.order_id,
                        status=status,
                    )
            except Exception:
                log.exception("reconcile_cancel_orphan_error", order_id=order.order_id)
        kept = [order for order in parsed_orders if order.order_id not in cancelled_ids]
        return len(cancelled_ids), kept

    def _mark_ready(
        self,
        now: datetime,
        *,
        source: str,
        positions: int,
        resting: int,
        orphans: int,
        cancelled: int,
        tape_restore: TapeRestore,
    ) -> None:
        self.state.ready_to_trade = True
        self.state.status = STATUS_READY
        self.state.source = source
        self.state.error = ""
        self.state.next_retry_ts = None
        self.state.last_ok_ts = now
        self.state.position_count = positions
        self.state.resting_count = resting
        self.state.orphan_count = orphans
        self.state.cancelled_orphans = cancelled
        self.state.paper_fills_restored = tape_restore.fills
        self.state.paper_quotes_restored = tape_restore.resting
        self.portfolio.ready_to_trade = True
        if self.execution is not None:
            self.execution.ready_to_trade = True
        if self.tape:
            self.tape.write(
                "reconcile",
                source=source,
                status=STATUS_READY,
                positions=positions,
                resting=resting,
                orphans=orphans,
                cancelled_orphans=cancelled,
                paper_fills=tape_restore.fills,
                paper_quotes=tape_restore.resting,
            )
        log.info(
            "reconcile_ok",
            source=source,
            positions=positions,
            resting=resting,
            orphans=orphans,
            cancelled_orphans=cancelled,
            paper_fills=tape_restore.fills,
            paper_quotes=tape_restore.resting,
        )

    def _fail(self, now: datetime, error: str) -> ReconcileState:
        delay = retry_delay_seconds(self.state.attempts)
        self.state.ready_to_trade = False
        self.state.status = STATUS_NOT_READY
        self.state.source = ""
        self.state.error = error
        self.state.next_retry_ts = now + timedelta(seconds=delay)
        self.portfolio.ready_to_trade = False
        if self.execution is not None:
            self.execution.ready_to_trade = False
        log.warning(
            "reconcile_failed",
            error=error,
            attempt=self.state.attempts,
            retry_s=round(delay, 3),
            hint="ready_to_trade=false; refusing new quotes until exchange snapshot succeeds",
        )
        if self.tape:
            self.tape.write(
                "reconcile_failed",
                error=error,
                attempt=self.state.attempts,
                retry_s=round(delay, 3),
            )
        return self.state

    def _live_tickers(self) -> set[str]:
        universe = self.universe
        if universe is None:
            return set()
        tickers = set(getattr(universe, "markets", {}) or {})
        tickers.update(getattr(universe, "settling", {}) or {})
        return tickers

    def _event_lookup(self) -> dict[str, str]:
        lookup: dict[str, str] = {}
        universe = self.universe
        if universe is None:
            return lookup
        for bucket in (getattr(universe, "markets", {}), getattr(universe, "settling", {})):
            for ticker, market in (bucket or {}).items():
                lookup[str(ticker)] = str(getattr(market, "event_ticker", ticker) or ticker)
        return lookup

    def _books(self) -> dict[str, OrderBook]:
        store = self.order_books
        if store is None:
            return {}
        books: dict[str, OrderBook] = {}
        for ticker in self._live_tickers():
            book = store.get(ticker) if hasattr(store, "get") else None
            if book is not None:
                books[ticker] = book
        return books

    def _tape_owned_ids(self, day: date) -> set[str]:
        if self.tape is None:
            return set()
        owned: set[str] = set()
        for record in self.tape.iter_records():
            if _record_date(record) != day:
                continue
            for key in ("order_id", "client_order_id"):
                if record.get(key):
                    owned.add(str(record[key]))
        return owned
