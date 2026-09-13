"""Risk Desk v1 — locked hard limits for the $1000 paper bankroll.

Limits rescale with `settings.bankroll`. Kill switch cancels/refuses
until an explicit reset. Last 60s of each window: no new risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from kalshi_pbot.config import CLIP_MAX, CLIP_MIN, Settings
from kalshi_pbot.types import (
    IntentKind,
    PortfolioSnapshot,
    QuoteIntent,
    RejectReason,
    RiskDecision,
)


def _now(ts: datetime | None) -> datetime:
    if ts is None:
        return datetime.now(UTC)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def seconds_to_close(close_time: datetime, now: datetime | None = None) -> float:
    close = close_time if close_time.tzinfo else close_time.replace(tzinfo=UTC)
    return (close - _now(now)).total_seconds()


def in_last_seconds(close_time: datetime, last_seconds: int, now: datetime | None = None) -> bool:
    return seconds_to_close(close_time, now) <= last_seconds


@dataclass
class RiskEngine:
    settings: Settings
    kill_active: bool = False
    kill_reason: str = ""
    _latched_daily: bool = field(default=False, init=False)

    def trip(self, reason: str) -> None:
        self.kill_active = True
        self.kill_reason = reason

    def reset_kill(self) -> None:
        self.kill_active = False
        self.kill_reason = ""
        self._latched_daily = False

    def maybe_trip_daily(self, snapshot: PortfolioSnapshot) -> bool:
        if snapshot.daily_pnl <= -self.settings.daily_loss_limit:
            self._latched_daily = True
            self.trip(
                f"daily_loss {snapshot.daily_pnl} <= -{self.settings.daily_loss_limit}"
            )
            return True
        return False

    def evaluate(
        self,
        intent: QuoteIntent,
        snapshot: PortfolioSnapshot,
        *,
        close_time: datetime,
        now: datetime | None = None,
    ) -> RiskDecision:
        self.maybe_trip_daily(snapshot)
        if snapshot.kill_active or self.kill_active:
            if intent.kind in {IntentKind.FLATTEN, IntentKind.CANCEL}:
                return RiskDecision(True, RejectReason.OK, "flatten_during_kill")
            return RiskDecision(
                False,
                RejectReason.KILL_SWITCH,
                self.kill_reason or snapshot.kill_reason or "kill switch armed",
            )

        if intent.count <= 0 or intent.price <= 0 or intent.price >= 1:
            return RiskDecision(False, RejectReason.INVALID, "price/count out of range")

        flatten_like = intent.kind in {IntentKind.FLATTEN, IntentKind.CANCEL} or intent.reduce_only

        if in_last_seconds(close_time, self.settings.last_seconds, now):
            if flatten_like:
                return RiskDecision(True, RejectReason.OK, "last_seconds_flatten")
            return RiskDecision(
                False,
                RejectReason.LAST_SECONDS,
                f"no new risk in last {self.settings.last_seconds}s before close",
            )

        if not flatten_like:
            if intent.notional < CLIP_MIN and intent.notional + Decimal("0.01") < CLIP_MIN:
                # Completing/flattening may be smaller; new entries stay in band.
                if intent.kind is IntentKind.ENTRY and intent.notional < CLIP_MIN:
                    return RiskDecision(
                        False,
                        RejectReason.PER_FILL,
                        f"clip {intent.notional} below min ${CLIP_MIN}",
                    )
            if intent.notional > CLIP_MAX:
                return RiskDecision(
                    False,
                    RejectReason.PER_FILL,
                    f"clip {intent.notional} above max ${CLIP_MAX}",
                )

        projected_windows = set(snapshot.window_ids)
        projected_windows.add(intent.event_ticker)
        if (
            not flatten_like
            and intent.event_ticker not in snapshot.window_ids
            and len(projected_windows) > self.settings.max_windows
        ):
            return RiskDecision(
                False,
                RejectReason.CONCURRENT_WINDOWS,
                f"{len(projected_windows)} > max {self.settings.max_windows} windows",
            )

        projected_open = snapshot.open_notional
        if not flatten_like:
            projected_open += intent.notional
        if projected_open > self.settings.max_open_notional:
            return RiskDecision(
                False,
                RejectReason.OPEN_NOTIONAL,
                f"open {projected_open} > max {self.settings.max_open_notional}",
            )

        if not flatten_like:
            onesided = self._projected_onesided(intent, snapshot)
            if onesided > self.settings.max_onesided:
                return RiskDecision(
                    False,
                    RejectReason.ONESIDED_CAP,
                    f"onesided {onesided} > max {self.settings.max_onesided}",
                )

        return RiskDecision(True, RejectReason.OK)

    def _projected_onesided(self, intent: QuoteIntent, snapshot: PortfolioSnapshot) -> Decimal:
        pos = snapshot.positions.get(intent.market_ticker)
        yes_qty = pos.yes_qty if pos else Decimal("0")
        no_qty = pos.no_qty if pos else Decimal("0")
        yes_cost = pos.yes_cost if pos else Decimal("0")
        no_cost = pos.no_cost if pos else Decimal("0")

        if intent.outcome.value == "yes":
            yes_qty += intent.count
            yes_cost += intent.notional
        else:
            no_qty += intent.count
            no_cost += intent.notional

        paired = min(yes_qty, no_qty)
        extra_yes = yes_qty - paired
        extra_no = no_qty - paired
        yes_px = (yes_cost / yes_qty) if yes_qty else Decimal("0")
        no_px = (no_cost / no_qty) if no_qty else Decimal("0")
        return extra_yes * yes_px + extra_no * no_px
