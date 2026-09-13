"""Allow ``python -m kalshi_pbot`` (Windows-friendly alternative to the console script)."""

from kalshi_pbot.cli import app

if __name__ == "__main__":
    app()
