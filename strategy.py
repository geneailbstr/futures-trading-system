"""
strategy.py — Signal generation and market analysis
EMA crossover, RSI, VWAP, Volume, Regime detection
"""

import pandas as pd
import numpy as np
import ta
from datetime import datetime, date
import pytz
import config

ET = pytz.timezone("America/New_York")


# ─────────────────────────────────────────
# MARKET REGIME DETECTION
# ─────────────────────────────────────────
def detect_market_regime(df):
    """
    Detect if market is trending, ranging, or volatile
    Returns: 'TRENDING', 'RANGING', or 'VOLATILE'
    """
    try:
        if df is None or len(df) < 30:
            return "RANGING"

        # ADX — measures trend strength
        adx = ta.trend.ADXIndicator(
            df["high"], df["low"], df["close"],
            window=config.ADX_PERIOD
        )
        adx_val = adx.adx().iloc[-1]

        # ATR — measures volatility
        atr = ta.volatility.AverageTrueRange(
            df["high"], df["low"], df["close"],
            window=config.ATR_PERIOD
        )
        atr_val  = atr.average_true_range().iloc[-1]
        atr_pct  = atr_val / df["close"].iloc[-1] * 100

        # Regime logic
        if atr_pct > 2.0:
            return "VOLATILE"
        elif adx_val >= config.ADX_TREND_THRESHOLD:
            return "TRENDING"
        else:
            return "RANGING"

    except Exception as e:
        print(f"⚠️ Regime detection error: {e}")
        return "RANGING"


