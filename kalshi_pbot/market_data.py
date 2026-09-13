"""Events-first discovery, persisted windows, bids-only book rebuild."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from kalshi_pbot.config import Settings
from kalshi_pbot.kalshi_client import MarketSource
from kalshi_pbot.risk_engine import in_last_seconds, recycle_ready
from kalshi_pbot.series import is_supported_window
from kalshi_pbot.types import (
    CFBTick,
    D,
    MarketWindow,
    OrderBook,
    Outcome,
    PriceLevel,
    PublicTrade,
    TopOfBook,
)
from kalshi_pbot.windows import WindowStore

log = structlog.get_logger(__name__)

_DEAD_STATUSES = {"closed", "settled", "finalized", "determined"}


def is_live_window(market: MarketWindow, now: datetime) -> bool:
    if market.status in _DEAD_STATUSES:
        return False
    return market.open_time <= now < market.close_time


def parse_book_levels(rows: list[list[str]] | None) -> list[PriceLevel]:
    levels: list[PriceLevel] = []
    for row in rows or []:
        if not row or len(row) < 2:
            continue
        price = D(row[0])
        size = D(row[1])
        if size <= 0:
            continue
        levels.append(PriceLevel(price=price, size=size))
    levels.sort(key=lambda lvl: lvl.price)
    return levels


@dataclass(frozen=True)
class BookTouch:
    ticker: str
    outcome: Outcome
    price: Decimal
    old_size: Decimal
    new_size: Decimal


def _msg_ts_ms(body: dict[str, Any]) -> int:
    if body.get("ts_ms"):
        return int(body["ts_ms"])
    for key in ("created_time", "ts", "timestamp"):
        raw = body.get(key)
        if not raw:
            continue
        if isinstance(raw, (int, float)):
            value = float(raw)
            return int(value if value > 1e12 else value * 1000)
        try:
            return int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            continue
    return int(datetime.now(UTC).timestamp() * 1000)


def parse_public_trade(message: dict[str, Any]) -> PublicTrade | None:
    body = message.get("msg") or message
    ticker = body.get("market_ticker") or body.get("ticker")
    if not ticker:
        return None
    yes = D(body.get("yes_price_dollars") or body.get("yes_price") or body.get("price") or 0)
    no = D(body.get("no_price_dollars") or body.get("no_price") or 0)
    if no <= 0 and 0 < yes < 1:
        no = Decimal("1") - yes
    if yes <= 0:
        return None
    count = D(body.get("count_fp") or body.get("count") or 0)
    if count <= 0:
        return None
    taker_raw = str(body.get("taker_side") or body.get("taker_outcome") or "").lower()
    taker = Outcome.YES if taker_raw in {"yes", "bid"} else Outcome.NO
    return PublicTrade(
        trade_id=str(body.get("trade_id") or body.get("id") or ""),
        market_ticker=str(ticker),
        yes_price=yes,
        no_price=no,
        count=count,
        taker_outcome=taker,
        ts_ms=_msg_ts_ms(body),
        is_block=bool(body.get("is_block") or body.get("block_trade")),
    )


def parse_cfb_tick(message: dict[str, Any]) -> CFBTick | None:
    body = message.get("msg") or message
    index_id = body.get("index_id") or body.get("symbol")
    if not index_id:
        return None
    value = D(body.get("value") or body.get("price") or 0)
    avg = body.get("avg_60s") or body.get("average_60s")
    settle = body.get("settle_avg") or body.get("settlement_average")
    return CFBTick(
        index_id=str(index_id),
        value=value,
        avg_60s=D(avg) if avg is not None else None,
        settle_avg=D(settle) if settle is not None else None,
        received_at_ms=_msg_ts_ms(body),
    )


def book_from_rest(ticker: str, payload: dict[str, Any]) -> OrderBook:
    fp = payload.get("orderbook_fp") or payload.get("orderbook") or payload
    yes = fp.get("yes_dollars") or fp.get("yes_dollars_fp") or fp.get("yes") or []
    no = fp.get("no_dollars") or fp.get("no_dollars_fp") or fp.get("no") or []
    return OrderBook(ticker=ticker, yes_bids=parse_book_levels(yes), no_bids=parse_book_levels(no))


class OrderBookStore:
    """Live books: REST snapshot plus WS snapshot/delta."""

    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}

    def get(self, ticker: str) -> OrderBook | None:
        return self._books.get(ticker)

    def set(self, book: OrderBook) -> None:
        self._books[book.ticker] = book

    def tob(self, ticker: str) -> TopOfBook | None:
        book = self._books.get(ticker)
        return book.tob() if book else None

    def apply_snapshot(self, msg: dict[str, Any]) -> OrderBook:
        body = msg.get("msg") or msg
        ticker = body["market_ticker"]
        yes = body.get("yes_dollars_fp") or body.get("yes_dollars") or []
        no = body.get("no_dollars_fp") or body.get("no_dollars") or []
        book = OrderBook(
            ticker=ticker,
            yes_bids=parse_book_levels(yes),
            no_bids=parse_book_levels(no),
            seq=int(msg.get("seq") or 0),
        )
        self.set(book)
        return book

    def apply_delta(self, msg: dict[str, Any]) -> tuple[OrderBook | None, Decimal, Decimal]:
        """Return (book, old_size, new_size) at the touched level."""
        body = msg.get("msg") or msg
        ticker = body["market_ticker"]
        book = self._books.get(ticker)
        if book is None:
            return None, Decimal("0"), Decimal("0")
        seq = int(msg.get("seq") or 0)
        if book.seq and seq and seq != book.seq + 1:
            log.warning("book_seq_gap", ticker=ticker, have=book.seq, got=seq)
        side = str(body.get("side") or "").lower()
        price = D(body["price_dollars"])
        delta = D(body["delta_fp"])
        outcome = Outcome.YES if side == "yes" else Outcome.NO
        old = book.size_at(outcome, price)
        levels = book.yes_bids if outcome is Outcome.YES else book.no_bids
        updated = _apply_level_delta(levels, price, delta)
        if outcome is Outcome.YES:
            book.yes_bids = updated
        else:
            book.no_bids = updated
        book.seq = seq or book.seq
        new = book.size_at(outcome, price)
        return book, old, new

    def handle_ws(self, message: dict[str, Any]) -> BookTouch | None:
        kind = message.get("type")
        if kind == "orderbook_snapshot":
            self.apply_snapshot(message)
            return None
        if kind == "orderbook_delta":
            body = message.get("msg") or message
            book, old, new = self.apply_delta(message)
            if book is None:
                return None
            side = str(body.get("side") or "").lower()
            outcome = Outcome.YES if side == "yes" else Outcome.NO
            return BookTouch(
                ticker=book.ticker,
                outcome=outcome,
                price=D(body["price_dollars"]),
                old_size=old,
                new_size=new,
            )
        return None


def _apply_level_delta(
    levels: list[PriceLevel], price: Decimal, delta: Decimal
) -> list[PriceLevel]:
    by_price = {lvl.price: lvl.size for lvl in levels}
    by_price[price] = by_price.get(price, Decimal("0")) + delta
    return [PriceLevel(price=px, size=sz) for px, sz in sorted(by_price.items()) if sz > 0]


class MarketUniverse:
    """Events-first 15m windows. Rollover via GET /events?status=unopened."""

    def __init__(self, settings: Settings, source: MarketSource) -> None:
        self.settings = settings
        self.source = source
        self.store = WindowStore(settings.windows_path)
        self.markets: dict[str, MarketWindow] = {}
        self.upcoming: list[MarketWindow] = []
        self.settling: dict[str, MarketWindow] = {}

    def refresh(self, *, now: datetime | None = None) -> list[MarketWindow]:
        now = now or datetime.now(UTC)
        discovered: list[MarketWindow] = []
        upcoming: list[MarketWindow] = []
        for series in self.settings.series_tickers:
            try:
                opened = self.source.list_events(series, "open")
                unopened = self.source.list_events(series, "unopened")
            except Exception:
                log.exception("discover_failed", series=series)
                opened, unopened = self.source.list_open_markets(series), []
            discovered.extend(opened)
            upcoming.extend(unopened)

        catalog = [
            m
            for m in discovered + upcoming
            if is_supported_window(m, self.settings.min_window_minutes)
        ]
        skipped = len(discovered) + len(upcoming) - len(catalog)
        if skipped:
            log.info(
                "skipped_short_windows",
                count=skipped,
                min_minutes=self.settings.min_window_minutes,
            )
        self.store.upsert(catalog)
        self.store.persist()
        # Live clips often sit under events?status=unopened (market status=active)
        # while events?status=open is the window that just determined.
        self.upcoming = [
            m
            for m in catalog
            if m.open_time > now and m.status not in _DEAD_STATUSES
        ]
        live = [m for m in catalog if is_live_window(m, now)]
        live.sort(key=lambda m: m.close_time)

        chosen: list[MarketWindow] = []
        seen_events: set[str] = set()
        for market in live:
            if market.window_id in seen_events:
                continue
            if len(chosen) >= self.settings.max_windows:
                break
            chosen.append(market)
            seen_events.add(market.window_id)

        previous = dict(self.markets)
        current = {m.ticker: m for m in chosen}
        added = set(current) - set(previous)
        removed = set(previous) - set(current)
        for ticker in removed:
            self.settling[ticker] = previous[ticker]
        for market in catalog:
            if market.ticker in current:
                continue
            if market.close_time <= now:
                prior = self.settling.get(market.ticker)
                if prior is None or market.result or market.settlement_ts:
                    self.settling[market.ticker] = market
        if added or removed or self.upcoming or self.settling:
            log.info(
                "universe_rollover",
                added=sorted(added),
                removed=sorted(removed),
                active=[m.ticker for m in chosen],
                unopened=[m.ticker for m in self.upcoming[:4]],
                settling=[m.ticker for m in self.due_for_recycle(now)],
                persisted=str(self.store.path),
            )
        self.markets = current
        return chosen

    def due_for_recycle(self, now: datetime | None = None) -> list[MarketWindow]:
        now = now or datetime.now(UTC)
        delay = self.settings.effective_settle_recycle_seconds
        return [
            m
            for m in self.settling.values()
            if recycle_ready(m.close_time, delay, now, expected_expiration=m.expected_expiration)
        ]

    def mark_recycled(self, ticker: str) -> MarketWindow | None:
        return self.settling.pop(ticker, None)

    def tradable(self, *, now: datetime | None = None) -> list[MarketWindow]:
        now = now or datetime.now(UTC)
        return [
            m
            for m in self.markets.values()
            if not in_last_seconds(m.close_time, self.settings.last_seconds, now)
        ]

    def flatten_only(self, *, now: datetime | None = None) -> list[MarketWindow]:
        now = now or datetime.now(UTC)
        return [
            m
            for m in self.markets.values()
            if in_last_seconds(m.close_time, self.settings.last_seconds, now)
        ]

    def hydrate_books(self, store: OrderBookStore) -> None:
        for market in self.markets.values():
            try:
                payload = self.source.get_orderbook(market.ticker)
                store.set(book_from_rest(market.ticker, payload))
            except Exception:
                log.exception("orderbook_fetch_failed", ticker=market.ticker)
