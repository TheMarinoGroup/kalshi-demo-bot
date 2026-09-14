"""Risk Desk v1 — locked hard limits (5% / 3% / 2% of bankroll).

Paper-v2 Option B defaults are $500: open $25, onesided $15, daily $10.
Limits rescale with `settings.bankroll`. Kill switch cancels/refuses
until an explicit reset. Last 60s of each window: no new risk.

Post-close paper recycle is ``close_time + settle_recycle_seconds``
(default 75s; measured p99≈59s). Do **not** use ``expected_expiration``
(~+300s) as a settle-lock — that field is not actual settlement latency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from kalshi_pbot.config import CLIP_MAX, CLIP_MIN, Settings
from kalshi_pbot.types import (
    IntentKind,
    PortfolioSnapshot,
    Position,
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


def seconds_since_close(close_time: datetime, now: datetime | None = None) -> float:
    return -seconds_to_close(close_time, now)


def ttc_zone(seconds_to_close: float) -> str:
    """GREEN >180s, AMBER 180–60s, RED ≤60s (includes last-60s lock and after close)."""
    if seconds_to_close > 180:
        return "GREEN"
    if seconds_to_close > 60:
        return "AMBER"
    return "RED"


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def capital_free_at(
    close_time: datetime,
    recycle_seconds: int,
    *,
    settlement_ts: datetime | None = None,
    expected_expiration: datetime | None = None,
) -> datetime:
    """Unlock stamp = max(settlement_ts, close + 60–90s plan). Never expected_expiration."""
    del expected_expiration  # never a settle-lock
    plan = _aware(close_time) + timedelta(seconds=recycle_seconds)
    if settlement_ts is None:
        return plan
    return max(plan, _aware(settlement_ts))


def recycle_ready(
    close_time: datetime,
    recycle_seconds: int,
    now: datetime | None = None,
    *,
    expected_expiration: datetime | None = None,
    settlement_ts: datetime | None = None,
) -> bool:
    """True when paper capital may recycle. Ignores expected_expiration."""
    free = capital_free_at(
        close_time,
        recycle_seconds,
        settlement_ts=settlement_ts,
        expected_expiration=expected_expiration,
    )
    return _now(now) >= free


def has_unpaired_inventory(snapshot: PortfolioSnapshot) -> bool:
    return any(pos.unpaired_qty > 0 for pos in snapshot.positions.values())


def unpaired_age_seconds(pos: Position, now: datetime | None = None) -> float | None:
    if pos.unpaired_qty <= 0 or pos.unpaired_since is None:
        return None
    return (_now(now) - _aware(pos.unpaired_since)).total_seconds()


def over_soft_onesided(pos: Position, settings: Settings) -> bool:
    """True when unpaired notional is strictly above the soft onesided preference."""
    if pos.unpaired_qty <= 0 or settings.soft_onesided <= 0:
        return False
    return pos.unpaired_notional() > settings.soft_onesided


def should_abort_unpaired(
    pos: Position,
    settings: Settings,
    now: datetime | None = None,
) -> bool:
    """Soft flatten: age or notional over the soft preference. Does not trip the hard kill."""
    if pos.unpaired_qty <= 0:
        return False
    if over_soft_onesided(pos, settings):
        return True
    max_age = settings.max_unpaired_age_seconds
    if max_age <= 0:
        return False
    age = unpaired_age_seconds(pos, now)
    return age is not None and age >= max_age


def unpaired_abort_reason(
    pos: Position,
    settings: Settings,
    now: datetime | None = None,
) -> str:
    if over_soft_onesided(pos, settings):
        return "unpaired_soft_abort"
    return "unpaired_age_abort"


def increases_open_risk(intent: QuoteIntent) -> bool:
    """True when registering this intent would add gross open (cost or reserved)."""
    return not (
        intent.reduce_only
        or intent.sell
        or intent.kind in {IntentKind.FLATTEN, IntentKind.CANCEL}
    )


def projected_open_notional(snapshot: PortfolioSnapshot, intent: QuoteIntent) -> Decimal:
    """Gross open after this intent is registered (reserved + cost)."""
    if not increases_open_risk(intent):
        return snapshot.open_notional
    return snapshot.open_notional + intent.notional


def over_max_open(snapshot: PortfolioSnapshot, settings: Settings) -> bool:
    """True when gross open is strictly above the locked cap (overshoot)."""
    return snapshot.open_notional > settings.max_open_notional


def should_abort_open(
    snapshot: PortfolioSnapshot,
    settings: Settings,
) -> bool:
    """Soft flatten when open overshoots the cap. Does not trip the hard kill."""
    return over_max_open(snapshot, settings)


def persistable_kill_reason(reason: str) -> bool:
    """Daily-loss and manual latches survive process restart; open/onesided do not."""
    return classify_kill(reason) in {"loss", "manual"}


def classify_kill(reason: str) -> str:
    """Map a kill-switch detail string to HUD codes: loss / open / one-sided / manual."""
    text = (reason or "").strip().lower()
    if not text:
        return ""
    if "manual" in text:
        return "manual"
    if "daily_loss" in text or text.startswith("loss"):
        return "loss"
    if "onesided" in text or "one-sided" in text or "one_sided" in text:
        return "one-sided"
    if "open_notional" in text or text.startswith("open"):
        return "open"
    return text.split()[0]


@dataclass
class RiskEngine:
    settings: Settings
    kill_active: bool = False
    kill_reason: str = ""
    _latched_daily: bool = field(default=False, init=False)

    def trip(self, reason: str) -> None:
        self.kill_active = True
        self.kill_reason = reason
        if persistable_kill_reason(reason):
            self._persist_kill()
        else:
            self._clear_persisted_kill()

    def reset_kill(self) -> None:
        self.kill_active = False
        self.kill_reason = ""
        self._latched_daily = False
        self._clear_persisted_kill()

    def restore_persisted_kill(self, now: datetime | None = None) -> bool:
        """Reload a same-day daily-loss/manual latch. Watchdog restart must not clear it."""
        path = Path(self.settings.kill_latch_path)
        if not path.is_file():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        day_raw = str(payload.get("day") or "")
        reason = str(payload.get("reason") or "")
        today = (now or datetime.now(UTC)).date().isoformat()
        if day_raw != today or not persistable_kill_reason(reason):
            self._clear_persisted_kill()
            return False
        self.kill_active = True
        self.kill_reason = reason
        self._latched_daily = bool(payload.get("latched_daily")) or classify_kill(reason) == "loss"
        return True

    def _persist_kill(self) -> None:
        path = Path(self.settings.kill_latch_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "day": datetime.now(UTC).date().isoformat(),
                        "reason": self.kill_reason,
                        "latched_daily": self._latched_daily,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            return

    def _clear_persisted_kill(self) -> None:
        path = Path(self.settings.kill_latch_path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return

    def maybe_trip_daily(self, snapshot: PortfolioSnapshot) -> bool:
        if snapshot.daily_pnl <= -self.settings.daily_loss_limit:
            self._latched_daily = True
            self.trip(
                f"daily_loss {snapshot.daily_pnl} <= -{self.settings.daily_loss_limit}"
            )
            return True
        return False

    def latch_inventory_kills(self) -> bool:
        """Paper-tape flattens open/onesided overshoot without freezing the soak."""
        return not self.settings.paper_tape

    def maybe_trip_limits(self, snapshot: PortfolioSnapshot) -> bool:
        """Latch daily loss always. Open/onesided latch only outside paper-tape."""
        tripped = self.maybe_trip_daily(snapshot)
        if snapshot.open_notional >= self.settings.max_open_notional:
            if self.latch_inventory_kills():
                self.trip(
                    f"open_notional {snapshot.open_notional} >= {self.settings.max_open_notional}"
                )
                return True
        if snapshot.unpaired_notional >= self.settings.max_onesided:
            if self.latch_inventory_kills():
                self.trip(
                    f"onesided {snapshot.unpaired_notional} >= {self.settings.max_onesided}"
                )
                return True
        return tripped or self.kill_active

    def evaluate(
        self,
        intent: QuoteIntent,
        snapshot: PortfolioSnapshot,
        *,
        close_time: datetime,
        now: datetime | None = None,
    ) -> RiskDecision:
        self.maybe_trip_limits(snapshot)
        if not snapshot.ready_to_trade:
            return RiskDecision(
                False,
                RejectReason.NOT_READY,
                "reconcile not ready — no new risk until exchange snapshot succeeds",
            )
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

        if (
            not flatten_like
            and intent.kind is IntentKind.ENTRY
            and has_unpaired_inventory(snapshot)
        ):
            return RiskDecision(
                False,
                RejectReason.UNPAIRED_EXISTS,
                "no new clip while unpaired inventory exists",
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

        if increases_open_risk(intent):
            if snapshot.open_notional >= self.settings.max_open_notional:
                return RiskDecision(
                    False,
                    RejectReason.OPEN_NOTIONAL,
                    f"open {snapshot.open_notional} >= max {self.settings.max_open_notional}",
                )
            projected_open = projected_open_notional(snapshot, intent)
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
        """Portfolio-wide unpaired notional after this intent (all windows)."""
        others = Decimal("0")
        for ticker, pos in snapshot.positions.items():
            if ticker != intent.market_ticker:
                others += pos.unpaired_notional()

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
        return others + extra_yes * yes_px + extra_no * no_px
