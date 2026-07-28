"""
tests/test_risk.py — Unit tests for RiskManager

These cover position sizing, adaptive risk adjustment, and the daily
circuit breakers. Thresholds are read from config rather than hardcoded,
so the tests validate behavior rather than restating magic numbers.
"""

import pytest

import config
import risk


STARTING_BALANCE = 25_000.0


@pytest.fixture
def rm(tmp_path, monkeypatch):
    """
    A RiskManager isolated from the real state file.

    RiskManager loads persistent state from disk on construction. Pointing
    STATE_FILE at a temp directory keeps the developer's live trading state
    out of the tests, and keeps the tests from writing to it.
    """
    monkeypatch.setattr(risk, "STATE_FILE", str(tmp_path / "state.json"))
    manager = risk.RiskManager(STARTING_BALANCE)
    manager.consecutive_wins = 0
    manager.consecutive_losses = 0
    return manager


# ─────────────────────────────────────────
# Adaptive risk
# ─────────────────────────────────────────

def test_adaptive_risk_returns_base_when_flat(rm):
    """With no win or loss streak, risk should sit at the configured base."""
    assert rm._adaptive_risk() == config.BASE_RISK


def test_adaptive_risk_never_drops_below_floor(rm):
    """A long losing streak reduces risk, but never past MIN_RISK."""
    rm.consecutive_losses = 50
    assert rm._adaptive_risk() >= config.MIN_RISK


def test_adaptive_risk_never_exceeds_ceiling(rm):
    """A long winning streak increases risk, but never past MAX_RISK."""
    rm.consecutive_wins = 50
    assert rm._adaptive_risk() <= config.MAX_RISK


def test_losing_streak_reduces_risk(rm):
    """Risk after hitting the loss threshold is lower than baseline."""
    baseline = rm._adaptive_risk()
    rm.consecutive_losses = config.ADAPTIVE_LOSS_THRESHOLD
    assert rm._adaptive_risk() <= baseline


# ─────────────────────────────────────────
# Position sizing
# ─────────────────────────────────────────

def test_position_size_is_at_least_one_contract(rm):
    """Even an absurdly wide stop should never size down to zero."""
    contracts = rm.calculate_position_size("MNQ", risk_per_point=100_000)
    assert contracts >= 1


def test_tighter_stop_allows_more_contracts(rm):
    """Risk per point is the denominator — a tighter stop buys more size."""
    tight = rm.calculate_position_size("MNQ", risk_per_point=5)
    wide = rm.calculate_position_size("MNQ", risk_per_point=50)
    assert tight > wide


def test_contract_point_value_affects_sizing(rm):
    """
    MES carries a higher dollar-per-point than MNQ, so the same stop
    distance should permit fewer MES contracts.
    """
    assert config.CONTRACT_SPECS["MES"] > config.CONTRACT_SPECS["MNQ"]
    mnq = rm.calculate_position_size("MNQ", risk_per_point=10)
    mes = rm.calculate_position_size("MES", risk_per_point=10)
    assert mes <= mnq


def test_ranging_regime_does_not_increase_size(rm):
    """Ranging markets halve risk, so size must not grow."""
    trending = rm.calculate_position_size("MNQ", risk_per_point=10, regime="TRENDING")
    ranging = rm.calculate_position_size("MNQ", risk_per_point=10, regime="RANGING")
    assert ranging <= trending


def test_news_factor_scales_size_down(rm):
    """A news factor below 1.0 should not produce a larger position."""
    normal = rm.calculate_position_size("MNQ", risk_per_point=10, news_factor=1.0)
    damped = rm.calculate_position_size("MNQ", risk_per_point=10, news_factor=0.5)
    assert damped <= normal


def test_unknown_symbol_falls_back_to_default_point_value(rm):
    """An unrecognized symbol should still return a usable size, not crash."""
    contracts = rm.calculate_position_size("XYZ", risk_per_point=10)
    assert contracts >= 1


# ─────────────────────────────────────────
# Consistency cap
# ─────────────────────────────────────────

def test_consistency_cap_is_passthrough(rm):
    """
    The consistency rule is a pass-time look-back, not a mid-day blocker.
    This test pins that intentional decision so a future change to
    _apply_consistency_cap is a deliberate one, not an accident.
    """
    assert rm._apply_consistency_cap(250.0) == 250.0


# ─────────────────────────────────────────
# Daily circuit breakers
# ─────────────────────────────────────────

def test_fresh_session_allows_trading(rm):
    can_trade, reason = rm.check_daily_limits()
    assert can_trade is True
    assert reason == "OK"


def test_max_trades_blocks_further_trading(rm):
    rm.trades_today = config.MAX_TRADES_PER_DAY
    can_trade, reason = rm.check_daily_limits()
    assert can_trade is False
    assert "Max trades" in reason


def test_daily_loss_limit_blocks_and_sets_flag(rm):
    loss_limit = STARTING_BALANCE * config.DAILY_LOSS_LIMIT
    rm.daily_pnl = -loss_limit
    can_trade, reason = rm.check_daily_limits()
    assert can_trade is False
    assert rm.daily_loss_hit is True


def test_small_loss_does_not_block(rm):
    loss_limit = STARTING_BALANCE * config.DAILY_LOSS_LIMIT
    rm.daily_pnl = -(loss_limit * 0.5)
    can_trade, _ = rm.check_daily_limits()
    assert can_trade is True


def test_profit_lock_blocks_and_sets_flag(rm):
    profit_lock = STARTING_BALANCE * config.PROFIT_LOCK_PCT
    rm.daily_pnl = profit_lock
    can_trade, reason = rm.check_daily_limits()
    assert can_trade is False
    assert rm.profit_lock_hit is True


def test_consecutive_loss_stop_blocks(rm):
    rm.consecutive_losses = config.CONSECUTIVE_LOSS_STOP
    can_trade, reason = rm.check_daily_limits()
    assert can_trade is False
    assert "Consecutive loss" in reason


def test_check_daily_limits_returns_a_pair(rm):
    """Callers unpack two values — lock the contract down."""
    result = rm.check_daily_limits()
    assert isinstance(result, tuple)
    assert len(result) == 2


# ─────────────────────────────────────────
# Early warning
# ─────────────────────────────────────────

def test_not_approaching_limit_at_session_start(rm):
    assert rm.approaching_daily_limit() is False


def test_approaching_limit_triggers_at_eighty_percent(rm):
    loss_limit = STARTING_BALANCE * config.DAILY_LOSS_LIMIT
    rm.daily_pnl = -(loss_limit * 0.8)
    assert rm.approaching_daily_limit() is True


def test_warning_fires_before_the_hard_stop(rm):
    """
    The warning must trigger strictly earlier than the block, otherwise it
    serves no purpose.
    """
    loss_limit = STARTING_BALANCE * config.DAILY_LOSS_LIMIT
    rm.daily_pnl = -(loss_limit * 0.85)
    can_trade, _ = rm.check_daily_limits()
    assert rm.approaching_daily_limit() is True
    assert can_trade is True
