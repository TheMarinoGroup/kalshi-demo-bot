"""Market discovery, rollover, and bids-only order book reconstruction."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from kalshi_pbot.config import Settings
from kalshi_pbot.kalshi_client import MarketSource
from kalshi_pbot.risk_engine import in_last_seconds
from kalshi_pbot.types import D, MarketWindow, OrderBook, PriceLevel

log = structlog.get_logger(__name__)


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

    def apply_delta(self, msg: dict[str, Any]) -> OrderBook | None:
        body = msg.get("msg") or msg
        ticker = body["market_ticker"]
        book = self._books.get(ticker)
        if book is None:
            return None
        seq = int(msg.get("seq") or 0)
        if book.seq and seq and seq != book.seq + 1:
            log.warning("book_seq_gap", ticker=ticker, have=book.seq, got=seq)
        side = str(body.get("side") or "").lower()
        price = D(body["price_dollars"])
        delta = D(body["delta_fp"])
        levels = book.yes_bids if side == "yes" else book.no_bids
        updated = _apply_level_delta(levels, price, delta)
        if side == "yes":
            book.yes_bids = updated
        else:
            book.no_bids = updated
        book.seq = seq or book.seq
        return book

    def handle_ws(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "orderbook_snapshot":
            self.apply_snapshot(message)
        elif kind == "orderbook_delta":
            self.apply_delta(message)


def _apply_level_delta(
    levels: list[PriceLevel], price: Decimal, delta: Decimal
) -> list[PriceLevel]:
    by_price = {lvl.price: lvl.size for lvl in levels}
    by_price[price] = by_price.get(price, Decimal("0")) + delta
    return [
        PriceLevel(price=px, size=sz)
        for px, sz in sorted(by_price.items())
        if sz > 0
    ]


class MarketUniverse:
    """Discover open 15m windows by series_ticker and roll them cleanly."""

    def __init__(self, settings: Settings, source: MarketSource) -> None:
        self.settings = settings
        self.source = source
        self.markets: dict[str, MarketWindow] = {}

    def refresh(self, *, now: datetime | None = None) -> list[MarketWindow]:
        now = now or datetime.now(UTC)
        discovered: list[MarketWindow] = []
        for series in self.settings.series_tickers:
            try:
                discovered.extend(self.source.list_open_markets(series))
            except Exception:
                log.exception("discover_failed", series=series)

        # Drop closed / past close_time.
        live = [
            m
            for m in discovered
            if m.close_time > now
            and m.status not in {"closed", "settled", "finalized", "determined"}
        ]
        live.sort(key=lambda m: m.close_time)

        # Keep at most max_windows soonest-to-close unique events.
        chosen: list[MarketWindow] = []
        seen_events: set[str] = set()
        for market in live:
            if market.window_id in seen_events:
                continue
            if len(chosen) >= self.settings.max_windows:
                break
            chosen.append(market)
            seen_events.add(market.window_id)

        previous = set(self.markets)
        current = {m.ticker: m for m in chosen}
        added = set(current) - previous
        removed = previous - set(current)
        if added or removed:
            log.info(
                "universe_rollover",
                added=sorted(added),
                removed=sorted(removed),
                active=[m.ticker for m in chosen],
            )
        self.markets = current
        return chosen

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
