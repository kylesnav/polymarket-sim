"""CLI entrypoint for the Polymarket weather bot."""

from __future__ import annotations

import logging
from decimal import Decimal

import structlog
import typer

from src.config import Settings
from src.journal import Journal
from src.noaa import NOAAClient
from src.resolver import resolve_trades
from src.simulator import Simulator

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
) -> None:
    """Run simulation: scan markets, generate signals, execute paper trades."""
    settings = Settings()
    _configure_logging(settings.log_level)

    effective_bankroll = Decimal(str(bankroll))
    logger.info("starting_simulation", bankroll=str(effective_bankroll))

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


@app.command()
def resolve() -> None:
    """Resolve unresolved trades against actual NOAA weather outcomes."""
    settings = Settings()
    _configure_logging(settings.log_level)

    logger.info("starting_trade_resolution")

    journal = Journal()
    noaa = NOAAClient()

    try:
        stats = resolve_trades(journal, noaa)

        typer.echo("=== Trade Resolution Summary ===")
        typer.echo(f"Trades resolved: {stats['resolved_count']}")
        typer.echo(f"Wins: {stats['wins']}")
        typer.echo(f"Losses: {stats['losses']}")
        typer.echo(f"Total actual P&L: ${stats['total_pnl']:+.2f}")  # type: ignore[str-format]
    finally:
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


if __name__ == "__main__":
    app()
