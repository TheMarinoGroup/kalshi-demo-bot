"""Dig6 MM frozen contract models (HITLDecision / DeskMode / SizeIntent / BusEvent).

JSON schemas live in ``schemas/v0/``. LIVE is unreachable. Family D / scale_in
is refused with ``blocked_by=LANE_MM``. Paper only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kalshi_pbot.config import coerce_desk_mode
from kalshi_pbot.kelly import DEFAULT_KELLY_MAX, kelly_fraction
from kalshi_pbot.types import D, IntentKind, PortfolioSnapshot, QuoteIntent

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas" / "v0"
DESK_LANE = "MM"
DESK_PROFILE = "dig6_tight"
BLOCKED_LANE_MM = "LANE_MM"
BLOCKED_HITL = "HITL_BLOCK"
SIZE_MODES = frozenset({"entry", "complete_only", "reduce_only", "flat"})
SCALE_IN = "scale_in"


class SizeIntentError(ValueError):
    def __init__(self, message: str, *, blocked_by: str | None = None) -> None:
        super().__init__(message)
        self.blocked_by = blocked_by


class DecisionKind(StrEnum):
    APPROVE = "approve"
    DENY = "deny"
    TIMEOUT_DENY = "timeout_deny"


class DecisionActor(StrEnum):
    USER = "user"
    ATLAS = "atlas"


class BusCode(StrEnum):
    SOFT_ABORT = "SoftAbort"
    NO_NEW_RISK = "NoNewRisk"
    UNPAIRED_KILL = "UnpairedKill"
    DAILY_KILL_LATCHED = "DailyKillLatched"
    HARD_KILL = "HardKill"
    KILL_CLEARED = "KillCleared"


class DeskModeName(StrEnum):
    PAPER = "PAPER"
    HITL = "HITL"
    LIVE_BLOCKED = "LIVE_BLOCKED"


def _iso(ts: datetime | None = None) -> str:
    now = ts or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.isoformat()


def _dec_str(value: Decimal | float | int | str | None) -> str | None:
    if value is None:
        return None
    return format(D(value), "f")


class HITLDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str
    decision: Literal["approve", "deny", "timeout_deny"]
    actor: Literal["user", "atlas"]
    ts: str


class DeskMode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    desk_lane: Literal["MM"] = DESK_LANE
    mode: Literal["PAPER", "HITL", "LIVE_BLOCKED"]
    paper: Literal[True] = True
    profile: Literal["dig6_tight"] = DESK_PROFILE
    bankroll: str


class SizeIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str
    desk_lane: Literal["MM"] = DESK_LANE
    mode: Literal["entry", "complete_only", "reduce_only", "flat"]
    ticker: str
    side: Literal["yes", "no"]
    clip: str | None = None
    count: str | None = None
    price: str | None = None
    kelly_frac: float | None = Field(default=None, ge=0, le=0.25)
    edge: float | None = None
    p_star: float | None = None
    projected_open: str | None = None
    projected_onesided: str | None = None
    projected_day_pnl: str | None = None
    kill_headroom: str | None = None
    blocked_by: str | None = None
    ts: str | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _no_scale_in(cls, value: object) -> object:
        raw = str(value or "").strip().lower()
        if raw == SCALE_IN:
            raise SizeIntentError(
                "scale_in is Family D / SCALE — MM lane only",
                blocked_by=BLOCKED_LANE_MM,
            )
        return value


class BusEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "SoftAbort",
        "NoNewRisk",
        "UnpairedKill",
        "DailyKillLatched",
        "HardKill",
        "KillCleared",
    ]
    ts: str
    ticker: str | None = None
    detail: str = ""


def intent_kind_to_mode(kind: IntentKind, *, reduce_only: bool = False) -> str:
    if kind is IntentKind.ENTRY:
        return "entry"
    if kind is IntentKind.COMPLETE_PAIR:
        return "complete_only"
    if kind is IntentKind.FLATTEN:
        return "flat"
    if reduce_only or kind is IntentKind.CANCEL:
        return "reduce_only"
    return "entry"


def is_new_risk_mode(mode: str) -> bool:
    return mode == "entry"


def parse_size_intent(payload: dict[str, Any]) -> SizeIntent:
    mode = str(payload.get("mode") or "").strip().lower()
    if mode == SCALE_IN:
        raise SizeIntentError(
            "scale_in is Family D / SCALE — MM lane only",
            blocked_by=BLOCKED_LANE_MM,
        )
    if mode and mode not in SIZE_MODES:
        raise SizeIntentError(f"unknown SizeIntent mode {mode!r}", blocked_by=BLOCKED_LANE_MM)
    return SizeIntent.model_validate(payload)


def size_ui_block(*, kelly_max: Decimal = DEFAULT_KELLY_MAX) -> dict[str, Any]:
    """Bayesian / Family D panel: SCALE is not on the MM lane."""
    return {
        "lane": DESK_LANE,
        "family_d": False,
        "scale_in": {"accepted": False, "blocked_by": BLOCKED_LANE_MM},
        "kelly_max": float(kelly_max),
        "note": "Family D / scale_in blocked_by=LANE_MM — no SCALE path",
    }


def desk_mode_payload(mode: str, bankroll: Decimal) -> dict[str, Any]:
    return DeskMode(
        mode=coerce_desk_mode(mode),  # type: ignore[arg-type]
        bankroll=_dec_str(bankroll) or "500",
    ).model_dump()


def size_intent_from_quote(
    intent: QuoteIntent,
    snapshot: PortfolioSnapshot,
    *,
    intent_id: str,
    settings: Any,
    bid_sum: Decimal | None = None,
    now: datetime | None = None,
    blocked_by: str | None = None,
) -> SizeIntent:
    mode = intent_kind_to_mode(intent.kind, reduce_only=intent.reduce_only)
    frac = kelly_fraction(intent.notional, settings.bankroll)
    if frac > DEFAULT_KELLY_MAX:
        frac = DEFAULT_KELLY_MAX
    edge = None
    if bid_sum is not None:
        edge = float(Decimal("1") - D(bid_sum))
    kill_headroom = settings.daily_loss_limit + snapshot.daily_pnl
    if kill_headroom < 0:
        kill_headroom = Decimal("0")
    from kalshi_pbot.risk_engine import projected_open_notional

    projected_open = projected_open_notional(snapshot, intent)
    projected_onesided = snapshot.unpaired_notional
    if mode == "entry":
        projected_onesided = snapshot.unpaired_notional + intent.notional
    return SizeIntent(
        intent_id=intent_id,
        mode=mode,  # type: ignore[arg-type]
        ticker=intent.market_ticker,
        side=intent.outcome.value,  # type: ignore[arg-type]
        clip=_dec_str(intent.notional),
        count=_dec_str(intent.count),
        price=_dec_str(intent.price),
        kelly_frac=float(frac),
        edge=edge,
        p_star=None,
        projected_open=_dec_str(projected_open),
        projected_onesided=_dec_str(projected_onesided),
        projected_day_pnl=_dec_str(snapshot.daily_pnl),
        kill_headroom=_dec_str(kill_headroom),
        blocked_by=blocked_by,
        ts=_iso(now),
    )


def schema_path(name: str) -> Path:
    return SCHEMA_DIR / name
