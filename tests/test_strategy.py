"""
tests/test_strategy.py — Unit tests for signal generation

Covers the volume filter (rolling median, not mean), ATR-based stops, EMA
crossover entry, and RSI/VWAP confirmation. Uses recorded candle fixtures
under tests/fixtures/ — no live network calls in the tests themselves.

tests/fixtures/mnq_5min_recorded.csv and mnq_15min_recorded.csv are real MNQ
front-month bars pulled once via tt_marketdata.get_bars_tastytrade() and
frozen to disk (see the provenance note at the bottom of this file). Each
scenario below is a real occurrence found in that window — a real EMA
crossover, a real volume spike, a real quiet stretch — located by scanning
the recorded data rather than engineered by hand. Slices are cut at the
timestamp of the bar being evaluated, on both timeframes, so no test sees
bars from after the moment it's asking about (the same causal constraint
generate_signal operates under live).
"""

import os

import pandas as pd
import pytest

import config
import strategy


FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

FULL_5MIN = pd.read_csv(
    os.path.join(FIXTURE_DIR, "mnq_5min_recorded.csv"), index_col="timestamp", parse_dates=True
)
FULL_15MIN = pd.read_csv(
    os.path.join(FIXTURE_DIR, "mnq_15min_recorded.csv"), index_col="timestamp", parse_dates=True
)

# Positions located by scanning the recorded window (see provenance note):
#   30 -> quiet bar, no EMA cross yet
#   35 -> real cross_up, all confirmation gates pass -> Buy
#   39 -> real cross_down, all confirmation gates pass -> Sell
#   32 -> a real volume spike (bar 14) sitting inside this bar's trailing
#         20-bar window, big enough to separate median from mean
NO_TRIGGER_IDX = 30
LONG_SIGNAL_IDX = 35
SHORT_SIGNAL_IDX = 39
VOLUME_SPIKE_IDX = 32


def _as_of(idx):
    """5min bars up to and including idx, plus the 15min bars available as of that same moment."""
    sub5 = FULL_5MIN.iloc[: idx + 1]
    ts = sub5.index[-1]
    sub15 = FULL_15MIN[FULL_15MIN.index <= ts]
    return sub5.copy(), sub15.copy()


# Scalar inputs for _confirm_long_verbose / _confirm_short_verbose that pass
# every gate. Individual tests override one field at a time to isolate a
# single gate. Prices avoid round numbers (config.ROUND_NUMBER_BUFFER) and
# prev_high/prev_low is None to skip the prior-level gate entirely. These are
# plain scalars, not candle data — the gate functions take floats, not a
# DataFrame, so there's nothing to "record" here.
GOOD_LONG = dict(rsi=55.0, price=21050.0, vwap=21040.0, volume=1000.0, vol_avg=1000.0, prev_high=None)
GOOD_SHORT = dict(rsi=45.0, price=21040.0, vwap=21050.0, volume=1000.0, vol_avg=1000.0, prev_low=None)


# ─────────────────────────────────────────
# Volume filter — rolling median, configured multiplier
# ─────────────────────────────────────────

def test_vol_avg_is_rolling_median_not_mean():
    """
    A real volume spike (bar 14 in the recorded window) sits inside the
    trailing 20-bar window ending at bar 32. A mean would be dragged well
    above what a typical bar looks like; a median ignores it. This pins the
    CLAUDE.md rule directly against real indicator output on real data.
    """
    enriched = strategy.calculate_indicators(FULL_5MIN.copy())
    row = enriched.iloc[VOLUME_SPIKE_IDX]

    trailing_20 = FULL_5MIN["volume"].iloc[VOLUME_SPIKE_IDX - 19 : VOLUME_SPIKE_IDX + 1]
    assert row["vol_avg"] == pytest.approx(trailing_20.median())
    assert trailing_20.mean() > trailing_20.median() * 2  # the spike is a real outlier, not noise
    assert row["vol_avg"] != pytest.approx(trailing_20.mean())


