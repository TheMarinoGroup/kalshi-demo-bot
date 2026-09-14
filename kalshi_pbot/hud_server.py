"""Local HUD HTTP + WebSocket server (paper/dry-run; no order POSTs)."""

from __future__ import annotations

import asyncio
import hmac
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from kalshi_pbot.contracts import BLOCKED_LANE_MM, SizeIntentError, parse_size_intent, size_ui_block
from kalshi_pbot.hud_state import MidHistory, build_snapshot

log = structlog.get_logger(__name__)

HUD_DIST = Path(__file__).resolve().parent / "hud_static"


class KillRequest(BaseModel):
    reason: str = Field(default="manual", max_length=120)


class HitlDecisionBody(BaseModel):
    decision: str = Field(..., min_length=3, max_length=16)


class PretradeBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    intent_id: str = "pretrade"
    mode: str
    ticker: str = "unknown"
    side: str = "yes"


class HudHub:
    def __init__(self) -> None:
        self.latest: dict[str, Any] = {}
        self.rev = 0

    def publish(self, snapshot: dict[str, Any]) -> None:
        self.latest = snapshot
        self.rev += 1


def _cors_origins(bot: Any) -> list[str]:
    port = int(getattr(getattr(bot, "settings", None), "hud_port", 8080) or 8080)
    return [
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]


def _attach_desk_cookie(bot: Any, response: Response) -> Response:
    """HttpOnly cookie for same-origin HUD Approve. Never put the token in JSON."""
    token = str(getattr(bot.settings, "desk_token", "") or "")
    if token:
        response.set_cookie(
            key="desk_token",
            value=token,
            httponly=True,
            samesite="strict",
            path="/",
        )
    return response


def create_app(bot: Any, hub: HudHub, history: MidHistory) -> FastAPI:
    app = FastAPI(title="Kalshi Paper Desk", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(bot),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Desk-Token"],
    )

    @app.middleware("http")
    async def desk_token_cookie(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        # Mint the session cookie only on HUD GETs. Never on /v0/state (queue dump)
        # and never on HITL 401s, which would leak the token in Set-Cookie.
        if request.method != "GET":
            return response
        path = request.url.path
        if path.startswith("/v0/"):
            return response
        return _attach_desk_cookie(bot, response)

    def _require_hitl_token(request: Request) -> None:
        expected = str(getattr(bot.settings, "desk_token", "") or "")
        got = request.headers.get("x-desk-token") or request.cookies.get("desk_token") or ""
        if not expected or not hmac.compare_digest(got, expected):
            raise HTTPException(status_code=401, detail="HITL requires X-Desk-Token")
        if bot.settings.live_submit and not getattr(
            bot.settings, "desk_token_from_operator", False
        ):
            raise HTTPException(
                status_code=403,
                detail="HITL approve disabled for --demo-submit without DESK_TOKEN",
            )

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "rev": hub.rev, "hud": True}

    @app.get("/api/snapshot")
    def snapshot() -> dict[str, Any]:
        if not hub.latest:
            hub.publish(build_snapshot(bot, history))
        return hub.latest

    @app.post("/api/kill")
    def trip_kill(body: KillRequest | None = None) -> dict[str, Any]:
        raw = (body.reason if body else "manual") or "manual"
        detail = raw if raw.lower().startswith("manual") else f"manual {raw}"
        bot.risk.trip(detail)
        bot.portfolio.kill_active = True
        bot.portfolio.kill_reason = bot.risk.kill_reason
        if hasattr(bot, "execution"):
            bot.execution.cancel_all()
        snap = build_snapshot(bot, history)
        hub.publish(snap)
        log.warning("hud_manual_kill", reason=bot.risk.kill_reason)
        return {"ok": True, "kill": snap["kill"]}

    @app.get("/v0/state")
    def v0_state() -> dict[str, Any]:
        hitl = getattr(bot, "hitl", None)
        if hitl is None:
            return {
                "desk_mode": {
                    "desk_lane": "MM",
                    "mode": "PAPER",
                    "paper": True,
                    "profile": "dig6_tight",
                    "bankroll": str(bot.settings.bankroll),
                },
                "hitl_queue": [],
                "bus_events": [],
                "allow_production": bool(bot.settings.allow_production),
                "size": size_ui_block(kelly_max=bot.settings.kelly_max),
            }
        payload = hitl.state_payload()
        payload["ts"] = hub.latest.get("ts") if hub.latest else None
        return payload

    @app.post("/v0/hitl/{intent_id}")
    def v0_hitl(intent_id: str, body: HitlDecisionBody, request: Request) -> dict[str, Any]:
        _require_hitl_token(request)
        if not hasattr(bot, "apply_hitl_decision"):
            raise HTTPException(status_code=501, detail="HITL desk not attached")
        try:
            record, submitted = bot.apply_hitl_decision(intent_id, body.decision)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown intent_id {intent_id}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        snap = build_snapshot(bot, history)
        hub.publish(snap)
        dumped = record.model_dump()
        dumped["submitted"] = len(submitted)
        dumped["hitl_queue"] = snap.get("hitl_queue") or []
        return dumped

    @app.post("/v0/risk/pretrade")
    def v0_pretrade(body: PretradeBody) -> dict[str, Any]:
        """Thin RiskPreTradeDecision stub. Kelly is informational; SCALE is LANE_MM."""
        payload = body.model_dump()
        try:
            size = parse_size_intent(payload)
        except SizeIntentError as exc:
            return {
                "allowed": False,
                "blocked_by": exc.blocked_by or BLOCKED_LANE_MM,
                "detail": str(exc),
                "kelly_frac": None,
            }
        except Exception as exc:
            mode = str(payload.get("mode") or "")
            if mode.lower() == "scale_in":
                return {
                    "allowed": False,
                    "blocked_by": BLOCKED_LANE_MM,
                    "detail": str(exc),
                    "kelly_frac": None,
                }
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        blocked = size.blocked_by
        allowed = blocked is None
        return {
            "allowed": allowed,
            "blocked_by": blocked,
            "kelly_frac": size.kelly_frac,
            "mode": size.mode,
            "desk_lane": "MM",
        }

    @app.websocket("/ws")
    async def stream(ws: WebSocket) -> None:
        await ws.accept()
        last = -1
        try:
            while True:
                if hub.rev != last and hub.latest:
                    await ws.send_json(hub.latest)
                    last = hub.rev
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            return
        except Exception:
            log.debug("hud_ws_closed")

    if HUD_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=HUD_DIST / "assets"), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(HUD_DIST / "index.html")

        @app.get("/{path:path}")
        def spa(path: str) -> FileResponse:
            candidate = HUD_DIST / path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(HUD_DIST / "index.html")

    return app


async def serve_hud(bot: Any, hub: HudHub, history: MidHistory) -> None:
    import uvicorn

    app = create_app(bot, hub, history)
    config = uvicorn.Config(
        app,
        host=bot.settings.hud_host,
        port=bot.settings.hud_port,
        log_level="warning",
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
    server = uvicorn.Server(config)
    log.info("hud_listen", host=bot.settings.hud_host, port=bot.settings.hud_port)
    await server.serve()
