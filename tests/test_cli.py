from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_start_hud_bat_is_ascii_launcher() -> None:
    text = Path("start-hud.bat").read_text(encoding="ascii")
    assert "cd /d \"%~dp0\"" in text
    assert "python -m kalshi_pbot hud" in text
    assert "http://127.0.0.1:8080" in text
    assert "Ctrl+C" in text


def test_python_module_cli_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "kalshi_pbot", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "hud" in result.stdout.lower()