def test_confirm_long_volume_gate_uses_configured_multiplier():
    """Volume exactly at vol_avg * VOLUME_MULTIPLIER passes; just under fails."""
    threshold = GOOD_LONG["vol_avg"] * config.VOLUME_MULTIPLIER

    at_threshold = {**GOOD_LONG, "volume": threshold}
    ok, _ = strategy._confirm_long_verbose(**at_threshold)
    assert ok is True

    below_threshold = {**GOOD_LONG, "volume": threshold - 1}
    ok, reason = strategy._confirm_long_verbose(**below_threshold)
    assert ok is False
    assert "volume" in reason


def test_confirm_short_volume_gate_uses_configured_multiplier():
    threshold = GOOD_SHORT["vol_avg"] * config.VOLUME_MULTIPLIER

    at_threshold = {**GOOD_SHORT, "volume": threshold}
    ok, _ = strategy._confirm_short_verbose(**at_threshold)
    assert ok is True

    below_threshold = {**GOOD_SHORT, "volume": threshold - 1}
    ok, reason = strategy._confirm_short_verbose(**below_threshold)
    assert ok is False
    assert "volume" in reason


def test_volume_multiplier_cannot_silently_diverge_from_config(monkeypatch):
    """
    Regression guard: the volume gate must read config.VOLUME_MULTIPLIER at
    call time, not a value baked in separately. If someone hardcodes a
    multiplier directly into strategy.py instead of referencing config, this
    test fails — the constant and the filter can't quietly drift apart.
    """
    monkeypatch.setattr(config, "VOLUME_MULTIPLIER", 2.0)

    # Passes under the real default (0.7x) but must now fail under 2.0x.
    volume = GOOD_LONG["vol_avg"] * 0.7
    ok, reason = strategy._confirm_long_verbose(**{**GOOD_LONG, "volume": volume})
    assert ok is False
    assert "volume" in reason

    # And a volume that clears the new 2.0x bar must pass.
    volume = GOOD_LONG["vol_avg"] * 2.0
    ok, _ = strategy._confirm_long_verbose(**{**GOOD_LONG, "volume": volume})
    assert ok is True


# ─────────────────────────────────────────
# ATR stop logic
# ─────────────────────────────────────────

def test_long_stop_and_targets_match_atr_formula():
    """
    Verifies generate_signal actually wires ATR into the stop/target
    formula on a real confirmed long signal — recomputes the expected
    values independently from the same recorded bar's real atr/price
    rather than restating hardcoded numbers.
    """
    sub5, sub15 = _as_of(LONG_SIGNAL_IDX)
    enriched = strategy.calculate_indicators(sub5.copy())
    price, atr = enriched.iloc[-1]["close"], enriched.iloc[-1]["atr"]

    sig = strategy.generate_signal(sub5.copy(), sub15.copy(), "MNQ", None, None, verbose=False)
    assert sig is not None and sig["direction"] == "Buy"

    expected_stop = price - (atr * config.ATR_STOP_MULTIPLIER) - config.SLIPPAGE_BUFFER
    expected_risk = price - expected_stop
    expected_t1 = price + expected_risk * config.PROFIT_TARGET_1
    expected_t2 = price + expected_risk * config.PROFIT_TARGET_2

    assert sig["stop"] == pytest.approx(round(expected_stop, 2), abs=0.01)
    assert sig["risk"] == pytest.approx(round(expected_risk, 2), abs=0.01)
    assert sig["target1"] == pytest.approx(round(expected_t1, 2), abs=0.01)
    assert sig["target2"] == pytest.approx(round(expected_t2, 2), abs=0.01)


def test_short_stop_and_targets_match_atr_formula():
    sub5, sub15 = _as_of(SHORT_SIGNAL_IDX)
    enriched = strategy.calculate_indicators(sub5.copy())
    price, atr = enriched.iloc[-1]["close"], enriched.iloc[-1]["atr"]

    sig = strategy.generate_signal(sub5.copy(), sub15.copy(), "MNQ", None, None, verbose=False)
    assert sig is not None and sig["direction"] == "Sell"

    expected_stop = price + (atr * config.ATR_STOP_MULTIPLIER) + config.SLIPPAGE_BUFFER
    expected_risk = expected_stop - price
    expected_t1 = price - expected_risk * config.PROFIT_TARGET_1
    expected_t2 = price - expected_risk * config.PROFIT_TARGET_2

    assert sig["stop"] == pytest.approx(round(expected_stop, 2), abs=0.01)
    assert sig["risk"] == pytest.approx(round(expected_risk, 2), abs=0.01)
    assert sig["target1"] == pytest.approx(round(expected_t1, 2), abs=0.01)
    assert sig["target2"] == pytest.approx(round(expected_t2, 2), abs=0.01)