# ─────────────────────────────────────────
# TECHNICAL INDICATORS
# ─────────────────────────────────────────
def calculate_indicators(df):
    """
    Add all technical indicators to dataframe
    Returns enriched dataframe
    """
    try:
        if df is None or len(df) < 30:
            return None

        # Zero-volume bars are a known artifact of resampling sparse/missing
        # 1-min proxy data (yfinance) into 5min/15min bars — confirmed in
        # diagnostic logs on 2026-06-24 ("volume 0" appearing during market
        # hours, which should be impossible). Forward-fill them from the
        # nearest real bar so they don't poison the rolling average or
        # falsely fail the volume-spike check on an otherwise valid signal.
        zero_vol_count = (df["volume"] == 0).sum()
        if zero_vol_count > 0:
            df["volume"] = df["volume"].replace(0, pd.NA).ffill().bfill()

        # EMA
        df["ema_fast"] = ta.trend.EMAIndicator(
            df["close"], window=config.EMA_FAST
        ).ema_indicator()

        df["ema_slow"] = ta.trend.EMAIndicator(
            df["close"], window=config.EMA_SLOW
        ).ema_indicator()

        # RSI
        df["rsi"] = ta.momentum.RSIIndicator(
            df["close"], window=config.RSI_PERIOD
        ).rsi()

        # VWAP (volume weighted average price)
        df["vwap"] = ta.volume.VolumeWeightedAveragePrice(
            df["high"], df["low"], df["close"], df["volume"]
        ).volume_weighted_average_price()

        # Volume average — MEDIAN, not mean. Diagnostic logs on 2026-06-25/26
        # showed legitimate bars (60K-540K volume) consistently rejected
        # against an average demanding 900K+. A rolling MEAN gets dragged
        # way up by occasional outlier spike bars (open/close auctions, or
        # an occasional bad aggregate bar from the proxy data) sitting
        # inside the same 20-bar window. Median is far more robust to that
        # — it reflects "what a typical bar actually looks like" instead
        # of getting skewed by a handful of extreme ones.
        df["vol_avg"] = df["volume"].rolling(20).median()

        # ATR for stop placement
        atr_indicator = ta.volatility.AverageTrueRange(
            df["high"], df["low"], df["close"],
            window=config.ATR_PERIOD
        )
        df["atr"] = atr_indicator.average_true_range()

        # EMA crossover signal
        df["cross_up"]   = (
            (df["ema_fast"] > df["ema_slow"]) &
            (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
        )
        df["cross_down"] = (
            (df["ema_fast"] < df["ema_slow"]) &
            (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
        )

        return df

    except Exception as e:
        print(f"❌ Indicator calculation error: {e}")
        return None


# ─────────────────────────────────────────
# SIGNAL GENERATION
# ─────────────────────────────────────────
def generate_signal(df_5min, df_15min, symbol, prev_high, prev_low, verbose=True):
    """
    Generate trade signal for a symbol
    Both timeframes must agree for entry
    Returns: dict with signal details or None

    When verbose=True, prints exactly which gate the symbol failed at,
    so "no trades today" can be diagnosed instead of guessed at.
    """
    try:
        # Calculate indicators on both timeframes
        df5  = calculate_indicators(df_5min.copy())
        df15 = calculate_indicators(df_15min.copy())

        if df5 is None or df15 is None:
            if verbose:
                print(f"   [{symbol}] REJECT: indicator calc failed (insufficient bars)")
            return None

        # Latest values
        c5  = df5.iloc[-1]
        c15 = df15.iloc[-1]

        price      = c5["close"]
        atr        = c5["atr"]
        rsi        = c5["rsi"]
        vwap       = c5["vwap"]
        volume     = c5["volume"]
        vol_avg    = c5["vol_avg"]

        cross_up_5   = bool(c5["cross_up"])
        cross_down_5 = bool(c5["cross_down"])
        trend_up_5   = c5["ema_fast"] > c5["ema_slow"]
        trend_down_5 = c5["ema_fast"] < c5["ema_slow"]
        cross_up_15  = bool(c15["cross_up"])
        cross_down_15 = bool(c15["cross_down"])

        long_trigger  = cross_up_5 or (trend_up_5 and cross_up_15)
        short_trigger = cross_down_5 or (trend_down_5 and cross_down_15)

        # ── LONG SIGNAL ──
        if long_trigger:
            ok, reason = _confirm_long_verbose(rsi, price, vwap, volume, vol_avg, prev_high)
            if ok:
                stop  = price - (atr * config.ATR_STOP_MULTIPLIER) - config.SLIPPAGE_BUFFER
                risk  = price - stop
                t1    = price + (risk * config.PROFIT_TARGET_1)
                t2    = price + (risk * config.PROFIT_TARGET_2)

                if risk <= 0 or (t2 - price) / risk < config.MIN_RR_RATIO:
                    if verbose:
                        print(f"   [{symbol}] REJECT long: R:R {((t2-price)/risk if risk>0 else 0):.2f} "
                              f"below minimum {config.MIN_RR_RATIO}")
                    return None

                if verbose:
                    print(f"   [{symbol}] ✅ LONG signal — all gates passed")
                return {
                    "symbol":    symbol,
                    "direction": "Buy",
                    "entry":     price,
                    "stop":      round(stop, 2),
                    "target1":   round(t1, 2),
                    "target2":   round(t2, 2),
                    "risk":      round(risk, 2),
                    "atr":       round(atr, 2),
                    "rsi":       round(rsi, 2),
                    "reason":    "EMA crossover long confirmed"
                }
            elif verbose:
                print(f"   [{symbol}] long trigger fired but REJECTED: {reason}")

        # ── SHORT SIGNAL ──
        if short_trigger:
            ok, reason = _confirm_short_verbose(rsi, price, vwap, volume, vol_avg, prev_low)
            if ok:
                stop  = price + (atr * config.ATR_STOP_MULTIPLIER) + config.SLIPPAGE_BUFFER
                risk  = stop - price
                t1    = price - (risk * config.PROFIT_TARGET_1)
                t2    = price - (risk * config.PROFIT_TARGET_2)

                if risk <= 0 or (price - t2) / risk < config.MIN_RR_RATIO:
                    if verbose:
                        print(f"   [{symbol}] REJECT short: R:R {((price-t2)/risk if risk>0 else 0):.2f} "
                              f"below minimum {config.MIN_RR_RATIO}")
                    return None

                if verbose:
                    print(f"   [{symbol}] ✅ SHORT signal — all gates passed")
                return {
                    "symbol":    symbol,
                    "direction": "Sell",
                    "entry":     price,
                    "stop":      round(stop, 2),
                    "target1":   round(t1, 2),
                    "target2":   round(t2, 2),
                    "risk":      round(risk, 2),
                    "atr":       round(atr, 2),
                    "rsi":       round(rsi, 2),
                    "reason":    "EMA crossover short confirmed"
                }
            elif verbose:
                print(f"   [{symbol}] short trigger fired but REJECTED: {reason}")

        if verbose and not long_trigger and not short_trigger:
            print(f"   [{symbol}] no trigger — EMA flat/no crossover "
                  f"(fast {c5['ema_fast']:.2f} vs slow {c5['ema_slow']:.2f}, "
                  f"RSI {rsi:.1f}, price {price:.2f} vs VWAP {vwap:.2f})")

        return None

    except Exception as e:
        print(f"❌ Signal generation error for {symbol}: {e}")
        return None


def _confirm_long(rsi, price, vwap, volume, vol_avg, prev_high):
    """All long filters must pass"""
    checks = [
        config.RSI_LONG_MIN <= rsi <= config.RSI_LONG_MAX,  # RSI in range
        price > vwap,                                         # above VWAP
        volume >= vol_avg * config.VOLUME_MULTIPLIER,         # volume spike
        not _near_round_number(price),                        # not at round number
        not _near_level(price, prev_high),                    # not at prev high resistance
    ]
    return all(checks)


def _confirm_long_verbose(rsi, price, vwap, volume, vol_avg, prev_high):
    """Same as _confirm_long but returns (passed, reason_if_failed) for diagnostics"""
    if not (config.RSI_LONG_MIN <= rsi <= config.RSI_LONG_MAX):
        return False, f"RSI {rsi:.1f} outside long range [{config.RSI_LONG_MIN}-{config.RSI_LONG_MAX}]"
    if not (price > vwap):
        return False, f"price {price:.2f} not above VWAP {vwap:.2f}"
    if not (volume >= vol_avg * config.VOLUME_MULTIPLIER):
        return False, f"volume {volume:.0f} below {config.VOLUME_MULTIPLIER}x avg ({vol_avg*config.VOLUME_MULTIPLIER:.0f} needed)"
    if _near_round_number(price):
        return False, f"price {price:.2f} too close to a round number"
    if _near_level(price, prev_high):
        return False, f"price {price:.2f} too close to prev day high {prev_high}"
    return True, "all gates passed"


def _confirm_short(rsi, price, vwap, volume, vol_avg, prev_low):
    """All short filters must pass"""
    checks = [
        config.RSI_SHORT_MIN <= rsi <= config.RSI_SHORT_MAX,  # RSI in range
        price < vwap,                                          # below VWAP
        volume >= vol_avg * config.VOLUME_MULTIPLIER,          # volume spike
        not _near_round_number(price),                         # not at round number
        not _near_level(price, prev_low),                      # not at prev low support
    ]
    return all(checks)


def _confirm_short_verbose(rsi, price, vwap, volume, vol_avg, prev_low):
    """Same as _confirm_short but returns (passed, reason_if_failed) for diagnostics"""
    if not (config.RSI_SHORT_MIN <= rsi <= config.RSI_SHORT_MAX):
        return False, f"RSI {rsi:.1f} outside short range [{config.RSI_SHORT_MIN}-{config.RSI_SHORT_MAX}]"
    if not (price < vwap):
        return False, f"price {price:.2f} not below VWAP {vwap:.2f}"
    if not (volume >= vol_avg * config.VOLUME_MULTIPLIER):
        return False, f"volume {volume:.0f} below {config.VOLUME_MULTIPLIER}x avg ({vol_avg*config.VOLUME_MULTIPLIER:.0f} needed)"
    if _near_round_number(price):
        return False, f"price {price:.2f} too close to a round number"
    if _near_level(price, prev_low):
        return False, f"price {price:.2f} too close to prev day low {prev_low}"
    return True, "all gates passed"


def _near_round_number(price, buffer=None):
    """Check if price is near a round number"""
    buf = buffer or config.ROUND_NUMBER_BUFFER
    remainder = price % 100
    return remainder < buf or remainder > (100 - buf)


def _near_level(price, level, buffer=None):
    """Check if price is near a key level"""
    if level is None:
        return False
    buf = buffer or config.PREV_DAY_BUFFER
    return abs(price - level) < buf


# ─────────────────────────────────────────
# CORRELATION CHECK
# ─────────────────────────────────────────
def signals_agree(signals):
    """
    Check if MNQ and MES signals agree
    Returns True only if both signal in same direction
    """
    if not config.USE_CORRELATION:
        return True

    directions = [s["direction"] for s in signals.values() if s is not None]
    if len(directions) < 2:
        return len(directions) == 1

    return len(set(directions)) == 1  # all same direction


# ─────────────────────────────────────────
# OPENING RANGE
# ─────────────────────────────────────────
def get_opening_range(df_5min):
    """
    Calculate the opening range (first 15 minutes)
    Returns (or_high, or_low)
    """
    try:
        now_et     = datetime.now(ET)
        open_time  = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        range_end  = open_time + pd.Timedelta(minutes=config.OPENING_RANGE_MINUTES)

        mask = (
            (df_5min.index >= open_time) &
            (df_5min.index < range_end)
        )
        or_bars = df_5min[mask]

        if len(or_bars) == 0:
            return None, None

        return or_bars["high"].max(), or_bars["low"].min()

    except Exception as e:
        print(f"⚠️ Opening range error: {e}")
        return None, None


# ─────────────────────────────────────────
# PREVIOUS DAY LEVELS
# ─────────────────────────────────────────
def get_prev_day_levels(df_daily):
    """Get previous day high and low"""
    try:
        if df_daily is None or len(df_daily) < 2:
            return None, None
        prev         = df_daily.iloc[-2]
        return prev["high"], prev["low"]
    except Exception:
        return None, None


# ─────────────────────────────────────────
# GAP DETECTION
# ─────────────────────────────────────────
def detect_gap(df_daily):
    """
    Check if market gapped significantly at open
    Returns True if gap > threshold (wait for fill)
    """
    try:
        if df_daily is None or len(df_daily) < 2:
            return False

        prev_close  = df_daily.iloc[-2]["close"]
        today_open  = df_daily.iloc[-1]["open"]
        gap_pct     = abs(today_open - prev_close) / prev_close

        if gap_pct > config.GAP_THRESHOLD:
            print(f"⚠️ Gap detected: {gap_pct*100:.2f}% — waiting for fill")
            return True
        return False

    except Exception:
        return False


# ─────────────────────────────────────────
# SESSION CHECK
# ─────────────────────────────────────────
def in_trading_window():
    """Check if current time is in a valid trading window"""
    now = datetime.now(ET).strftime("%H:%M")

    morning   = config.MORNING_START   <= now <= config.MORNING_END
    afternoon = config.AFTERNOON_START <= now <= config.AFTERNOON_END

    return morning or afternoon


def past_force_close():
    """Check if it's past the force close time"""
    now = datetime.now(ET).strftime("%H:%M")
    return now >= config.FORCE_CLOSE_TIME


def is_monday():
    return datetime.now(ET).weekday() == 0


def is_friday():
    return datetime.now(ET).weekday() == 4


def past_friday_cutoff():
    now = datetime.now(ET).strftime("%H:%M")
    return is_friday() and now >= config.FRIDAY_CUTOFF


def is_opex_friday():
    """Check if today is options expiration Friday (3rd Friday of month)"""
    today     = date.today()
    if today.weekday() != 4:
        return False
    # Third Friday = day between 15th and 21st
    return 15 <= today.day <= 21
