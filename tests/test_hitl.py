from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kalshi_pbot.config import Settings
from kalshi_pbot.contracts import (
    BLOCKED_LANE_MM,
    SCHEMA_DIR,
    SizeIntentError,
    parse_size_intent,
)
from kalshi_pbot.hud_server import HudHub, create_app
from kalshi_pbot.hud_state import MidHistory, build_snapshot
from kalshi_pbot.runner import PaperBot
from kalshi_pbot.types import IntentKind
from tests.conftest import book


def _underround_bot(*, desk_mode: str = "PAPER", **kwargs: object) -> PaperBot:
    settings = Settings(
        mock=True,
        dry_run=True,
        paper_tape=True,
        series="KXBTC15M",
        desk_mode=desk_mode,  # type: ignore[arg-type]
        **kwargs,
    )
    bot = PaperBot(settings)
    bot.universe.refresh()
    bot.universe.hydrate_books(bot.books)
    return bot


def _hitl_headers(bot: PaperBot) -> dict[str, str]:
    return {"X-Desk-Token": bot.settings.desk_token}


def test_schema_files_exist() -> None:
    names = {
        "HITLDecision.json",
        "DeskMode.json",
        "SizeIntent.v0.1-mm.json",
        "BusEvent.json",
    }
    assert names <= {p.name for p in SCHEMA_DIR.iterdir()}
    size = json.loads((SCHEMA_DIR / "SizeIntent.v0.1-mm.json").read_text())
    assert "scale_in" not in size["properties"]["mode"]["enum"]
    bus = json.loads((SCHEMA_DIR / "BusEvent.json").read_text())
    assert "SoftAbort" in bus["properties"]["code"]["enum"]
    assert bus["properties"]["code"]["enum"].index("SoftAbort") < bus["properties"]["code"][
        "enum"
    ].index("HardKill")


def test_scale_in_rejected_lane_mm() -> None:
    with pytest.raises(SizeIntentError) as exc:
        parse_size_intent(
            {
                "intent_id": "x",
                "mode": "scale_in",
                "ticker": "KXBTC15M-T",
                "side": "yes",
            }
        )
    assert exc.value.blocked_by == BLOCKED_LANE_MM


def test_paper_mode_does_not_require_hitl_for_entry() -> None:
    bot = _underround_bot(desk_mode="PAPER")
    assert bot.settings.desk_mode == "PAPER"
    assert bot.settings.allow_production is False
    submitted = bot.step()
    entries = [q for q in submitted if q.kind is IntentKind.ENTRY]
    assert entries
    assert bot.hitl.pending_payloads() == []
    snap = build_snapshot(bot, MidHistory())
    assert snap["hitl_queue"] == []
    assert snap["desk_mode"]["mode"] == "PAPER"
    assert snap["desk_mode"]["paper"] is True
    assert snap["desk_mode"]["desk_lane"] == "MM"
    assert snap["size"]["scale_in"]["blocked_by"] == BLOCKED_LANE_MM
    assert snap["allow_production"] is False


