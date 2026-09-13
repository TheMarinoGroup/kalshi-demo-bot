"""PnL, fee, fill-rate, and limit-utilization metrics."""

from __future__ import annotations

from decimal import Decimal

import structlog

from kalshi_pbot.config import Settings
from kalshi_pbot.portfolio import Portfolio
from kalshi_pbot.types import BotMetrics, PortfolioSnapshot

log = structlog.get_logger(__name__)


def compute_metrics(
    settings: Settings, portfolio: Portfolio, snapshot: PortfolioSnapshot
) -> BotMetrics:
    fill_count = len(portfolio.fills)
    order_count = portfolio.orders_submitted
    fill_rate = (Decimal(fill_count) / Decimal(order_count)) if order_count else Decimal("0")
    open_util = (
        snapshot.open_notional / settings.max_open_notional
        if settings.max_open_notional
        else Decimal("0")
    )
    onesided_util = (
        snapshot.unpaired_notional / settings.max_onesided
        if settings.max_onesided
        else Decimal("0")
    )
    loss = -snapshot.daily_pnl if snapshot.daily_pnl < 0 else Decimal("0")
    daily_util = loss / settings.daily_loss_limit if settings.daily_loss_limit else Decimal("0")
    return BotMetrics(
        realized_pnl=snapshot.realized_pnl,
        unrealized_pnl=snapshot.unrealized_pnl,
        fees=snapshot.fees,
        daily_pnl=snapshot.daily_pnl,
        fill_count=fill_count,
        order_count=order_count,
        fill_rate=fill_rate,
        incomplete_pair_notional=snapshot.unpaired_notional,
        open_notional=snapshot.open_notional,
        open_notional_util=open_util,
        onesided_util=onesided_util,
        daily_loss_util=daily_util,
        windows_used=len(snapshot.window_ids),
        kill_active=snapshot.kill_active or False,
        dry_run=settings.dry_run,
        paper_tape=settings.paper_tape,
        latency_ms=settings.latency_ms,
    )


def emit_metrics(metrics: BotMetrics) -> None:
    log.info(
        "metrics",
        realized_pnl=str(metrics.realized_pnl),
        unrealized_pnl=str(metrics.unrealized_pnl),
        fees=str(metrics.fees),
        daily_pnl=str(metrics.daily_pnl),
        fill_count=metrics.fill_count,
        order_count=metrics.order_count,
        fill_rate=str(metrics.fill_rate),
        incomplete_pair=str(metrics.incomplete_pair_notional),
        open_notional=str(metrics.open_notional),
        open_notional_util=str(metrics.open_notional_util),
        onesided_util=str(metrics.onesided_util),
        daily_loss_util=str(metrics.daily_loss_util),
        windows_used=metrics.windows_used,
        kill_active=metrics.kill_active,
        dry_run=metrics.dry_run,
        paper_tape=metrics.paper_tape,
        latency_ms=metrics.latency_ms,
    )
