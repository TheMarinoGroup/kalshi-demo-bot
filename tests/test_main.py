from __future__ import annotations

import subprocess
import sys

from kalshi_pbot.cli import app


def test_module_entrypoint_exposes_cli_app() -> None:
    from kalshi_pbot.__main__ import app as main_app

    assert main_app is app


def test_python_m_supports_hud_mock() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "kalshi_pbot", "hud", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    combined = f"{result.stdout}\n{result.stderr}"
    assert "--mock" in combined
