"""Local HUD HTTP + WebSocket server (paper/dry-run; no order POSTs)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from kalshi_pbot.hud_state import MidHistory, build_snapshot

log = structlog.get_logger(__name__)

HUD_DIST = Path(__file__).resolve().parent / "hud_static"


class HudHub:
    def __init__(self) -> None:
        self.latest: dict[str, Any] = {}
        self.rev = 0

    def publish(self, snapshot: dict[str, Any]) -> None:
        self.latest = snapshot
        self.rev += 1


def create_app(bot: Any, hub: HudHub, history: MidHistory) -> FastAPI:
    app = FastAPI(title="Kalshi Paper Desk", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "rev": hub.rev, "hud": True}

    @app.get("/api/snapshot")
    def snapshot() -> dict[str, Any]:
        if not hub.latest:
            hub.publish(build_snapshot(bot, history))
        return hub.latest

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