def test_rr_ratio_below_minimum_blocks_signal(monkeypatch):
    """A real, otherwise-confirmed crossover is still rejected if R:R can't clear MIN_RR_RATIO."""
    sub5, sub15 = _as_of(LONG_SIGNAL_IDX)
    monkeypatch.setattr(config, "MIN_RR_RATIO", 100.0)
    sig = strategy.generate_signal(sub5.copy(), sub15.copy(), "MNQ", None, None, verbose=False)
    assert sig is None


# ─────────────────────────────────────────
# EMA cross entry
# ─────────────────────────────────────────

def test_cross_up_flag_set_when_fast_ema_crosses_above_slow():
    enriched = strategy.calculate_indicators(FULL_5MIN.copy())
    row = enriched.iloc[LONG_SIGNAL_IDX]
    assert bool(row["cross_up"]) is True
    assert bool(row["cross_down"]) is False
    assert row["ema_fast"] > row["ema_slow"]


def test_cross_down_flag_set_when_fast_ema_crosses_below_slow():
    enriched = strategy.calculate_indicators(FULL_5MIN.copy())
    row = enriched.iloc[SHORT_SIGNAL_IDX]
    assert bool(row["cross_down"]) is True
    assert bool(row["cross_up"]) is False
    assert row["ema_fast"] < row["ema_slow"]


def test_5min_cross_alone_triggers_long_signal():
    """
    long_trigger = cross_up_5 or (trend_up_5 and cross_up_15) — the 15min
    slice here has no crossover of its own at this moment, so a Buy signal
    proves the 5-min-alone path fires independently.
    """
    sub5, sub15 = _as_of(LONG_SIGNAL_IDX)
    sig = strategy.generate_signal(sub5.copy(), sub15.copy(), "MNQ", None, None, verbose=False)
    assert sig is not None
    assert sig["direction"] == "Buy"
    assert sig["reason"] == "EMA crossover long confirmed"


def test_5min_cross_alone_triggers_short_signal():
    sub5, sub15 = _as_of(SHORT_SIGNAL_IDX)
    sig = strategy.generate_signal(sub5.copy(), sub15.copy(), "MNQ", None, None, verbose=False)
    assert sig is not None
    assert sig["direction"] == "Sell"
    assert sig["reason"] == "EMA crossover short confirmed"


def test_no_crossover_produces_no_signal():
    sub5, sub15 = _as_of(NO_TRIGGER_IDX)
    enriched = strategy.calculate_indicators(sub5.copy())
    last = enriched.iloc[-1]
    assert bool(last["cross_up"]) is False
    assert bool(last["cross_down"]) is False

    sig = strategy.generate_signal(sub5.copy(), sub15.copy(), "MNQ", None, None, verbose=False)
    assert sig is None


# ─────────────────────────────────────────
# RSI / VWAP confirmation
# ─────────────────────────────────────────

def test_confirm_long_accepts_rsi_at_range_boundaries():
    lo = {**GOOD_LONG, "rsi": config.RSI_LONG_MIN}
    hi = {**GOOD_LONG, "rsi": config.RSI_LONG_MAX}
    assert strategy._confirm_long_verbose(**lo)[0] is True
    assert strategy._confirm_long_verbose(**hi)[0] is True


def test_confirm_long_rejects_rsi_outside_range():
    below = {**GOOD_LONG, "rsi": config.RSI_LONG_MIN - 0.1}
    above = {**GOOD_LONG, "rsi": config.RSI_LONG_MAX + 0.1}
    ok, reason = strategy._confirm_long_verbose(**below)
    assert ok is False and "RSI" in reason
    ok, reason = strategy._confirm_long_verbose(**above)
    assert ok is False and "RSI" in reason


