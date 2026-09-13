"""CLI for the Kalshi demo/paper bot."""

from __future__ import annotations

from typing import Annotated

import structlog
import typer

from kalshi_pbot.config import LATENCY_BUCKETS, Settings, load_settings
from kalshi_pbot.logging_setup import configure_logging

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Kalshi demo/paper bot for 15-minute crypto Up/Down market-making.",
)
log = structlog.get_logger(__name__)


def _settings(
    *,
    dry_run: bool | None = None,
    demo_submit: bool = False,
    mock: bool = False,
    series: str | None = None,
    bankroll: float | None = None,
    latency: int | None = None,
    tape: str | None = None,
) -> Settings:
    overrides: dict[str, object] = {}
    if mock:
        overrides["mock"] = True
        overrides["dry_run"] = True
        overrides["paper_tape"] = True
    if demo_submit:
        overrides["dry_run"] = False
        overrides["paper_tape"] = False
        overrides["mock"] = False
    elif dry_run is True:
        overrides["dry_run"] = True
        overrides["paper_tape"] = True
    if series:
        overrides["series"] = series
    if bankroll is not None:
        overrides["bankroll"] = bankroll
    if latency is not None:
        overrides["latency_ms"] = latency
    if tape:
        overrides["tape_path"] = tape
    settings = load_settings(**overrides)
    if demo_submit and not settings.has_credentials():
        raise typer.BadParameter(
            "--demo-submit needs KALSHI_API_KEY_ID and a private key path or PEM."
        )
    if demo_submit and settings.env != "demo" and not settings.allow_production:
        raise typer.BadParameter("Refusing non-demo submit without KALSHI_ALLOW_PRODUCTION=1")
    return settings


@app.command()
def run(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--no-dry-run", help="Log orders only (default)."),
    ] = True,
    demo_submit: Annotated[
        bool,
        typer.Option(
            "--demo-submit",
            help="Place/cancel orders on the Kalshi DEMO API. Off by default.",
        ),
    ] = False,
    mock: Annotated[
        bool,
        typer.Option("--mock", help="Use synthetic KXBTC15M/KXETH15M books (implies dry-run)."),
    ] = False,
    series: Annotated[
        str | None,
        typer.Option("--series", help="Comma-separated series tickers."),
    ] = None,
    bankroll: Annotated[
        float | None,
        typer.Option("--bankroll", help="Paper bankroll; risk limits rescale."),
    ] = None,
    latency: Annotated[
        int,
        typer.Option(
            "--latency",
            help="Paper-matcher latency bucket in ms (50, 150, or 500).",
        ),
    ] = 150,
    tape: Annotated[
        str | None,
        typer.Option("--tape", help="JSONL paper-tape path."),
    ] = None,
) -> None:
    """Discover windows, evaluate maker/pair quotes, and paper-tape or demo-submit."""
    if latency not in LATENCY_BUCKETS:
        raise typer.BadParameter(f"--latency must be one of {LATENCY_BUCKETS}")
    settings = _settings(
        dry_run=dry_run,
        demo_submit=demo_submit,
        mock=mock,
        series=series,
        bankroll=bankroll,
        latency=latency,
        tape=tape,
    )
    configure_logging(settings.log_level, settings.log_json)
    if demo_submit:
        log.warning("demo_submit_enabled", order_rest=settings.resolved_order_rest)
    from kalshi_pbot.runner import run_bot

    run_bot(settings)


@app.command()
def discover(
    mock: Annotated[bool, typer.Option("--mock")] = False,
    series: Annotated[str | None, typer.Option("--series")] = None,
) -> None:
    """List open + unopened 15-minute windows via GET /events (prod public REST)."""
    settings = _settings(mock=mock, series=series)
    configure_logging(settings.log_level, settings.log_json)
    from kalshi_pbot.runner import discover_once

    markets = discover_once(settings)
    if not markets:
        typer.echo("No open 15m windows found (try --mock).")
        raise typer.Exit(code=0)
    for market in markets:
        typer.echo(
            f"{market.series_ticker:10} {market.ticker:28} "
            f"open={market.open_time.isoformat()} close={market.close_time.isoformat()} "
            f"fee={market.fee_type}×{market.fee_multiplier}"
        )


