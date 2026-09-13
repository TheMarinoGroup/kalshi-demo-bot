"""Serialize PaperBot state for the Bloomberg-style HUD."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from kalshi_pbot.config import CLIP_MAX, CLIP_MIN, SETTLE_RECYCLE_BAND, Settings
from kalshi_pbot.metrics import compute_metrics
from kalshi_pbot.risk_engine import (
    classify_kill,
    in_last_seconds,
    recycle_ready,
    seconds_since_close,
    seconds_to_close,
)
from kalshi_pbot.types import Fill, MarketWindow, OrderBook, PortfolioSnapshot

HISTORY_LEN = 180


def _f(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


def _util(value: Decimal | float | int, cap: Decimal | float | int) -> float:
    cap_f = float(cap)
    if cap_f <= 0:
        return 0.0
    return float(value) / cap_f


def _tone(util: float, *, kill: bool = False) -> str:
    if kill or util >= 1.0:
        return "red"
    if util >= 0.80:
        return "amber"
    return "green"


def _asset_key(ticker: str) -> str:
    upper = ticker.upper()
    if "BTC" in upper:
        return "BTC"
    if "ETH" in upper:
        return "ETH"
    return "OTHER"


def _mode_block(settings: Settings) -> dict[str, Any]:
    live_prod = bool(settings.live_submit and settings.env == "production")
    hard_stop = live_prod and not settings.allow_production
    badge = "LIVE" if live_prod else "PAPER"
    return {
        "env": settings.env,
        "dry_run": settings.dry_run,
        "paper_tape": settings.paper_tape,
        "live_submit": settings.live_submit,
        "allow_production": settings.allow_production,
        "mock": settings.mock,
        "latency_ms": settings.latency_ms,
        "badge": badge,
        "paper_only": badge == "PAPER",
        "hard_stop": hard_stop,
        "demo_submit": bool(settings.live_submit and settings.env != "production"),
    }


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


def _last_fill(fills: list[Fill]) -> dict[str, Any] | None:
    if not fills:
        return None
    fill = fills[-1]
    notional = fill.price * fill.count
    return {
        "fill_id": fill.fill_id,
        "ticker": fill.market_ticker,
        "outcome": fill.outcome.value,
        "price": _f(fill.price),
        "count": _f(fill.count),
        "notional": _f(notional),
        "fee": _f(fill.fee),
        "liquidity": "taker" if fill.is_taker else "maker",
        "ts_ms": fill.ts_ms,
    }


def _onesided_detail(port: PortfolioSnapshot) -> dict[str, Any]:
    worst: tuple[Decimal, str, str | None] | None = None
    for pos in port.positions.values():
        notion = pos.unpaired_notional()
        if notion <= 0:
            continue
        outcome = pos.unpaired_outcome.value if pos.unpaired_outcome else None
        if worst is None or notion > worst[0]:
            worst = (notion, pos.market_ticker, outcome)
    if worst is None:
        return {"ticker": None, "leg": None, "notional": 0.0}
    return {"ticker": worst[1], "leg": worst[2], "notional": _f(worst[0])}


def _mix(port: PortfolioSnapshot) -> dict[str, float]:
    mix = {"BTC": 0.0, "ETH": 0.0, "OTHER": 0.0}
    for pos in port.positions.values():
        mix[_asset_key(pos.market_ticker)] += float(pos.cost_basis())
    for order in port.resting:
        mix[_asset_key(order.market_ticker)] += float(order.reserved_notional)
    return mix


def _settle_row(
    market: MarketWindow, settings: Settings, now: datetime, locked: Decimal
) -> dict[str, Any]:
    recycle_s = settings.effective_settle_recycle_seconds
    since = seconds_since_close(market.close_time, now)
    to_unlock = recycle_s - since
    unlocked = recycle_ready(
        market.close_time,
        recycle_s,
        now,
        expected_expiration=market.expected_expiration,
    ) and market.result is not None
    to_settlement = None
    if market.settlement_ts is not None:
        settle = (
            market.settlement_ts
            if market.settlement_ts.tzinfo
            else market.settlement_ts.replace(tzinfo=UTC)
        )
        to_settlement = (settle - now).total_seconds()
    return {
        "ticker": market.ticker,
        "series": market.series_ticker,
        "close": _iso(market.close_time),
        "settlement_ts": _iso(market.settlement_ts),
        "expected_expiration": _iso(market.expected_expiration),
        "expected_expiration_is_lock": False,
        "result": market.result.value if market.result else None,
        "recycle_s": recycle_s,
        "seconds_since_close": since,
        "seconds_to_unlock": to_unlock,
        "seconds_to_settlement_ts": to_settlement,
        "unlocked": unlocked,
        "free_on": "settlement_ts",
        "locked_notional": _f(locked),
    }


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
    kill_reason = port.kill_reason or bot.risk.kill_reason
    kill_active = bool(port.kill_active or bot.risk.kill_active)
    kill_code = classify_kill(kill_reason) if kill_active else ""

    last = _last_fill(bot.portfolio.fills)
    last_notional = Decimal(str(last["notional"])) if last else Decimal("0")
    onesided = _onesided_detail(port)
    loss = -port.daily_pnl if port.daily_pnl < 0 else Decimal("0")
    open_util = _util(port.open_notional, settings.max_open_notional)
    onesided_util = _util(port.unpaired_notional, settings.max_onesided)
    windows_util = _util(len(port.window_ids), settings.max_windows)
    daily_util = _util(loss, settings.daily_loss_limit)
    fill_util = _util(last_notional, CLIP_MAX)
    open_breach = port.open_notional >= settings.max_open_notional
    onesided_breach = port.unpaired_notional >= settings.max_onesided
    daily_breach = port.daily_pnl <= -settings.daily_loss_limit
    fill_breach = last_notional > CLIP_MAX

    windows = []
    gate_rows = []
    any_last_60 = False
    any_violation = False
    for market in list(bot.universe.markets.values()) + list(bot.universe.upcoming[:6]):
        book = bot.books.get(market.ticker)
        tob = book.tob() if book else None
        to_close = seconds_to_close(market.close_time, now)
        last_60 = in_last_seconds(market.close_time, settings.last_seconds, now)
        live = market.ticker in bot.universe.markets
        new_risk = (not kill_active) and (not last_60) and live
        # Violation: last-60s with a still-resting entry (post-only) quote.
        violation = bool(
            live
            and last_60
            and any(
                o.event_ticker == market.event_ticker and o.post_only for o in port.resting
            )
        )
        if live and last_60:
            any_last_60 = True
        if violation:
            any_violation = True
        windows.append(
            {
                "ticker": market.ticker,
                "event": market.event_ticker,
                "series": market.series_ticker,
                "title": market.title,
                "status": market.status,
                "open": _iso(market.open_time),
                "close": _iso(market.close_time),
                "settlement_ts": _iso(market.settlement_ts),
                "expected_expiration": _iso(market.expected_expiration),
                "seconds_to_close": to_close,
                "seconds_since_close": seconds_since_close(market.close_time, now),
                "last_60s": last_60,
                "new_risk_allowed": new_risk,
                "gate_violation": violation,
                "live": live,
                "yes_bid": _f(tob.yes_bid) if tob else None,
                "yes_ask": _f(tob.yes_ask) if tob else None,
                "no_bid": _f(tob.no_bid) if tob else None,
                "no_ask": _f(tob.no_ask) if tob else None,
                "mid": _f(tob.mid_yes) if tob else None,
                "spread": _f(tob.spread_yes) if tob else None,
                "spark": history.series(market.ticker),
            }
        )
        if live:
            gate_rows.append(
                {
                    "ticker": market.ticker,
                    "series": market.series_ticker,
                    "seconds_to_close": to_close,
                    "last_60s": last_60,
                    "new_risk_allowed": new_risk,
                    "violation": violation,
                }
            )

    new_risk_allowed = (not kill_active) and (not any_last_60)

    settle_rows = []
    for market in bot.universe.settling.values():
        pos = port.positions.get(market.ticker)
        locked = pos.cost_basis() if pos else Decimal("0")
        settle_rows.append(_settle_row(market, settings, now, locked))

    maker_fills = bot.portfolio.maker_fill_count
    taker_fills = bot.portfolio.taker_fill_count
    fill_n = maker_fills + taker_fills
    maker_first_pct = (maker_fills / fill_n) if fill_n else 1.0
    dir_qty = bot.portfolio.directional_qty_settled
    pair_qty = bot.portfolio.paired_qty_realized
    settled_n = dir_qty + pair_qty
    settled_dir_pct = float(dir_qty / settled_n) if settled_n else 0.0
    drawdown = bot.portfolio.day_high_pnl - port.daily_pnl
    if drawdown < 0:
        drawdown = Decimal("0")

    util = {
        "fill": {
            "label": "fill",
            "value": _f(last_notional),
            "max": _f(CLIP_MAX),
            "util": fill_util,
            "tone": _tone(fill_util, kill=fill_breach),
        },
        "open": {
            "label": "open",
            "value": _f(port.open_notional),
            "max": _f(settings.max_open_notional),
            "util": open_util,
            "tone": _tone(open_util, kill=open_breach or kill_code == "open"),
        },
        "windows": {
            "label": "windows",
            "value": float(len(port.window_ids)),
            "max": float(settings.max_windows),
            "util": windows_util,
            "tone": _tone(windows_util),
        },
        "onesided": {
            "label": "one-sided",
            "value": _f(port.unpaired_notional),
            "max": _f(settings.max_onesided),
            "util": onesided_util,
            "tone": _tone(onesided_util, kill=onesided_breach or kill_code == "one-sided"),
        },
        "daily_loss": {
            "label": "daily loss",
            "value": _f(loss),
            "max": _f(settings.daily_loss_limit),
            "util": daily_util,
            "tone": _tone(daily_util, kill=daily_breach or kill_code == "loss"),
        },
    }

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

    band = list(SETTLE_RECYCLE_BAND)
    return {
        "v": 2,
        "ts": _iso(now),
        "mode": _mode_block(settings),
        "kill": {
            "active": kill_active,
            "state": "TRIPPED" if kill_active else "ARMED",
            "reason": kill_reason,
            "code": kill_code,
        },
        "risk": {
            "bankroll": _f(settings.bankroll),
            "clip": _f(settings.clip),
            "clip_min": _f(CLIP_MIN),
            "clip_max": _f(CLIP_MAX),
            "last_fill": last,
            "open_notional": _f(port.open_notional),
            "max_open": _f(settings.max_open_notional),
            "open_pct": open_util,
            "open_tone": util["open"]["tone"],
            "unpaired": _f(port.unpaired_notional),
            "max_onesided": _f(settings.max_onesided),
            "onesided_ticker": onesided["ticker"],
            "onesided_leg": onesided["leg"],
            "onesided_tone": util["onesided"]["tone"],
            "abort_unpaired": bool(port.unpaired_notional > 0),
            "daily_pnl": _f(port.daily_pnl),
            "daily_kill": _f(settings.daily_loss_limit),
            "unsettled_pnl": _f(port.unrealized_pnl),
            "unsettled_until": "settlement_ts",
            "windows": len(port.window_ids),
            "max_windows": settings.max_windows,
            "last_seconds": settings.last_seconds,
            "settle_recycle_s": settings.effective_settle_recycle_seconds,
            "settle_band": band,
            "min_window_minutes": settings.min_window_minutes,
        },
        "fees": {
            "today": _f(bot.portfolio.fees),
            "maker": _f(bot.portfolio.maker_fees),
            "taker": _f(bot.portfolio.taker_fees),
            "maker_pending_confirm": True,
            "note": "maker $0 pending demo-fill confirm",
        },
        "gate": {
            "last_seconds": settings.last_seconds,
            "new_risk_allowed": new_risk_allowed,
            "violation": any_violation,
            "windows": gate_rows,
        },
        "settle": {
            "lock": "settlement_ts",
            "not_expected_expiration": True,
            "plan_s": band,
            "recycle_s": settings.effective_settle_recycle_seconds,
            "rare_tail": settings.settle_rare_tail,
            "buffers": settle_rows,
        },
        "util": util,
        "extras": {
            "day_high": _f(bot.portfolio.day_high_pnl),
            "drawdown": _f(drawdown),
            "settled_directional_pct": settled_dir_pct,
            "maker_first_pct": maker_first_pct,
            "maker_fills": maker_fills,
            "taker_fills": taker_fills,
            "mix": _mix(port),
            "quote_mode": settings.quote_mode,
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
