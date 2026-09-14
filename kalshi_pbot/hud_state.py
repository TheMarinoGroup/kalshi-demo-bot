"""Serialize PaperBot state for the Bloomberg-style HUD."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from kalshi_pbot.config import CFB_INDEX, CLIP_MAX, CLIP_MIN, SETTLE_RECYCLE_BAND, Settings
from kalshi_pbot.fees import arb_taker_eligible, pair_cost
from kalshi_pbot.metrics import compute_metrics
from kalshi_pbot.risk_engine import (
    capital_free_at,
    classify_kill,
    in_last_seconds,
    recycle_ready,
    seconds_since_close,
    seconds_to_close,
    ttc_zone,
)
from kalshi_pbot.strategy.maker import is_underround
from kalshi_pbot.types import CFBTick, Fill, MarketWindow, OrderBook, PortfolioSnapshot

HISTORY_LEN = 180
PNL_CURVE_LEN = 720
PNL_CURVE_INTERVAL_MS = 8_000
TAPE_FILL_LEN = 100


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


class PnlHistory:
    """Session equity marks. Downsamples the 50ms HUD publish loop."""

    def __init__(self, maxlen: int = PNL_CURVE_LEN) -> None:
        self._points: deque[dict[str, float]] = deque(maxlen=maxlen)
        self._last_ms = 0
        self._last_daily: float | None = None
        self._last_fills = -1

    def push(
        self,
        now_ms: int,
        daily: float,
        realized: float,
        unrealized: float,
        fill_count: int,
        *,
        min_interval_ms: int = PNL_CURVE_INTERVAL_MS,
    ) -> None:
        point = {
            "t": float(now_ms),
            "daily": daily,
            "realized": realized,
            "unrealized": unrealized,
            "fill_count": float(fill_count),
        }
        changed = fill_count != self._last_fills or (
            self._last_daily is None or abs(daily - self._last_daily) >= 0.0001
        )
        due = not self._points or (now_ms - self._last_ms) >= min_interval_ms
        if not (changed or due):
            self._points[-1] = point
            return
        self._points.append(point)
        self._last_ms = now_ms
        self._last_daily = daily
        self._last_fills = fill_count

    def series(self) -> list[dict[str, float]]:
        return list(self._points)


class MidHistory:
    def __init__(self, maxlen: int = HISTORY_LEN) -> None:
        self._series: dict[str, deque[dict[str, float]]] = defaultdict(
            lambda: deque(maxlen=maxlen)
        )
        self.pnl = PnlHistory()

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


def _cfb_block(bot: Any, series: str) -> dict[str, Any]:
    ticks: dict[str, CFBTick] = getattr(bot, "cfb", {}) or {}
    index_id = CFB_INDEX.get(series)
    tick = ticks.get(index_id) if index_id else None
    return {
        "index_id": index_id,
        "avg_60s": _f(tick.avg_60s) if tick and tick.avg_60s is not None else None,
        "qtr_avg": _f(tick.settle_avg) if tick and tick.settle_avg is not None else None,
        "live": _f(tick.value) if tick else None,
        "lag_ms": None,  # unmeasured — HUD shows "—"
        "oracle": False,
        "label": "chart≠settle",
    }


def _book_depth(
    book: OrderBook | None,
    min_edge: Decimal,
    *,
    fee_type: str = "quadratic",
    multiplier: Decimal = Decimal("1"),
) -> dict[str, Any]:
    """Split Regime B underround from Regime A taker-arb. Never alias them as ARB."""
    empty = {
        "yes_bid_sz": None,
        "no_bid_sz": None,
        "yes_ask_sz": None,
        "no_ask_sz": None,
        "bid_sum": None,
        "ask_sum": None,
        "ask_sum_plus_fees": None,
        "underround": False,
        "arb_taker_eligible": False,
        "arb": False,  # reserved for Regime A only; never underround
    }
    if book is None:
        return empty
    bid_sum = book.bid_sum()
    ask_sum = book.ask_sum()
    yes_ask = book.implied_yes_ask()
    no_ask = book.implied_no_ask()
    # Regime B: same helper as paper-v2 ENTRY (bid_sum ≤ 1 − min_edge).
    underround = is_underround(book, min_edge)
    ask_plus_fees = None
    taker_ok = False
    if yes_ask is not None and no_ask is not None:
        _premium, fees = pair_cost(
            yes_ask,
            no_ask,
            Decimal("1"),
            yes_is_taker=True,
            no_is_taker=True,
            fee_type=fee_type,
            multiplier=multiplier,
        )
        ask_plus_fees = ask_sum + fees if ask_sum is not None else None
        # Regime A / Dig4: after-fee taker lock. Never alias underround as ARB.
        taker_ok = arb_taker_eligible(
            yes_ask,
            no_ask,
            Decimal("1"),
            fee_type=fee_type,
            multiplier=multiplier,
        )
    return {
        "yes_bid_sz": _f(book.best_yes_bid_size()),
        "no_bid_sz": _f(book.best_no_bid_size()),
        "yes_ask_sz": _f(book.best_no_bid_size()),  # implied ask size = opposite bid
        "no_ask_sz": _f(book.best_yes_bid_size()),
        "bid_sum": _f(bid_sum),
        "ask_sum": _f(ask_sum),
        "ask_sum_plus_fees": _f(ask_plus_fees),
        "underround": underround,
        "arb_taker_eligible": taker_ok,
        "arb": taker_ok,
    }


def _settle_row(
    market: MarketWindow, settings: Settings, now: datetime, locked: Decimal
) -> dict[str, Any]:
    recycle_s = settings.effective_settle_recycle_seconds
    since = seconds_since_close(market.close_time, now)
    free_at = capital_free_at(
        market.close_time,
        recycle_s,
        settlement_ts=market.settlement_ts,
        expected_expiration=market.expected_expiration,
    )
    to_unlock = (free_at - now).total_seconds()
    unlocked = recycle_ready(
        market.close_time,
        recycle_s,
        now,
        expected_expiration=market.expected_expiration,
        settlement_ts=market.settlement_ts,
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
        "capital_free_at": _iso(free_at),
        "expected_expiration": _iso(market.expected_expiration),
        "expected_expiration_is_lock": False,
        "result": market.result.value if market.result else None,
        "recycle_s": recycle_s,
        "seconds_since_close": since,
        "seconds_to_unlock": to_unlock,
        "seconds_to_settlement_ts": to_settlement,
        "unlocked": unlocked,
        "free_on": "max(settlement_ts, close+recycle)",
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
    recon = getattr(bot, "reconcile", None)
    if recon is not None:
        reconcile = recon.state.as_hud(cancel_orphans=settings.cancel_orphans)
    else:
        ready_fallback = bool(getattr(bot.portfolio, "ready_to_trade", True))
        reconcile = {
            "ready_to_trade": ready_fallback,
            "status": "READY" if ready_fallback else "SYNCING",
            "source": "",
            "error": "",
            "attempts": 0,
            "position_count": 0,
            "resting_count": 0,
            "orphan_count": 0,
            "cancelled_orphans": 0,
            "cancel_orphans": bool(getattr(settings, "cancel_orphans", False)),
            "paper_fills_restored": 0,
            "paper_quotes_restored": 0,
            "blotter_fills": 0,
            "settlements_applied": 0,
            "next_retry_ts": None,
            "hard_hold": False,
            "book_verified": ready_fallback,
        }
    ready = bool(reconcile.get("ready_to_trade"))
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
        new_risk = ready and (not kill_active) and (not last_60) and live
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
        depth = _book_depth(
            book,
            settings.min_edge,
            fee_type=market.fee_type,
            multiplier=market.fee_multiplier,
        )
        free_at = capital_free_at(
            market.close_time,
            settings.effective_settle_recycle_seconds,
            settlement_ts=market.settlement_ts,
            expected_expiration=market.expected_expiration,
        )
        zone = ttc_zone(to_close)
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
                "capital_free_at": _iso(free_at),
                "expected_expiration": _iso(market.expected_expiration),
                "seconds_to_close": to_close,
                "ttc": to_close,
                "ttc_zone": zone,
                "seconds_since_close": seconds_since_close(market.close_time, now),
                "last_60s": last_60,
                "last60s_lock": last_60,
                "new_risk_allowed": new_risk,
                "reconcile_ready": ready,
                "reconcile_status": reconcile.get("status"),
                "hard_hold": bool(reconcile.get("hard_hold")),
                "book_verified": bool(reconcile.get("book_verified", ready)),
                "gate_violation": violation,
                "live": live,
                "yes_bid": _f(tob.yes_bid) if tob else None,
                "yes_ask": _f(tob.yes_ask) if tob else None,
                "no_bid": _f(tob.no_bid) if tob else None,
                "no_ask": _f(tob.no_ask) if tob else None,
                "yes_bid_sz": depth["yes_bid_sz"],
                "no_bid_sz": depth["no_bid_sz"],
                "yes_ask_sz": depth["yes_ask_sz"],
                "no_ask_sz": depth["no_ask_sz"],
                "bid_sum": depth["bid_sum"],
                "ask_sum": depth["ask_sum"],
                "ask_sum_plus_fees": depth["ask_sum_plus_fees"],
                "underround": depth["underround"],
                "arb_taker_eligible": depth["arb_taker_eligible"],
                "arb": depth["arb"],
                "spread": _f(tob.spread_yes) if tob else None,
                "mid": _f(tob.mid_yes) if tob else None,
                "floor_strike": _f(market.floor_strike),
                "cfb": _cfb_block(bot, market.series_ticker),
                "spark": history.series(market.ticker),
            }
        )
        if live:
            gate_rows.append(
                {
                    "ticker": market.ticker,
                    "series": market.series_ticker,
                    "seconds_to_close": to_close,
                    "ttc_zone": zone,
                    "last_60s": last_60,
                    "last60s_lock": last_60,
                    "new_risk_allowed": new_risk,
                    "violation": violation,
                }
            )

    new_risk_allowed = ready and (not kill_active) and (not any_last_60)

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
            "label": "util_open",
            "value": _f(port.open_notional),
            "max": _f(settings.max_open_notional),
            "util": open_util,
            "tone": _tone(open_util, kill=open_breach or kill_code == "open"),
        },
        "windows": {
            "label": "util_windows",
            "value": float(len(port.window_ids)),
            "max": float(settings.max_windows),
            "util": windows_util,
            "tone": _tone(windows_util),
        },
        "onesided": {
            "label": "util_onesided",
            "value": _f(port.unpaired_notional),
            "max": _f(settings.max_onesided),
            "util": onesided_util,
            "tone": _tone(onesided_util, kill=onesided_breach or kill_code == "one-sided"),
        },
        "daily_loss": {
            "label": "day_pnl_net",
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
            "notional": _f(f.price * f.count),
            "fee": _f(f.fee),
            "liquidity": "taker" if f.is_taker else "maker",
            "ts_ms": f.ts_ms,
        }
        for f in bot.portfolio.fills[-TAPE_FILL_LEN:]
    ]

    now_ms = int(now.timestamp() * 1000)
    history.pnl.push(
        now_ms,
        float(metrics.daily_pnl),
        float(metrics.realized_pnl),
        float(metrics.unrealized_pnl),
        metrics.fill_count,
    )

    band = list(SETTLE_RECYCLE_BAND)
    return {
        "v": 3,
        "ts": _iso(now),
        "mode": _mode_block(settings),
        "reconcile": reconcile,
        "kill": {
            "active": kill_active,
            "state": "TRIPPED" if kill_active else "ARMED",
            "reason": kill_reason,
            "code": kill_code,
            "strobe": kill_active or bool(port.unpaired_notional > 0),
            "unpaired_abort": bool(port.unpaired_notional > 0),
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
            "day_pnl_net": _f(port.daily_pnl),
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
            "maker": None,  # unmeasured — HUD shows "—" until demo confirm
            "taker": _f(bot.portfolio.taker_fees),
            "maker_pending_confirm": True,
            "note": "maker fee — until measured / demo-fill confirm",
        },
        "gate": {
            "last_seconds": settings.last_seconds,
            "last60s_lock": any_last_60,
            "no_new_risk": any_last_60 or kill_active or (not ready),
            "new_risk_allowed": new_risk_allowed,
            "ready_to_trade": ready,
            "hard_hold": bool(reconcile.get("hard_hold")),
            "book_verified": bool(reconcile.get("book_verified", ready)),
            "violation": any_violation,
            "windows": gate_rows,
        },
        "settle": {
            "lock": "max(settlement_ts, close+recycle)",
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
            "day_pnl_net": _f(metrics.daily_pnl),
            "fill_count": metrics.fill_count,
            "order_count": metrics.order_count,
            "fill_rate": _f(metrics.fill_rate),
            "open_util": _f(metrics.open_notional_util),
            "onesided_util": _f(metrics.onesided_util),
            "daily_loss_util": _f(metrics.daily_loss_util),
            "curve": history.pnl.series(),
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