def test_hitl_approve_allows_one_entry() -> None:
    bot = _underround_bot(desk_mode="HITL")
    submitted = bot.step()
    assert not [q for q in submitted if q.kind is IntentKind.ENTRY]
    pending = bot.hitl.pending_payloads()
    assert pending
    assert pending[0]["mode"] == "entry"
    assert pending[0]["desk_lane"] == "MM"
    intent_id = pending[0]["intent_id"]
    hub = HudHub()
    app = create_app(bot, hub, MidHistory())
    client = TestClient(app)
    state = client.get("/v0/state").json()
    assert state["desk_mode"]["mode"] == "HITL"
    assert state["hitl_queue"]
    assert state["allow_production"] is False
    assert "desk_token" not in state
    res = client.post(
        f"/v0/hitl/{intent_id}",
        json={"decision": "approve"},
        headers=_hitl_headers(bot),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["decision"] == "approve"
    assert body["actor"] == "user"
    assert body["intent_id"] == intent_id
    assert body["submitted"] == 1
    assert bot.execution.dry_run_orders
    assert bot.matcher.orders
    again = bot.step()
    assert not [q for q in again if q.kind is IntentKind.ENTRY]
    snap = client.get("/api/snapshot").json()
    assert snap["hitl_queue"] == []
    assert snap["mode"]["badge"] == "HITL"
    assert snap["mode"]["paper_only"] is True


def test_hitl_deny_blocks_entry() -> None:
    bot = _underround_bot(desk_mode="HITL")
    bot.step()
    pending = bot.hitl.pending_payloads()
    assert pending
    intent_id = pending[0]["intent_id"]
    record, submitted = bot.apply_hitl_decision(intent_id, "deny")
    assert record.decision == "deny"
    assert record.actor == "user"
    assert submitted == []
    assert not bot.execution.dry_run_orders
    later = bot.step()
    assert not [q for q in later if q.kind is IntentKind.ENTRY]
    blocked = [c for c in bot.hitl.decisions if c["intent_id"] == intent_id]
    assert blocked[0]["decision"] == "deny"
    # Fingerprint stays blocked; no new queue card for the same entry.
    assert bot.hitl.pending_payloads() == []


def test_hitl_timeout_deny_blocks() -> None:
    bot = _underround_bot(desk_mode="HITL", hitl_timeout_seconds=60)
    bot.step()
    pending = bot.hitl.pending_payloads()
    assert pending
    intent_id = pending[0]["intent_id"]
    item = bot.hitl.pending[intent_id]
    expired = bot.hitl.expire(item.queued_at + timedelta(seconds=61))
    assert len(expired) == 1
    assert expired[0].decision == "timeout_deny"
    assert expired[0].actor == "atlas"
    assert expired[0].intent_id == intent_id
    later = bot.step()
    assert not [q for q in later if q.kind is IntentKind.ENTRY]
    assert not bot.execution.dry_run_orders
    snap = build_snapshot(bot, MidHistory())
    assert snap["hitl_queue"] == []


def test_live_blocked_allow_production_false() -> None:
    settings = Settings(mock=True, dry_run=True, paper_tape=True, desk_mode="LIVE")  # type: ignore[arg-type]
    assert settings.desk_mode == "LIVE_BLOCKED"
    assert settings.allow_production is False
    bot = PaperBot(settings)
    bot.universe.refresh()
    bot.universe.hydrate_books(bot.books)
    submitted = bot.step()
    assert not [q for q in submitted if q.kind is IntentKind.ENTRY]
    snap = build_snapshot(bot, MidHistory())
    assert snap["mode"]["badge"] == "LIVE_BLOCKED"
    assert snap["mode"]["hard_stop"] is True
    assert snap["allow_production"] is False
    assert snap["desk_mode"]["paper"] is True


def test_desk_mode_env_unprefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESK_MODE", "HITL")
    settings = Settings(mock=True, dry_run=True, paper_tape=True)
    assert settings.desk_mode == "HITL"
    assert settings.allow_production is False


def test_pretrade_stub_rejects_scale_in() -> None:
    bot = _underround_bot(desk_mode="PAPER")
    client = TestClient(create_app(bot, HudHub(), MidHistory()))
    res = client.post(
        "/v0/risk/pretrade",
        json={
            "intent_id": "scale",
            "mode": "scale_in",
            "ticker": "KXBTC15M-T",
            "side": "yes",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["allowed"] is False
    assert body["blocked_by"] == BLOCKED_LANE_MM


def test_snapshot_must_show_hitl_and_size_on_paper_bot() -> None:
    bot = _underround_bot()
    snap = build_snapshot(bot, MidHistory())
    assert snap["mode"]["allow_production"] is False
    assert snap["size"]["family_d"] is False
    assert snap["size"]["scale_in"]["accepted"] is False
    assert snap["extras"]["scale_in_blocked_by"] == "LANE_MM"
    assert snap["desk_mode"]["profile"] == "dig6_tight"
    assert snap["desk_mode"]["bankroll"] in {"500", "500.0", str(bot.settings.bankroll)}
    assert Path("schemas/v0/HITLDecision.json").is_file()
    assert snap["desk_token"]


def test_hitl_post_requires_token() -> None:
    bot = _underround_bot(desk_mode="HITL")
    bot.step()
    pending = bot.hitl.pending_payloads()
    assert pending
    intent_id = pending[0]["intent_id"]
    client = TestClient(create_app(bot, HudHub(), MidHistory()))
    denied = client.post(f"/v0/hitl/{intent_id}", json={"decision": "approve"})
    assert denied.status_code == 401
    assert not bot.execution.dry_run_orders
    ok = client.post(
        f"/v0/hitl/{intent_id}",
        json={"decision": "deny"},
        headers=_hitl_headers(bot),
    )
    assert ok.status_code == 200
    assert ok.json()["decision"] == "deny"


def test_hitl_wrong_token_is_401() -> None:
    bot = _underround_bot(desk_mode="HITL")
    bot.step()
    intent_id = bot.hitl.pending_payloads()[0]["intent_id"]
    client = TestClient(create_app(bot, HudHub(), MidHistory()))
    res = client.post(
        f"/v0/hitl/{intent_id}",
        json={"decision": "approve"},
        headers={"X-Desk-Token": "not-the-desk-token"},
    )
    assert res.status_code == 401
    assert not bot.execution.dry_run_orders


def test_hitl_cors_does_not_allow_foreign_origin() -> None:
    bot = _underround_bot(desk_mode="HITL")
    client = TestClient(create_app(bot, HudHub(), MidHistory()))
    res = client.options(
        "/v0/hitl/intent",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-desk-token",
        },
    )
    assert res.headers.get("access-control-allow-origin") not in {
        "*",
        "http://evil.example",
    }


def test_hitl_demo_submit_refuses_without_operator_token() -> None:
    settings = Settings(
        mock=True,
        dry_run=False,
        paper_tape=False,
        series="KXBTC15M",
        desk_mode="HITL",
    )
    bot = PaperBot(settings)
    bot.universe.refresh()
    bot.universe.hydrate_books(bot.books)
    assert bot.settings.live_submit is True
    assert bot.settings.desk_token_from_operator is False
    bot.step()
    pending = bot.hitl.pending_payloads()
    assert pending
    client = TestClient(create_app(bot, HudHub(), MidHistory()))
    res = client.post(
        f"/v0/hitl/{pending[0]['intent_id']}",
        json={"decision": "approve"},
        headers=_hitl_headers(bot),
    )
    assert res.status_code == 403
    assert not bot.execution.dry_run_orders
    snap = build_snapshot(bot, MidHistory())
    assert not snap.get("desk_token")
    assert "desk_token" not in client.get("/v0/state").json()


def test_demo_submit_snapshot_hides_operator_token() -> None:
    settings = Settings(
        mock=True,
        dry_run=False,
        paper_tape=False,
        series="KXBTC15M",
        desk_mode="HITL",
        desk_token="operator-secret-token",
    )
    assert settings.live_submit is True
    assert settings.desk_token_from_operator is True
    bot = PaperBot(settings)
    snap = build_snapshot(bot, MidHistory())
    assert snap.get("desk_token") == ""
    state = TestClient(create_app(bot, HudHub(), MidHistory())).get("/v0/state").json()
    assert "desk_token" not in state


def test_approve_after_deadline_is_timeout_deny() -> None:
    bot = _underround_bot(desk_mode="HITL", hitl_timeout_seconds=60)
    bot.step()
    pending = bot.hitl.pending_payloads()
    assert pending
    intent_id = pending[0]["intent_id"]
    item = bot.hitl.pending[intent_id]
    late = item.expires_at + timedelta(seconds=1)
    record, submitted = bot.apply_hitl_decision(intent_id, "approve", now=late)
    assert record.decision == "timeout_deny"
    assert record.actor == "atlas"
    assert submitted == []
    assert not bot.execution.dry_run_orders
    assert bot.hitl.take_approved() == []


def test_approve_refuses_stale_underround() -> None:
    bot = _underround_bot(desk_mode="HITL")
    bot.step()
    pending = bot.hitl.pending_payloads()
    assert pending
    intent_id = pending[0]["intent_id"]
    ticker = pending[0]["ticker"]
    stale = book("0.4900", "0.4900")
    stale.ticker = ticker
    bot.books.set(stale)
    record, submitted = bot.apply_hitl_decision(intent_id, "approve")
    assert record.decision == "approve"
    assert submitted == []
    assert not bot.execution.dry_run_orders