def test_confirm_short_accepts_rsi_at_range_boundaries():
    lo = {**GOOD_SHORT, "rsi": config.RSI_SHORT_MIN}
    hi = {**GOOD_SHORT, "rsi": config.RSI_SHORT_MAX}
    assert strategy._confirm_short_verbose(**lo)[0] is True
    assert strategy._confirm_short_verbose(**hi)[0] is True


def test_confirm_short_rejects_rsi_outside_range():
    below = {**GOOD_SHORT, "rsi": config.RSI_SHORT_MIN - 0.1}
    above = {**GOOD_SHORT, "rsi": config.RSI_SHORT_MAX + 0.1}
    ok, reason = strategy._confirm_short_verbose(**below)
    assert ok is False and "RSI" in reason
    ok, reason = strategy._confirm_short_verbose(**above)
    assert ok is False and "RSI" in reason


def test_confirm_long_requires_price_above_vwap():
    below_vwap = {**GOOD_LONG, "price": GOOD_LONG["vwap"] - 1}
    ok, reason = strategy._confirm_long_verbose(**below_vwap)
    assert ok is False
    assert "VWAP" in reason


def test_confirm_short_requires_price_below_vwap():
    above_vwap = {**GOOD_SHORT, "price": GOOD_SHORT["vwap"] + 1}
    ok, reason = strategy._confirm_short_verbose(**above_vwap)
    assert ok is False
    assert "VWAP" in reason


def test_confirm_long_passes_when_all_gates_clear():
    assert strategy._confirm_long_verbose(**GOOD_LONG) == (True, "all gates passed")


def test_confirm_short_passes_when_all_gates_clear():
    assert strategy._confirm_short_verbose(**GOOD_SHORT) == (True, "all gates passed")


def test_real_long_signal_confirmation_reports_rsi_in_range():
    """
    End-to-end sanity check on real data: the actual RSI computed for the
    recorded long-signal bar must itself fall inside the configured range,
    tying the scalar-level RSI gate tests above back to a real occurrence.
    """
    sub5, sub15 = _as_of(LONG_SIGNAL_IDX)
    sig = strategy.generate_signal(sub5.copy(), sub15.copy(), "MNQ", None, None, verbose=False)
    assert config.RSI_LONG_MIN <= sig["rsi"] <= config.RSI_LONG_MAX


def test_real_short_signal_confirmation_reports_rsi_in_range():
    sub5, sub15 = _as_of(SHORT_SIGNAL_IDX)
    sig = strategy.generate_signal(sub5.copy(), sub15.copy(), "MNQ", None, None, verbose=False)
    assert config.RSI_SHORT_MIN <= sig["rsi"] <= config.RSI_SHORT_MAX


# ─────────────────────────────────────────
# Fixture provenance
# ─────────────────────────────────────────
#
# tests/fixtures/mnq_5min_recorded.csv and mnq_15min_recorded.csv are real
# MNQ front-month bars, pulled once via:
#
#   import tt_marketdata as tt
#   df5  = tt.get_bars_tastytrade("MNQ", "5min", 500)
#   df15 = tt.get_bars_tastytrade("MNQ", "15min", 500)
#
# tt_marketdata caps lookback at 6h/18h respectively, so this is a single
# real session's worth of bars (57 5-min bars, 67 15-min bars), not 500 —
# the count argument is a ceiling the lookback window doesn't reach on these
# timeframes. The specific bar indices referenced above (NO_TRIGGER_IDX,
# LONG_SIGNAL_IDX, SHORT_SIGNAL_IDX, VOLUME_SPIKE_IDX) were located by
# running calculate_indicators()/generate_signal() over the recorded window
# and scanning for real occurrences of each condition — nothing in this
# file was hand-tuned into the price data. Re-pulling fresh bars will very
# likely move these indices; if these tests start failing after a re-pull,
# re-run that scan rather than assuming the indicator logic broke.
