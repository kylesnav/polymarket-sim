"""Tests for hard limit checks."""

from decimal import Decimal

from src.limits import (
    check_bankroll_limit,
    check_daily_loss,
    check_kill_switch,
    check_position_limit,
)


class TestPositionLimit:
    """Tests for check_position_limit."""

    def test_allows_trade_within_cap(self) -> None:
        """Trade within position cap is allowed."""
        allowed, reason = check_position_limit(
            trade_size=Decimal("20"),
            bankroll=Decimal("500"),
            cap_pct=Decimal("0.05"),
        )
        assert allowed is True
        assert reason == "OK"

    def test_rejects_oversized_trade(self) -> None:
        """Trade exceeding position cap is rejected."""
        allowed, reason = check_position_limit(
            trade_size=Decimal("30"),
            bankroll=Decimal("500"),
            cap_pct=Decimal("0.05"),
        )
        assert allowed is False
        assert "exceeds position cap" in reason

    def test_allows_trade_at_exact_cap(self) -> None:
        """Trade at exactly the position cap is allowed."""
        allowed, _ = check_position_limit(
            trade_size=Decimal("25"),
            bankroll=Decimal("500"),
            cap_pct=Decimal("0.05"),
        )
        assert allowed is True


class TestBankrollLimit:
    """Tests for check_bankroll_limit."""

    def test_allows_within_bankroll(self) -> None:
        """Trade within bankroll is allowed."""
        allowed, reason = check_bankroll_limit(
            current_value=Decimal("400"),
            pending=Decimal("50"),
            max_bankroll=Decimal("500"),
        )
        assert allowed is True
        assert reason == "OK"

    def test_rejects_over_bankroll(self) -> None:
        """Trade that exceeds max bankroll is rejected."""
        allowed, reason = check_bankroll_limit(
            current_value=Decimal("480"),
            pending=Decimal("30"),
            max_bankroll=Decimal("500"),
        )
        assert allowed is False
        assert "exceeds max bankroll" in reason

    def test_allows_at_exact_bankroll(self) -> None:
        """Trade at exactly max bankroll is allowed."""
        allowed, _ = check_bankroll_limit(
            current_value=Decimal("450"),
            pending=Decimal("50"),
            max_bankroll=Decimal("500"),
        )
        assert allowed is True


class TestDailyLoss:
    """Tests for check_daily_loss."""

    def test_allows_positive_pnl(self) -> None:
        """Positive P&L is always allowed."""
        allowed, reason = check_daily_loss(
            daily_pnl=Decimal("10"),
            starting_bankroll=Decimal("500"),
        )
        assert allowed is True
        assert reason == "OK"

    def test_allows_small_loss(self) -> None:
        """Loss below the limit is allowed."""
        allowed, reason = check_daily_loss(
            daily_pnl=Decimal("-20"),
            starting_bankroll=Decimal("500"),
            limit_pct=Decimal("0.05"),
        )
        assert allowed is True

    def test_halts_at_loss_limit(self) -> None:
        """Loss at or exceeding -5% halts trading."""
        allowed, reason = check_daily_loss(
            daily_pnl=Decimal("-25"),
            starting_bankroll=Decimal("500"),
            limit_pct=Decimal("0.05"),
        )
        assert allowed is False
        assert "Daily loss" in reason

    def test_halts_at_exact_limit(self) -> None:
        """Exactly -5% triggers the halt."""
        allowed, _ = check_daily_loss(
            daily_pnl=Decimal("-25"),
            starting_bankroll=Decimal("500"),
            limit_pct=Decimal("0.05"),
        )
        assert allowed is False

    def test_zero_pnl_allowed(self) -> None:
        """Zero P&L is allowed."""
        allowed, _ = check_daily_loss(
            daily_pnl=Decimal("0"),
            starting_bankroll=Decimal("500"),
        )
        assert allowed is True


class TestKillSwitch:
    """Tests for check_kill_switch."""

    def test_kill_switch_blocks_when_true(self) -> None:
        """Kill switch blocks all trading when engaged."""
        allowed, reason = check_kill_switch(kill_switch=True)
        assert allowed is False
        assert "Kill switch" in reason

    def test_kill_switch_allows_when_false(self) -> None:
        """Kill switch allows trading when not engaged."""
        allowed, reason = check_kill_switch(kill_switch=False)
        assert allowed is True
        assert reason == "OK"
