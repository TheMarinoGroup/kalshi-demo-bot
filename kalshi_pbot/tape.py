"""Append-only JSONL paper tape.

v1 writes live research records. Later 7×24h expectancy runs replay this
file through ``kalshi_pbot.expectancy.replay_tape``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

SCHEMA_VERSION = 1


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


class JsonlTape:
    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **fields: Any) -> dict[str, Any]:
        record = {
            "v": SCHEMA_VERSION,
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            **{k: _jsonable(v) for k, v in fields.items()},
        }
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        return record

    def iter_records(self) -> list[dict[str, Any]]:
        if not self.path or not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                out.append(json.loads(line))
        return out
