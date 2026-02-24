"""CLI entrypoint for the Polymarket weather bot."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
import typer

from src.config import Settings
from src.journal import Journal
from src.noaa import NOAAClient
from src.polymarket import PolymarketClient
from src.resolver import resolve_trades
from src.simulator import Simulator

if TYPE_CHECKING:
    from src.models import Signal

logger = structlog.get_logger()

app = typer.Typer(help="Polymarket weather bot — NOAA-based simulation")

_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _configure_logging(level: str) -> None:
    """Configure structlog with the given log level.

    Args:
        level: Log level string (e.g., "INFO", "DEBUG").
    """
    log_level = _LOG_LEVELS.get(level.upper(), logging.INFO)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )


@app.command()
def scan() -> None:
    """Scan for weather markets with edge, print signals."""
    settings = Settings()
    _configure_logging(settings.log_level)

    logger.info("starting_scan")

    sim = Simulator(
        bankroll=Decimal(str(settings.max_bankroll)),
        min_edge=Decimal(str(settings.min_edge_threshold)),
        kelly_fraction=Decimal(str(settings.kelly_fraction)),
        position_cap_pct=Decimal(str(settings.position_cap_pct)),
        max_bankroll=Decimal(str(settings.max_bankroll)),
        daily_loss_limit_pct=Decimal(str(settings.daily_loss_limit_pct)),
        kill_switch=settings.kill_switch,
    )

    try:
        signals = sim.run_scan()

        if not signals:
            typer.echo("No actionable signals found.")
            return

        # Print signals table
        typer.echo(
            f"{'Market':<45} {'NOAA':>6} {'Market':>7} {'Edge':>7} {'Size ($)':>9}"
        )
        typer.echo("-" * 78)
        for signal in signals:
            typer.echo(
                f"{signal.market_id[:44]:<45} "
                f"{signal.noaa_probability:>6.2f} "
                f"{signal.market_price:>7.2f} "
                f"{signal.edge:>+7.2f} "
                f"${signal.recommended_size:>8.2f}"
            )
        typer.echo(f"\nFound {len(signals)} signal(s) above threshold.")
    finally:
        sim.close()


@app.command()
def sim(
    bankroll: float = typer.Option(500.0, help="Starting bankroll in dollars"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show hypothetical P&L without writing trades"
    ),
) -> None:
    """Run simulation: scan markets, generate signals, execute paper trades."""
    settings = Settings()
    _configure_logging(settings.log_level)

    effective_bankroll = Decimal(str(bankroll))
    logger.info(
        "starting_simulation",
        bankroll=str(effective_bankroll),
        dry_run=dry_run,
    )

    simulator = Simulator(
        bankroll=effective_bankroll,
        min_edge=Decimal(str(settings.min_edge_threshold)),
        kelly_fraction=Decimal(str(settings.kelly_fraction)),
        position_cap_pct=Decimal(str(settings.position_cap_pct)),
        max_bankroll=Decimal(str(settings.max_bankroll)),
        daily_loss_limit_pct=Decimal(str(settings.daily_loss_limit_pct)),
        kill_switch=settings.kill_switch,
    )

    try:
        # Scan for signals
        signals = simulator.run_scan()

        if not signals:
            typer.echo("No actionable signals found.")
            return

        typer.echo(f"Found {len(signals)} signal(s) above threshold.")

        if dry_run:
            _print_dry_run(signals)
            return

        # Execute paper trades
        trades = simulator.execute_signals(signals)

        if trades:
            typer.echo(f"Executed {len(trades)} paper trade(s).")
            for trade in trades:
                typer.echo(
                    f"  {trade.side} {trade.market_id[:30]} "
                    f"@ {trade.price} | size: ${trade.size} | edge: {trade.edge:+.2f}"
                )

        portfolio = simulator.get_portfolio()
        typer.echo(f"\nDaily P&L: ${portfolio.daily_pnl:+.2f} (sim)")
        typer.echo(f"Bankroll: ${portfolio.total_value:.2f}")
    finally:
        simulator.close()


def _print_dry_run(signals: list[Signal]) -> None:
    """Print hypothetical P&L summary for signals without writing to journal.

    Uses expected value (edge * size) as the projected P&L per signal.

    Args:
        signals: Trading signals from the scan.
    """

    typer.echo("\n=== Dry Run: Hypothetical P&L ===")
    typer.echo(
        f"{'Market':<40} {'Side':<5} {'NOAA':>6} {'Mkt':>6} "
        f"{'Edge':>7} {'Size ($)':>9} {'Proj P&L':>10}"
    )
    typer.echo("-" * 88)

    total_pnl = Decimal("0")
    total_size = Decimal("0")

    for signal in signals:
        proj_pnl = signal.edge * signal.recommended_size
        total_pnl += proj_pnl
        total_size += signal.recommended_size

        typer.echo(
            f"{signal.market_id[:39]:<40} {signal.side:<5} "
            f"{signal.noaa_probability:>6.2f} {signal.market_price:>6.2f} "
            f"{signal.edge:>+7.2f} ${signal.recommended_size:>8.2f} "
            f"${proj_pnl:>+9.2f}"
        )

    typer.echo("-" * 88)
    typer.echo(
        f"{'TOTAL':<40} {'':<5} {'':<6} {'':<6} "
        f"{'':>7} ${total_size:>8.2f} ${total_pnl:>+9.2f}"
    )
    typer.echo(
        f"\n{len(signals)} signal(s) | "
        f"Total exposure: ${total_size:.2f} | "
        f"Projected P&L: ${total_pnl:+.2f}"
    )
    typer.echo("(No trades written to journal)")


@app.command()
def positions() -> None:
    """Show open positions with estimated P&L (max, expected)."""
    settings = Settings()
    _configure_logging(settings.log_level)

    journal = Journal()
    try:
        data = journal.get_open_positions_with_pnl()
        pos_list = data["positions"]
        summary = data["summary"]

        if not pos_list:
            typer.echo("No open positions.")
            return

        typer.echo(f"=== Open Positions ({summary['position_count']}) ===\n")
        typer.echo(
            f"{'Market':<40} {'Side':<5} {'Stake':>8} {'NOAA':>6} "
            f"{'Edge':>7} {'MaxProfit':>10} {'Expected':>10} {'Event':>12}"
        )
        typer.echo("-" * 104)

        for pos in pos_list:
            question = pos["question"][:39] if pos["question"] else pos["market_id"][:39]
            event_str = ""
            if pos["event_date"]:
                event_str = str(pos["event_date"])
                if pos["days_until_event"] is not None:
                    event_str += f" ({pos['days_until_event']}d)"

            typer.echo(
                f"{question:<40} {pos['side']:<5} "
                f"${pos['size']:>7.2f} "
                f"{pos['noaa_probability']:>6.2f} "
                f"{pos['edge']:>+7.2f} "
                f"${pos['max_profit']:>+9.2f} "
                f"${pos['expected_pnl']:>+9.2f} "
                f"{event_str:>12}"
            )

        typer.echo("-" * 104)
        typer.echo("\n=== Portfolio P&L Estimate ===")
        typer.echo(f"Total Exposure:    ${summary['total_exposure']:.2f}")
        typer.echo(
            f"Expected P&L:      ${summary['total_expected_pnl']:+.2f}"
            f" ({float(summary['total_expected_return']):.1%} return)"
        )
        typer.echo(
            f"Max Profit:        ${summary['total_max_profit']:+.2f}"
            f" (best case — all bets win)"
        )
        typer.echo(
            f"Max Loss:          ${summary['total_max_loss']:+.2f}"
            f" (worst case — all bets lose)"
        )

        # Also show realized P&L if any
        report = journal.get_report_data(days=365)
        if report["resolved_trades"]:
            typer.echo(f"Actual P&L:        ${report['actual_pnl']:+.2f} (resolved bets)")
    finally:
        journal.close()


@app.command()
def events() -> None:
    """Scan for multi-outcome weather events with bucket-level signals."""
    settings = Settings()
    _configure_logging(settings.log_level)

    logger.info("starting_event_scan")

    sim = Simulator(
        bankroll=Decimal(str(settings.max_bankroll)),
        min_edge=Decimal(str(settings.min_edge_threshold)),
        kelly_fraction=Decimal(str(settings.kelly_fraction)),
        position_cap_pct=Decimal(str(settings.position_cap_pct)),
        max_bankroll=Decimal(str(settings.max_bankroll)),
        daily_loss_limit_pct=Decimal(str(settings.daily_loss_limit_pct)),
        kill_switch=settings.kill_switch,
    )

    try:
        signals = sim.run_event_scan()

        if not signals:
            typer.echo("No actionable bucket signals found.")
            return

        typer.echo(
            f"{'Event':<30} {'Bucket':<20} {'Side':<5} "
            f"{'NOAA':>6} {'Mkt':>6} {'Edge':>7} {'Size ($)':>9}"
        )
        typer.echo("-" * 88)
        for signal in signals:
            typer.echo(
                f"{signal.event_id[:29]:<30} "
                f"{signal.outcome_label[:19]:<20} "
                f"{signal.side:<5} "
                f"{signal.noaa_probability:>6.2f} "
                f"{signal.market_price:>6.2f} "
                f"{signal.edge:>+7.2f} "
                f"${signal.recommended_size:>8.2f}"
            )
        typer.echo(f"\nFound {len(signals)} bucket signal(s) above threshold.")
    finally:
        sim.close()


@app.command()
def resolve() -> None:
    """Resolve unresolved trades using Polymarket resolution data."""
    settings = Settings()
    _configure_logging(settings.log_level)

    logger.info("starting_trade_resolution")

    journal = Journal()
    polymarket = PolymarketClient()
    noaa = NOAAClient()

    try:
        stats = resolve_trades(journal, polymarket, noaa)

        typer.echo("=== Trade Resolution Summary ===")
        typer.echo(f"Trades resolved: {stats['resolved_count']}")
        typer.echo(f"Wins: {stats['wins']}")
        typer.echo(f"Losses: {stats['losses']}")
        typer.echo(f"Total actual P&L: ${stats['total_pnl']:+.2f}")  # type: ignore[str-format]
    finally:
        polymarket.close()
        noaa.close()
        journal.close()


@app.command()
def report(
    days: int = typer.Option(30, help="Number of days of history to show"),
) -> None:
    """Show paper P&L summary from the trade journal."""
    settings = Settings()
    _configure_logging(settings.log_level)

    journal = Journal()
    try:
        data = journal.get_report_data(days)

        typer.echo(f"=== {days}-day Paper Trading Report ===")
        typer.echo(f"Trades executed: {data['filled_trades']}")  # type: ignore[str-format]
        typer.echo(f"Trades resolved: {data['resolved_trades']}")  # type: ignore[str-format]
        typer.echo()
        typer.echo("Simulated P&L (edge-based):")
        typer.echo(f"  P&L: ${data['simulated_pnl']:+.2f}")  # type: ignore[str-format]
        typer.echo(
            f"  Wins: {data['wins']} | Losses: {data['losses']} | "
            f"Win rate: {data['win_rate']:.0%}"  # type: ignore[str-format]
        )
        if data['resolved_trades']:  # type: ignore[comparison-overlap]
            typer.echo()
            typer.echo("Actual P&L (resolved trades):")
            typer.echo(f"  P&L: ${data['actual_pnl']:+.2f}")  # type: ignore[str-format]
            typer.echo(
                f"  Wins: {data['actual_wins']} | Losses: {data['actual_losses']} | "
                f"Win rate: {data['actual_win_rate']:.0%}"  # type: ignore[str-format]
            )
        typer.echo()
        typer.echo(
            f"Avg edge: {data['avg_edge']:.1%} | "  # type: ignore[str-format]
            f"Avg position: ${data['avg_size']:.2f}"  # type: ignore[str-format]
        )
    finally:
        journal.close()


@app.command()
def status() -> None:
    """Show current configuration and safety rail status."""
    settings = Settings()
    _configure_logging(settings.log_level)

    typer.echo("=== Polymarket Weather Bot Status ===")
    typer.echo(f"Max Bankroll:      ${settings.max_bankroll:.2f}")
    typer.echo(f"Position Cap:      {settings.position_cap_pct:.0%}")
    typer.echo(f"Kelly Fraction:    {settings.kelly_fraction}")
    typer.echo(f"Min Edge:          {settings.min_edge_threshold:.0%}")
    typer.echo(f"Daily Loss Limit:  {settings.daily_loss_limit_pct:.0%}")
    typer.echo(f"Kill Switch:       {'ENGAGED' if settings.kill_switch else 'OFF'}")
    typer.echo(f"Log Level:         {settings.log_level}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Host to bind the web server to"),
    port: int = typer.Option(8000, help="Port to run the web server on"),
) -> None:
    """Start the web dashboard server.

    Opens the Weather Edge Tracker UI in your browser.
    Everything available in the CLI is also available through the web interface.
    """
    import uvicorn  # noqa: PLC0415

    settings = Settings()
    _configure_logging(settings.log_level)

    typer.echo(f"Starting Weather Edge Tracker at http://{host}:{port}")
    typer.echo("Press Ctrl+C to stop.\n")

    uvicorn.run(
        "src.server:app",
        host=host,
        port=port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    app()
