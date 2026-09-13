from __future__ import annotations

from kalshi_pbot.config import Settings
from kalshi_pbot.hud_state import MidHistory, build_snapshot
from kalshi_pbot.runner import PaperBot


def test_snapshot_from_mock_bot() -> None:
    bot = PaperBot(Settings(mock=True, dry_run=True, paper_tape=True, series="KXBTC15M"))
    bot.universe.refresh()
    bot.universe.hydrate_books(bot.books)
    bot.step()
    snap = build_snapshot(bot, MidHistory())
    assert snap["mode"]["paper_tape"] is True
    assert snap["mode"]["live_submit"] is False
    assert snap["risk"]["min_window_minutes"] == 15
    assert snap["risk"]["bankroll"] == 1000
    assert snap["windows"]
    assert all(w["series"].endswith("15M") or "H" in w["series"] for w in snap["windows"])
