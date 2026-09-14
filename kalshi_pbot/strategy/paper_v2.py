"""Paper-v2 Option B state machine (PAPER only).

Option B hard caps at $500 bankroll (same 5% / 3% / 2% fractions):
open $25, onesided $15, daily kill $10. Clip $10. One window. BTC-only.

Daily kill $10 = 1× clip, so the soft layer is mandatory:
complete the other side after any touch; abort above $10 or after 45s;
never open a new one-sided clip while unpaired exists.

Does not switch to two_sided. Does not POST production orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from kalshi_pbot.config import Settings
from kalshi_pbot.risk_engine import (
    has_unpaired_inventory,
    in_last_seconds,
    should_abort_unpaired,
    unpaired_abort_reason,
)
from kalshi_pbot.types import PortfolioSnapshot, Position


class PaperV2State(StrEnum):
    """Quote → touch → complete → abort. Never two_sided."""

    FLAT = "flat"  # no inventory — may open one one-sided clip
    COMPLETE = "complete"  # unpaired on this ticker — quote the other side only
    SOFT_ABORT = "soft_abort"  # flatten (soft $10 / age 45s / cannot complete)
    BLOCK_NEW = "block_new"  # unpaired elsewhere — no new onesided
    LAST_SECONDS = "last_seconds"  # no new risk; flatten only
    HARD_KILL = "hard_kill"  # Risk Desk kill (open / onesided / daily)


@dataclass(frozen=True)
class PaperV2Decision:
    state: PaperV2State
    reason: str

    @property
    def allow_new_onesided(self) -> bool:
        return self.state is PaperV2State.FLAT

    @property
    def flatten(self) -> bool:
        return self.state in {PaperV2State.SOFT_ABORT, PaperV2State.LAST_SECONDS}

    @property
    def complete_other_side(self) -> bool:
        return self.state is PaperV2State.COMPLETE


def _local_position(snapshot: PortfolioSnapshot, ticker: str) -> Position | None:
    return snapshot.positions.get(ticker)


def _unpaired_usd(snapshot: PortfolioSnapshot) -> Decimal:
    if snapshot.unpaired_notional > 0:
        return snapshot.unpaired_notional
    return sum((pos.unpaired_notional() for pos in snapshot.positions.values()), Decimal("0"))


def classify_paper_v2(
    snapshot: PortfolioSnapshot,
    ticker: str,
    settings: Settings,
    *,
    now: datetime | None = None,
    close_time: datetime | None = None,
    can_complete: bool = True,
) -> PaperV2Decision:
    """Classify the next paper-v2 action for one ticker."""
    if snapshot.kill_active:
        return PaperV2Decision(PaperV2State.HARD_KILL, snapshot.kill_reason or "kill")
    if _unpaired_usd(snapshot) >= settings.max_onesided:
        return PaperV2Decision(PaperV2State.HARD_KILL, "onesided_hard")
    if snapshot.daily_pnl <= -settings.daily_loss_limit:
        return PaperV2Decision(PaperV2State.HARD_KILL, "daily_loss")
    if snapshot.open_notional >= settings.max_open_notional:
        return PaperV2Decision(PaperV2State.HARD_KILL, "open_notional")

    pos = _local_position(snapshot, ticker)
    local_unpaired = pos is not None and pos.unpaired_qty > 0

    if close_time is not None and in_last_seconds(close_time, settings.last_seconds, now):
        return PaperV2Decision(PaperV2State.LAST_SECONDS, "last_seconds")

    if local_unpaired and pos is not None:
        if should_abort_unpaired(pos, settings, now):
            return PaperV2Decision(
                PaperV2State.SOFT_ABORT, unpaired_abort_reason(pos, settings, now)
            )
        if not can_complete:
            return PaperV2Decision(PaperV2State.SOFT_ABORT, "unpaired_cannot_complete")
        return PaperV2Decision(PaperV2State.COMPLETE, "complete_after_touch")

    if has_unpaired_inventory(snapshot):
        return PaperV2Decision(PaperV2State.BLOCK_NEW, "unpaired_exists")

    return PaperV2Decision(PaperV2State.FLAT, "quote_one_sided")
