from __future__ import annotations

from kalshi_pbot.expectancy import replay_tape
from kalshi_pbot.tape import JsonlTape


def test_tape_round_trip_and_replay(tmp_path) -> None:
    path = tmp_path / "tape.jsonl"
    tape = JsonlTape(path)
    tape.write("quote", ticker="T", price="0.48", count="10")
    tape.write(
        "paper_fill",
        ticker="T",
        fee="0",
        notional="4.80",
        latency_ms=150,
    )
    tape.write("ambiguous_wipe", ticker="T")
    report = replay_tape(path)
    assert report.records == 3
    assert report.quotes == 1
    assert report.paper_fills == 1
    assert report.ambiguous_wipes == 1
    assert report.by_latency[150] == 1
