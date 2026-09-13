"""Expectancy / tape replay hook for later 7×24h research runs.

v1 ships the module and a dry replay path. A full multi-day sampler is
out of scope; this is the plug-in surface.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from kalshi_pbot.config import Settings
from kalshi_pbot.tape import JsonlTape
from kalshi_pbot.types import D


@dataclass
class ExpectancyReport:
    records: int = 0
    quotes: int = 0
    paper_fills: int = 0
    ambiguous_wipes: int = 0
    fees: Decimal = Decimal("0")
    fill_notional: Decimal = Decimal("0")
    by_latency: dict[int, int] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)

    @property
    def avg_fill_px_notional(self) -> Decimal:
        if self.paper_fills <= 0:
            return Decimal("0")
        return (self.fill_notional / Decimal(self.paper_fills)).quantize(Decimal("0.0001"))


def replay_tape(path: str | Path, settings: Settings | None = None) -> ExpectancyReport:
    """Aggregate a paper tape. Does not place orders."""
    del settings
    tape = JsonlTape(path)
    report = ExpectancyReport()
    by_lat: dict[int, int] = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    for rec in tape.iter_records():
        report.records += 1
        kind = str(rec.get("kind") or "")
        by_kind[kind] += 1
        if kind == "quote":
            report.quotes += 1
        elif kind == "paper_fill":
            report.paper_fills += 1
            report.fees += D(rec.get("fee") or 0)
            report.fill_notional += D(rec.get("notional") or 0)
            by_lat[int(rec.get("latency_ms") or 0)] += 1
        elif kind == "ambiguous_wipe":
            report.ambiguous_wipes += 1
    report.by_latency = dict(by_lat)
    report.by_kind = dict(by_kind)
    return report
