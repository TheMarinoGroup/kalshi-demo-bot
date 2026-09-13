"""Persist 15m event windows so rollover survives restarts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from kalshi_pbot.types import D, MarketWindow, Outcome


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def window_to_dict(window: MarketWindow) -> dict[str, object]:
    return {
        "ticker": window.ticker,
        "event_ticker": window.event_ticker,
        "series_ticker": window.series_ticker,
        "title": window.title,
        "status": window.status,
        "open_time": _iso(window.open_time),
        "close_time": _iso(window.close_time),
        "yes_sub_title": window.yes_sub_title,
        "no_sub_title": window.no_sub_title,
        "fee_type": window.fee_type,
        "fee_multiplier": str(window.fee_multiplier),
        "expected_expiration": (
            _iso(window.expected_expiration) if window.expected_expiration else None
        ),
        "settlement_ts": _iso(window.settlement_ts) if window.settlement_ts else None,
        "result": window.result.value if window.result else "",
    }


def window_from_dict(raw: dict[str, object]) -> MarketWindow:
    return MarketWindow(
        ticker=str(raw["ticker"]),
        event_ticker=str(raw.get("event_ticker") or raw["ticker"]),
        series_ticker=str(raw.get("series_ticker") or ""),
        title=str(raw.get("title") or raw["ticker"]),
        status=str(raw.get("status") or ""),
        open_time=_parse(str(raw["open_time"])),
        close_time=_parse(str(raw["close_time"])),
        yes_sub_title=str(raw.get("yes_sub_title") or ""),
        no_sub_title=str(raw.get("no_sub_title") or ""),
        fee_type=str(raw.get("fee_type") or "quadratic"),
        fee_multiplier=D(raw.get("fee_multiplier") or 1),
        expected_expiration=_parse_opt(raw.get("expected_expiration")),
        settlement_ts=_parse_opt(raw.get("settlement_ts")),
        result=_result(raw.get("result")),
    )


def _parse_opt(value: object) -> datetime | None:
    if not value:
        return None
    return _parse(str(value))


def _result(value: object) -> Outcome | None:
    raw = str(value or "").lower()
    if raw == "yes":
        return Outcome.YES
    if raw == "no":
        return Outcome.NO
    return None


class WindowStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.windows: dict[str, MarketWindow] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for raw in payload.get("windows") or []:
            window = window_from_dict(raw)
            self.windows[window.ticker] = window

    def upsert(self, windows: list[MarketWindow]) -> None:
        for window in windows:
            self.windows[window.ticker] = window

    def persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "windows": [window_to_dict(w) for w in sorted(
                self.windows.values(), key=lambda w: w.open_time
            )]
        }
        self.path.write_text(json.dumps(body, indent=2), encoding="utf-8")

    def known(self) -> list[MarketWindow]:
        return list(self.windows.values())
