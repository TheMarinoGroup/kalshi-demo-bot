"""HITL queue + bus for Dig6 MM paper desk.

PAPER: auto paper-v2 path, queue empty.
HITL: new-risk ``entry`` SizeIntents wait for approve; reduce/flatten/soft-abort
proceed without approval. Timeout (default 60s) → timeout_deny → HITL_BLOCK.
LIVE_BLOCKED: no unlock path; new risk refused. Paper only.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kalshi_pbot.config import Settings, coerce_desk_mode
from kalshi_pbot.contracts import (
    BLOCKED_HITL,
    BLOCKED_LANE_MM,
    BusCode,
    BusEvent,
    HITLDecision,
    desk_mode_payload,
    intent_kind_to_mode,
    is_new_risk_mode,
    size_intent_from_quote,
    size_ui_block,
)
from kalshi_pbot.types import IntentKind, PortfolioSnapshot, QuoteIntent

BUS_LEN = 24
DEFAULT_HITL_TIMEOUT = 60


def _now(ts: datetime | None) -> datetime:
    if ts is None:
        return datetime.now(UTC)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def _fingerprint(intent: QuoteIntent) -> str:
    mode = intent_kind_to_mode(intent.kind, reduce_only=intent.reduce_only)
    return f"{intent.market_ticker}:{intent.outcome.value}:{mode}"


@dataclass
class PendingHitl:
    intent_id: str
    quote: QuoteIntent
    size: dict[str, Any]
    queued_at: datetime
    expires_at: datetime
    status: str = "pending"
    market_ticker: str = ""
    event_ticker: str = ""


@dataclass
class HitlDesk:
    settings: Settings
    pending: dict[str, PendingHitl] = field(default_factory=dict)
    by_fingerprint: dict[str, str] = field(default_factory=dict)
    blocked: dict[str, str] = field(default_factory=dict)
    approved: dict[str, PendingHitl] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    bus: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=BUS_LEN))
    _kill_noted: bool = False

    @property
    def mode(self) -> str:
        return coerce_desk_mode(getattr(self.settings, "desk_mode", "PAPER"))

    @property
    def timeout_s(self) -> int:
        return int(getattr(self.settings, "hitl_timeout_seconds", DEFAULT_HITL_TIMEOUT) or 60)

    def desk_mode_payload(self) -> dict[str, Any]:
        return desk_mode_payload(self.mode, self.settings.bankroll)

    def is_paper_auto(self) -> bool:
        return self.mode == "PAPER"

    def is_hitl(self) -> bool:
        return self.mode == "HITL"

    def is_live_blocked(self) -> bool:
        return self.mode == "LIVE_BLOCKED"

    def requires_approval(self, intent: QuoteIntent) -> bool:
        if not self.is_hitl():
            return False
        mode = intent_kind_to_mode(intent.kind, reduce_only=intent.reduce_only)
        return is_new_risk_mode(mode)

    def blocks_new_risk(self, intent: QuoteIntent) -> bool:
        mode = intent_kind_to_mode(intent.kind, reduce_only=intent.reduce_only)
        if not is_new_risk_mode(mode):
            return False
        if self.is_live_blocked():
            return True
        fp = _fingerprint(intent)
        return fp in self.blocked

    def offer(
        self,
        intent: QuoteIntent,
        snapshot: PortfolioSnapshot,
        *,
        bid_sum: object = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Queue a new-risk entry. Returns the SizeIntent payload or None."""
        now = _now(now)
        if not self.requires_approval(intent):
            return None
        fp = _fingerprint(intent)
        if fp in self.blocked:
            size = size_intent_from_quote(
                intent,
                snapshot,
                intent_id=self.blocked[fp],
                settings=self.settings,
                bid_sum=bid_sum,  # type: ignore[arg-type]
                now=now,
                blocked_by=BLOCKED_HITL,
            )
            return size.model_dump()
        existing_id = self.by_fingerprint.get(fp)
        if existing_id and existing_id in self.pending:
            return self.pending[existing_id].size
        intent_id = uuid.uuid4().hex
        size = size_intent_from_quote(
            intent,
            snapshot,
            intent_id=intent_id,
            settings=self.settings,
            bid_sum=bid_sum,  # type: ignore[arg-type]
            now=now,
        )
        payload = size.model_dump()
        item = PendingHitl(
            intent_id=intent_id,
            quote=intent,
            size=payload,
            queued_at=now,
            expires_at=now + timedelta(seconds=self.timeout_s),
            market_ticker=intent.market_ticker,
            event_ticker=intent.event_ticker,
        )
        self.pending[intent_id] = item
        self.by_fingerprint[fp] = intent_id
        return payload

    def decide(
        self,
        intent_id: str,
        decision: str,
        *,
        actor: str = "user",
        now: datetime | None = None,
    ) -> HITLDecision:
        now = _now(now)
        raw = str(decision or "").strip().lower()
        if raw not in {"approve", "deny"}:
            raise ValueError("decision must be approve or deny")
        item = self.pending.get(intent_id)
        if item is None:
            raise KeyError(intent_id)
        if raw == "approve":
            item.status = "approved"
            self.approved[intent_id] = item
            self.pending.pop(intent_id, None)
            self._drop_fingerprint(intent_id)
            record = HITLDecision(
                intent_id=intent_id, decision="approve", actor=actor, ts=now.isoformat()
            )
        else:
            item.status = "denied"
            item.size["blocked_by"] = BLOCKED_HITL
            self.blocked[_fingerprint(item.quote)] = intent_id
            self.pending.pop(intent_id, None)
            record = HITLDecision(
                intent_id=intent_id, decision="deny", actor=actor, ts=now.isoformat()
            )
        dumped = record.model_dump()
        self.decisions.append(dumped)
        return record

    def expire(self, now: datetime | None = None) -> list[HITLDecision]:
        now = _now(now)
        expired: list[HITLDecision] = []
        for intent_id, item in list(self.pending.items()):
            if item.expires_at > now:
                continue
            item.status = "timeout_deny"
            item.size["blocked_by"] = BLOCKED_HITL
            self.blocked[_fingerprint(item.quote)] = intent_id
            self.pending.pop(intent_id, None)
            record = HITLDecision(
                intent_id=intent_id,
                decision="timeout_deny",
                actor="atlas",
                ts=now.isoformat(),
            )
            dumped = record.model_dump()
            self.decisions.append(dumped)
            expired.append(record)
        return expired

    def take_approved(self, intent_id: str | None = None) -> list[PendingHitl]:
        if intent_id is not None:
            item = self.approved.pop(intent_id, None)
            return [item] if item else []
        items = list(self.approved.values())
        self.approved.clear()
        return items

    def pending_payloads(self) -> list[dict[str, Any]]:
        return [item.size for item in self.pending.values()]

    def emit(
        self,
        code: str,
        *,
        ticker: str | None = None,
        detail: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if self.bus:
            last = self.bus[-1]
            if last.get("code") == code and last.get("ticker") == ticker:
                return last
        event = BusEvent(code=code, ts=_now(now).isoformat(), ticker=ticker, detail=detail)  # type: ignore[arg-type]
        payload = event.model_dump()
        self.bus.append(payload)
        return payload

    def note_reason(
        self, reason: str, *, ticker: str | None = None, now: datetime | None = None
    ) -> None:
        text = (reason or "").lower()
        if "last_60" in text or "last_seconds" in text:
            self.emit(BusCode.NO_NEW_RISK.value, ticker=ticker, detail=reason, now=now)
            return
        if "onesided" in text:
            self.emit(BusCode.UNPAIRED_KILL.value, ticker=ticker, detail=reason, now=now)
            return
        if "daily_loss" in text:
            if not self._kill_noted:
                self.emit(BusCode.DAILY_KILL_LATCHED.value, ticker=ticker, detail=reason, now=now)
                self.emit(BusCode.HARD_KILL.value, ticker=ticker, detail=reason, now=now)
                self._kill_noted = True
            return
        if "kill" in text:
            if not self._kill_noted:
                self.emit(BusCode.HARD_KILL.value, ticker=ticker, detail=reason, now=now)
                self._kill_noted = True
            return
        if "abort" in text or "unpaired" in text or "open_notional" in text:
            self.emit(BusCode.SOFT_ABORT.value, ticker=ticker, detail=reason, now=now)

    def note_kill_cleared(self, now: datetime | None = None) -> None:
        self._kill_noted = False
        self.emit(BusCode.KILL_CLEARED.value, now=now)

    def state_payload(self) -> dict[str, Any]:
        return {
            "desk_mode": self.desk_mode_payload(),
            "hitl_queue": self.pending_payloads(),
            "bus_events": list(self.bus),
            "allow_production": bool(self.settings.allow_production),
            "size": size_ui_block(kelly_max=self.settings.kelly_max),
            "hitl_timeout_s": self.timeout_s,
            "blocked_by_scale_in": BLOCKED_LANE_MM,
        }

    def _drop_fingerprint(self, intent_id: str) -> None:
        for fp, iid in list(self.by_fingerprint.items()):
            if iid == intent_id:
                self.by_fingerprint.pop(fp, None)


def flatten_may_bypass_hitl(intent: QuoteIntent) -> bool:
    """Reduce-only / flatten / complete-only do not wait on HITL."""
    if intent.reduce_only or intent.kind in {IntentKind.FLATTEN, IntentKind.CANCEL}:
        return True
    return intent.kind is IntentKind.COMPLETE_PAIR
