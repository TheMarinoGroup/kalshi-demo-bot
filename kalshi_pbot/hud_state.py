"""Serialize PaperBot state for the Bloomberg-style HUD."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from kalshi_pbot.metrics import compute_metrics
from kalshi_pbot.risk_engine import in_last_seconds, seconds_since_close, seconds_to_close
from kalshi_pbot.types import OrderBook

HISTORY_LEN = 180


def _f(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


class MidHistory:
    def __init__(self, maxlen: int = HISTORY_LEN) -> None:
        self._series: dict[str, deque[dict[str, float]]] = defaultdict(
            lambda: deque(maxlen=maxlen)
        )

    def push(self, ticker: str, mid: Decimal | None, spread: Decimal | None, now_ms: int) -> None:
        if mid is None:
            return
        self._series[ticker].append(
            {"t": float(now_ms), "mid": float(mid), "spread": float(spread or 0)}
        )

    def series(self, ticker: str) -> list[dict[str, float]]:
        return list(self._series.get(ticker, ()))


def build_snapshot(bot: Any, history: MidHistory, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    settings = bot.settings
    books: dict[str, OrderBook] = {
        m.ticker: book
        for m in bot.universe.markets.values()
        if (book := bot.books.get(m.ticker)) is not None
    }
    port = bot.portfolio.snapshot(books)
    metrics = compute_metrics(settings, bot.portfolio, port)

    windows = []
    for market in list(bot.universe.markets.values()) + list(bot.universe.upcoming[:6]):
        book = bot.books.get(market.ticker)
        tob = book.tob() if book else None
        to_close = seconds_to_close(market.close_time, now)
        windows.append(
            {
                "ticker": market.ticker,
                "event": market.event_ticker,
                "series": market.series_ticker,
                "title": market.title,
                "status": market.status,
                "open": _iso(market.open_time),
                "close": _iso(market.close_time),
                "seconds_to_close": to_close,
                "seconds_since_close": seconds_since_close(market.close_time, now),
                "last_60s": in_last_seconds(market.close_time, settings.last_seconds, now),
                "live": market.ticker in bot.universe.markets,
                "yes_bid": _f(tob.yes_bid) if tob else None,
                "yes_ask": _f(tob.yes_ask) if tob else None,
                "no_bid": _f(tob.no_bid) if tob else None,
                "no_ask": _f(tob.no_ask) if tob else None,
                "mid": _f(tob.mid_yes) if tob else None,
                "spread": _f(tob.spread_yes) if tob else None,
                "spark": history.series(market.ticker),
            }
        )

    positions = []
    for pos in port.positions.values():
        if pos.yes_qty == 0 and pos.no_qty == 0:
            continue
        positions.append(
            {
                "ticker": pos.market_ticker,
                "event": pos.event_ticker,
                "yes_qty": _f(pos.yes_qty),
                "no_qty": _f(pos.no_qty),
                "yes_avg": _f(pos.avg_yes()),
                "no_avg": _f(pos.avg_no()),
                "unpaired": _f(pos.unpaired_qty),
                "unpaired_outcome": pos.unpaired_outcome.value if pos.unpaired_outcome else None,
                "unpaired_notional": _f(pos.unpaired_notional()),
                "locked_pnl": _f(pos.locked_pair_pnl()),
                "fees": _f(pos.fees),
            }
        )

    resting = [
        {
            "order_id": o.order_id,
            "ticker": o.market_ticker,
            "outcome": o.outcome.value,
            "price": _f(o.price),
            "remaining": _f(o.remaining),
            "post_only": o.post_only,
            "liquidity": "maker" if o.post_only else "taker",
        }
        for o in port.resting
    ]

    fills = [
        {
            "fill_id": f.fill_id,
            "order_id": f.order_id,
            "ticker": f.market_ticker,
            "outcome": f.outcome.value,
            "price": _f(f.price),
            "count": _f(f.count),
            "fee": _f(f.fee),
            "liquidity": "taker" if f.is_taker else "maker",
            "ts_ms": f.ts_ms,
        }
        for f in bot.portfolio.fills[-40:]
    ]

    return {
        "v": 1,
        "ts": _iso(now),
        "mode": {
            "env": settings.env,
            "dry_run": settings.dry_run,
            "paper_tape": settings.paper_tape,
            "live_submit": settings.live_submit,
            "mock": settings.mock,
            "latency_ms": settings.latency_ms,
        },
        "kill": {
            "active": bool(port.kill_active or bot.risk.kill_active),
            "reason": port.kill_reason or bot.risk.kill_reason,
        },
        "risk": {
            "bankroll": _f(settings.bankroll),
            "clip": _f(settings.clip),
            "open_notional": _f(port.open_notional),
            "max_open": _f(settings.max_open_notional),
            "unpaired": _f(port.unpaired_notional),
            "max_onesided": _f(settings.max_onesided),
            "daily_pnl": _f(port.daily_pnl),
            "daily_kill": _f(settings.daily_loss_limit),
            "windows": len(port.window_ids),
            "max_windows": settings.max_windows,
            "last_seconds": settings.last_seconds,
            "settle_recycle_s": settings.effective_settle_recycle_seconds,
            "min_window_minutes": settings.min_window_minutes,
        },
        "pnl": {
            "realized": _f(metrics.realized_pnl),
            "unrealized": _f(metrics.unrealized_pnl),
            "fees": _f(metrics.fees),
            "daily": _f(metrics.daily_pnl),
            "fill_count": metrics.fill_count,
            "order_count": metrics.order_count,
            "fill_rate": _f(metrics.fill_rate),
            "open_util": _f(metrics.open_notional_util),
            "onesided_util": _f(metrics.onesided_util),
            "daily_loss_util": _f(metrics.daily_loss_util),
        },
        "windows": windows,
        "upcoming": [
            {
                "ticker": m.ticker,
                "series": m.series_ticker,
                "open": _iso(m.open_time),
                "close": _iso(m.close_time),
            }
            for m in bot.universe.upcoming[:8]
        ],
        "positions": positions,
        "resting": resting,
        "fills": fills,
        "series": list(settings.series_tickers),
    }