@app.command()
def status() -> None:
    """Print resolved config and Risk Desk v1 limits (no secrets)."""
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_json)
    typer.echo(f"env              {settings.env}")
    typer.echo(f"data_rest        {settings.resolved_data_rest}")
    typer.echo(f"order_rest       {settings.resolved_order_rest}")
    typer.echo(f"ws               {settings.resolved_ws_url}")
    typer.echo(f"dry_run          {settings.dry_run}")
    typer.echo(f"paper_tape       {settings.paper_tape}")
    typer.echo(f"latency_ms       {settings.latency_ms}")
    typer.echo(f"live_submit      {settings.live_submit}")
    typer.echo(f"credentials      {settings.has_credentials()}")
    typer.echo(f"series           {','.join(settings.series_tickers)}")
    typer.echo(f"bankroll         ${settings.bankroll}")
    typer.echo(f"clip             ${settings.clip}  (band $10–$30)")
    typer.echo(f"max_open         ${settings.max_open_notional}  (5%)")
    typer.echo(f"daily_loss_kill  ${settings.daily_loss_limit}  (2%)")
    typer.echo(f"max_onesided     ${settings.max_onesided}  (3%)")
    typer.echo(f"max_windows      {settings.max_windows}")
    typer.echo(f"last_seconds     {settings.last_seconds}")
    typer.echo(
        f"settle_recycle   {settings.effective_settle_recycle_seconds}s  "
        f"(close→settlement p99≈59s; not expected_expiration)"
    )
    typer.echo(
        f"settle_rare_tail {settings.settle_rare_tail}  ({settings.settle_rare_tail_seconds}s)"
    )
    typer.echo(f"quote_mode       {settings.quote_mode}")
    typer.echo(f"min_edge         {settings.min_edge}")
    typer.echo(f"taker_pair_arb   {settings.taker_pair_arb}")
    typer.echo(f"windows_path     {settings.windows_path}")
    typer.echo(f"tape_path        {settings.tape_path}")


@app.command()
def replay(
    tape: Annotated[
        str,
        typer.Argument(help="JSONL paper tape to aggregate."),
    ] = "data/tape.jsonl",
) -> None:
    """Replay a paper tape into an expectancy summary (no orders)."""
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_json)
    from kalshi_pbot.expectancy import replay_tape

    report = replay_tape(tape, settings)
    typer.echo(f"records          {report.records}")
    typer.echo(f"quotes           {report.quotes}")
    typer.echo(f"paper_fills      {report.paper_fills}")
    typer.echo(f"ambiguous_wipes  {report.ambiguous_wipes}")
    typer.echo(f"fees             {report.fees}")
    typer.echo(f"fill_notional    {report.fill_notional}")
    typer.echo(f"by_latency       {report.by_latency}")
    typer.echo(f"by_kind          {report.by_kind}")


@app.command()
def flatten(
    demo_submit: Annotated[bool, typer.Option("--demo-submit")] = False,
) -> None:
    """Cancel all resting orders. Demo-submit required to hit the API."""
    settings = _settings(demo_submit=demo_submit, dry_run=not demo_submit)
    configure_logging(settings.log_level, settings.log_json)
    from kalshi_pbot.execution import ExecutionEngine
    from kalshi_pbot.kalshi_client import KalshiClient
    from kalshi_pbot.portfolio import Portfolio

    if settings.dry_run or settings.paper_tape:
        log.info("flatten_dry_run", hint="pass --demo-submit to cancel demo orders")
        return
    client = KalshiClient(settings)
    try:
        engine = ExecutionEngine(settings, Portfolio(settings), client.rest)
        result = engine.cancel_all()
        log.info("flatten_done", **{k: str(v) for k, v in result.items()})
    finally:
        client.close()


@app.command("reset-kill")
def reset_kill() -> None:
    """Document how to clear a latched kill switch (process restart + this flag)."""
    typer.echo(
        "The kill switch is in-process. Restart the bot after the daily loss "
        "is understood. There is no remote reset — that is intentional."
    )


if __name__ == "__main__":
    app()
