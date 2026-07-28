"""
marketdata.py — Multi-source market data with automatic failover
Primary source is Databento (real CME futures data for MNQ/MES).
Falls back to ETF proxies (QQQ/SPY) via yfinance/Finnhub/Alpha Vantage
if Databento is unavailable or credits run out.

Failover order: Databento -> yfinance -> Finnhub -> Alpha Vantage
"""

import pandas as pd
import requests
import time
from datetime import datetime, timedelta, timezone
import config

# Map futures symbols to free-data ETF proxies (fallback only)
PROXY_MAP = {
    "MNQ": "QQQ",
    "MES": "SPY",
}

# Databento continuous contract symbols
DATABENTO_SYMBOLS = {
    "MNQ": "MNQ.c.0",
    "MES": "MES.c.0",
}

FINNHUB_KEY       = getattr(config, "FINNHUB_API_KEY", None)
ALPHA_VANTAGE_KEY = getattr(config, "ALPHA_VANTAGE_API_KEY", None)
DATABENTO_KEY     = getattr(config, "DATABENTO_API_KEY", None)

_databento_client = None
_databento_disabled = False  # flips True if credits run out mid-session (historical path)

_cache = {}
CACHE_SECONDS = 30

# ─────────────────────────────────────────
# DATABENTO LIVE STREAMING
#
# db.Historical only serves data older than 24hrs — it cannot be used
# for live signals, which is exactly what caused the bot to see the
# same "stale" bar over and over all day. db.Live opens a persistent
# streaming connection and bars accumulate locally in memory as they
# actually happen. One client per symbol keeps identification simple.
# ─────────────────────────────────────────
import threading
import collections

_live_clients  = {}   # symbol -> db.Live client
_live_threads  = {}   # symbol -> background thread
_live_bars     = {sym: collections.deque(maxlen=2000) for sym in ["MNQ", "MES"]}
_live_lock     = threading.Lock()
_live_started  = {"MNQ": False, "MES": False}
_live_failed   = {"MNQ": False, "MES": False}


def _live_stream_worker(symbol, db_symbol):
    """Runs in a background thread, continuously appending live 1-min bars"""
    import databento as db
    try:
        client = db.Live(key=DATABENTO_KEY)
        client.subscribe(
            dataset  = "GLBX.MDP3",
            schema   = "ohlcv-1m",
            stype_in = "continuous",
            symbols  = [db_symbol],
        )
        _live_clients[symbol] = client

        first_bar_checked = False
        for record in client:
            try:
                price = float(record.close)

                # Sanity check the FIRST bar only — if scaling were wrong,
                # MNQ/MES would show as either ~30 (off by 1e9) or
                # ~30,000,000,000 (off by 1e-9) instead of a realistic
                # ~5,000-40,000 range. Catch it loud instead of silent.
                if not first_bar_checked:
                    if price < 100 or price > 200000:
                        print(f"   ⚠️ SUSPICIOUS PRICE for {symbol}: {price} — "
                              f"likely a scaling bug, disabling live feed for this symbol")
                        _live_failed[symbol] = True
                        return
                    print(f"   ✅ Live feed verified for {symbol}: first price {price}")
                    first_bar_checked = True

                with _live_lock:
                    _live_bars[symbol].append({
                        "timestamp": pd.Timestamp(record.ts_event, unit="ns", tz="UTC"),
                        "open":   float(record.open),
                        "high":   float(record.high),
                        "low":    float(record.low),
                        "close":  price,
                        "volume": record.volume,
                    })
            except Exception:
                continue  # skip malformed records, don't kill the stream

    except Exception as e:
        print(f"   Warning: Live feed for {symbol} failed: {e}")
        _live_failed[symbol] = True


def start_live_feed():
    """
    Opens persistent streaming connections for MNQ and MES.
    Call this ONCE at bot startup (pre-market) — not on every scan.
    """
    if not DATABENTO_KEY:
        return False

    any_started = False
    for symbol in ["MNQ", "MES"]:
        if _live_started[symbol]:
            continue
        try:
            db_symbol = DATABENTO_SYMBOLS.get(symbol, f"{symbol}.c.0")
            t = threading.Thread(
                target=_live_stream_worker, args=(symbol, db_symbol), daemon=True
            )
            t.start()
            _live_threads[symbol] = t
            _live_started[symbol] = True
            any_started = True
        except Exception as e:
            print(f"   Warning: could not start live feed for {symbol}: {e}")
            _live_failed[symbol] = True

    if any_started:
        print("   📡 Databento Live feed starting (MNQ + MES streaming)...")
    return any_started


def live_feed_ready(symbol, min_bars=5):
    """Check if enough live bars have accumulated to be usable"""
    with _live_lock:
        return len(_live_bars.get(symbol, [])) >= min_bars


def _get_bars_live(symbol, timeframe, count):
    """Read accumulated bars from the live in-memory buffer"""
    if not _live_started.get(symbol) or _live_failed.get(symbol):
        return None

    with _live_lock:
        bars = list(_live_bars.get(symbol, []))

    if len(bars) < 5:  # stream just started, not enough data yet
        return None

    df = pd.DataFrame(bars).set_index("timestamp").sort_index()

    resample_minutes = {"5min": 5, "15min": 15, "1hour": 60}.get(timeframe)
    if resample_minutes:
        df = df.resample(f"{resample_minutes}min").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()

    return df.tail(count)


def _get_databento_client():
    global _databento_client
    if _databento_client is None and DATABENTO_KEY:
        try:
            import databento as db
            _databento_client = db.Historical(DATABENTO_KEY)
        except Exception as e:
            print(f"   Warning: Databento client init failed: {e}")
    return _databento_client


def _cache_key(symbol, timeframe):
    return f"{symbol}_{timeframe}"


def get_bars(symbol, timeframe="5min", count=100):
    """
    Get OHLCV bars with automatic failover across sources.

    IMPORTANT: Databento's free tier only includes the Historical API,
    which serves data older than 24 hours — it cannot provide fresh
    intraday bars (confirmed: it returned the same frozen bar all day).
    Genuine live data requires a paid Databento Live subscription
    (~$179/mo) OR a broker's included feed (e.g. MFFU+Tradovate
    includes free real-time DXFeed data, but needs its own API auth
    fix before we can pull it programmatically).

    Until one of those is wired up, proxy data (QQQ/SPY via yfinance)
    is the PRIMARY source for intraday timeframes — it's free, has no
    staleness trap, and updates on every single poll. Databento
    Historical is still used for daily-bar lookups (prev high/low),
    where 24hr-old data is perfectly fine.
    """
    key = _cache_key(symbol, timeframe)
    now = time.time()

    if key in _cache:
        cached_time, cached_df = _cache[key]
        if now - cached_time < CACHE_SECONDS:
            return cached_df

    # Minimum acceptable rows scales with what was actually requested —
    # a daily-levels call for count=5 should accept 5 rows, not demand 20
    min_rows = min(count, 20)

    # ── REAL FUTURES DATA (tastytrade/DXLink) ──
    # Used in live mode, or in observe mode (USE_REAL_DATA=True).
    # Only for intraday futures timeframes; daily lookups still use the
    # historical sources below.
    try:
        import config as _cfg
        _use_real = (not _cfg.SIMULATION_MODE) or getattr(_cfg, 'USE_REAL_DATA', False)
    except Exception:
        _use_real = False
    if _use_real and symbol in ('MNQ', 'MES') and timeframe in ('5min', '15min', '1hour', '1day'):
        try:
            import tt_marketdata
            _df = tt_marketdata.get_bars_tastytrade(symbol, timeframe, count)
            if _df is not None and len(_df) >= min_rows:
                _cache[key] = (now, _df)
                return _df
            print(f'   ⚠️ tastytrade returned insufficient bars for {symbol} '
                  f'({0 if _df is None else len(_df)}/{min_rows})')
        except Exception as _e:
            print(f'   ⚠️ tastytrade feed error for {symbol}: {_e}')
        # In LIVE mode we must NOT fall back to proxy prices.
        try:
            import config as _cfg2
            if not _cfg2.SIMULATION_MODE:
                raise RuntimeError(
                    f'LIVE MODE: real futures data unavailable for {symbol} — '
                    f'refusing to fall back to proxy prices')
        except RuntimeError:
            raise
        except Exception:
            pass
        # Observe mode (sim + USE_REAL_DATA): fall through to proxy below
        # so the bot keeps running even if the feed hiccups.

    proxy = PROXY_MAP.get(symbol, symbol)

    if timeframe in ("5min", "15min", "1hour"):
        # Intraday: proxy data first — actually updates every poll
        df = _get_bars_yfinance(proxy, timeframe, count)
        if df is not None and len(df) >= min_rows:
            _cache[key] = (now, df)
            return df

        if FINNHUB_KEY:
            df = _get_bars_finnhub(proxy, timeframe, count)
            if df is not None and len(df) >= min_rows:
                print(f"   (using Finnhub proxy fallback for {symbol})")
                _cache[key] = (now, df)
                return df

        if ALPHA_VANTAGE_KEY:
            df = _get_bars_alphavantage(proxy, timeframe, count)
            if df is not None and len(df) >= min_rows:
                print(f"   (using Alpha Vantage proxy fallback for {symbol})")
                _cache[key] = (now, df)
                return df

        # Last resort only — Databento Historical may be stale intraday
        if DATABENTO_KEY and not _databento_disabled:
            df = _get_bars_databento(symbol, timeframe, count)
            if df is not None and len(df) >= min_rows:
                print(f"   ⚠️ Using Databento Historical for intraday {symbol} — "
                      f"may be stale (free tier has no live access)")
                _cache[key] = (now, df)
                return df

    else:
        # Daily/longer timeframes: Databento Historical is fine here —
        # staleness doesn't matter for yesterday's high/low
        if DATABENTO_KEY and not _databento_disabled:
            df = _get_bars_databento(symbol, timeframe, count)
            if df is not None and len(df) >= min_rows:
                _cache[key] = (now, df)
                return df

        df = _get_bars_yfinance(proxy, timeframe, count)
        if df is not None and len(df) >= min_rows:
            import config as _cfg  # guard: no proxy prices in live mode
            if not _cfg.SIMULATION_MODE:
                raise RuntimeError(
                    f"LIVE MODE: no real futures data for {symbol} — "
                    f"refusing to trade on proxy prices")
            print(f"   (using yfinance proxy fallback for {symbol})")
            _cache[key] = (now, df)
            return df

        if FINNHUB_KEY:
            df = _get_bars_finnhub(proxy, timeframe, count)
            if df is not None and len(df) >= min_rows:
                print(f"   (using Finnhub proxy fallback for {symbol})")
                _cache[key] = (now, df)
                return df

        if ALPHA_VANTAGE_KEY:
            df = _get_bars_alphavantage(proxy, timeframe, count)
            if df is not None and len(df) >= min_rows:
                print(f"   (using Alpha Vantage proxy fallback for {symbol})")
                _cache[key] = (now, df)
                return df

    print(f"   Warning: All data sources failed for {symbol}")
    return None


def _get_bars_databento(symbol, timeframe, count):
    """
    Primary source — real CME futures data via Databento.
    Uses continuous front-month contract (e.g. MNQ.c.0).

    Databento only offers ohlcv-1m / ohlcv-1h / ohlcv-1d natively —
    there is no 5m/15m schema. We pull 1-minute bars and resample
    locally to whatever timeframe the strategy actually needs.
    """
    global _databento_disabled
    try:
        client = _get_databento_client()
        if client is None:
            return None

        resample_minutes = {
            "5min": 5, "15min": 15, "1hour": 60,
        }.get(timeframe, None)

        if timeframe == "1day":
            schema = "ohlcv-1d"
        elif timeframe == "1hour":
            schema = "ohlcv-1h"
        else:
            schema = "ohlcv-1m"  # base resolution for 5min/15min, resampled below

        db_symbol = DATABENTO_SYMBOLS.get(symbol, f"{symbol}.c.0")

        # Need enough 1-min bars to produce `count` resampled bars
        multiplier = resample_minutes if resample_minutes else 1
        if schema == "ohlcv-1m":
            lookback_days = max(3, (count * multiplier // 1000) + 2)
        elif schema == "ohlcv-1h":
            lookback_days = max(10, (count // 24) + 5)
        else:  # ohlcv-1d
            lookback_days = int(count * 1.6) + 5

        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)

        data = client.timeseries.get_range(
            dataset    = "GLBX.MDP3",
            symbols    = [db_symbol],
            schema     = schema,
            stype_in   = "continuous",
            start      = start.strftime("%Y-%m-%d"),
            end        = end.strftime("%Y-%m-%d"),
        )

        df = data.to_df()
        if df is None or df.empty:
            return None

        df.index.name = "timestamp"
        df = df[["open", "high", "low", "close", "volume"]]

        # Resample 1-min bars up to 5min/15min if needed
        if resample_minutes and schema == "ohlcv-1m":
            df = df.resample(f"{resample_minutes}min").agg({
                "open":   "first",
                "high":   "max",
                "low":    "min",
                "close":  "last",
                "volume": "sum",
            }).dropna()

        return df.tail(count)

    except Exception as e:
        err = str(e).lower()
        if "credit" in err or "quota" in err or "insufficient" in err:
            print(f"   ⚠️ Databento credits exhausted — switching to proxy data for rest of session")
            _databento_disabled = True
        else:
            print(f"   Databento error for {symbol}: {e}")
        return None


def _get_bars_yfinance(proxy, timeframe, count):
    """Fallback source — Yahoo Finance via yfinance, free, no auth"""
    try:
        import yfinance as yf

        interval_map = {
            "5min": "5m", "15min": "15m", "1hour": "60m", "1day": "1d"
        }
        interval = interval_map.get(timeframe, "5m")

        period_map = {
            "5m": "5d", "15m": "1mo", "60m": "3mo", "1d": "1y"
        }
        period = period_map.get(interval, "5d")

        ticker = yf.Ticker(proxy)
        hist   = ticker.history(period=period, interval=interval)

        if hist.empty:
            return None

        hist = hist.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume"
        })
        hist.index.name = "timestamp"
        return hist[["open", "high", "low", "close", "volume"]].tail(count)

    except Exception as e:
        print(f"   yfinance error for {proxy}: {e}")
        return None


def _get_bars_finnhub(proxy, timeframe, count):
    """Fallback source — Finnhub free tier"""
    try:
        resolution_map = {"5min": "5", "15min": "15", "1hour": "60", "1day": "D"}
        resolution = resolution_map.get(timeframe, "5")

        now_ts   = int(datetime.now().timestamp())
        past_ts  = int((datetime.now() - timedelta(days=5)).timestamp())

        url = (
            f"https://finnhub.io/api/v1/stock/candle"
            f"?symbol={proxy}&resolution={resolution}"
            f"&from={past_ts}&to={now_ts}&token={FINNHUB_KEY}"
        )
        r = requests.get(url, timeout=10)
        data = r.json()

        if data.get("s") != "ok":
            return None

        df = pd.DataFrame({
            "timestamp": pd.to_datetime(data["t"], unit="s"),
            "open":   data["o"],
            "high":   data["h"],
            "low":    data["l"],
            "close":  data["c"],
            "volume": data["v"],
        })
        df.set_index("timestamp", inplace=True)
        return df.tail(count)

    except Exception as e:
        print(f"   Finnhub error for {proxy}: {e}")
        return None


def _get_bars_alphavantage(proxy, timeframe, count):
    """Final fallback — Alpha Vantage free tier"""
    try:
        interval_map = {"5min": "5min", "15min": "15min", "1hour": "60min"}

        if timeframe == "1day":
            func = "TIME_SERIES_DAILY"
            url = (
                f"https://www.alphavantage.co/query?function={func}"
                f"&symbol={proxy}&apikey={ALPHA_VANTAGE_KEY}"
            )
        else:
            interval = interval_map.get(timeframe, "5min")
            func = "TIME_SERIES_INTRADAY"
            url = (
                f"https://www.alphavantage.co/query?function={func}"
                f"&symbol={proxy}&interval={interval}"
                f"&apikey={ALPHA_VANTAGE_KEY}&outputsize=compact"
            )

        r    = requests.get(url, timeout=10)
        data = r.json()

        ts_key = next((k for k in data if "Time Series" in k), None)
        if not ts_key:
            return None

        rows = []
        for ts, vals in data[ts_key].items():
            rows.append({
                "timestamp": pd.to_datetime(ts),
                "open":   float(vals["1. open"]),
                "high":   float(vals["2. high"]),
                "low":    float(vals["3. low"]),
                "close":  float(vals["4. close"]),
                "volume": float(vals["5. volume"]),
            })

        df = pd.DataFrame(rows).set_index("timestamp").sort_index()
        return df.tail(count)

    except Exception as e:
        print(f"   Alpha Vantage error for {proxy}: {e}")
        return None


def get_quote(symbol):
    """Get latest price for a symbol — tries Databento, falls back to proxy"""
    if DATABENTO_KEY and not _databento_disabled:
        try:
            df = _get_bars_databento(symbol, "5min", 1)
            if df is not None and len(df) > 0:
                return {"symbol": symbol, "last": float(df.iloc[-1]["close"])}
        except Exception:
            pass

    proxy = PROXY_MAP.get(symbol, symbol)
    try:
        import yfinance as yf
        ticker = yf.Ticker(proxy)
        price  = ticker.fast_info.get("lastPrice")
        if price:
            return {"symbol": symbol, "last": price}
    except Exception as e:
        print(f"   Quote error for {symbol}: {e}")
    return None


def data_source_status():
    """Returns which source is the current PRIMARY for intraday data, for logging/reports"""
    if not DATABENTO_KEY:
        primary = "yfinance (QQQ/SPY proxy)"
    else:
        primary = "yfinance (QQQ/SPY proxy) — Databento Historical kept only for daily levels"
    return primary


def _get_bars_yfinance(proxy, timeframe, count):
    """Primary source — Yahoo Finance via yfinance, free, no auth"""
    try:
        import yfinance as yf

        interval_map = {
            "5min": "5m", "15min": "15m", "1hour": "60m", "1day": "1d"
        }
        interval = interval_map.get(timeframe, "5m")

        period_map = {
            "5m": "5d", "15m": "1mo", "60m": "3mo", "1d": "1y"
        }
        period = period_map.get(interval, "5d")

        ticker = yf.Ticker(proxy)
        hist   = ticker.history(period=period, interval=interval)

        if hist.empty:
            return None

        hist = hist.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume"
        })
        hist.index.name = "timestamp"
        return hist[["open", "high", "low", "close", "volume"]].tail(count)

    except Exception as e:
        print(f"   yfinance error for {proxy}: {e}")
        return None


def _get_bars_finnhub(proxy, timeframe, count):
    """Fallback source — Finnhub free tier"""
    try:
        resolution_map = {"5min": "5", "15min": "15", "1hour": "60", "1day": "D"}
        resolution = resolution_map.get(timeframe, "5")

        now_ts   = int(datetime.now().timestamp())
        past_ts  = int((datetime.now() - timedelta(days=5)).timestamp())

        url = (
            f"https://finnhub.io/api/v1/stock/candle"
            f"?symbol={proxy}&resolution={resolution}"
            f"&from={past_ts}&to={now_ts}&token={FINNHUB_KEY}"
        )
        r = requests.get(url, timeout=10)
        data = r.json()

        if data.get("s") != "ok":
            return None

        df = pd.DataFrame({
            "timestamp": pd.to_datetime(data["t"], unit="s"),
            "open":   data["o"],
            "high":   data["h"],
            "low":    data["l"],
            "close":  data["c"],
            "volume": data["v"],
        })
        df.set_index("timestamp", inplace=True)
        return df.tail(count)

    except Exception as e:
        print(f"   Finnhub error for {proxy}: {e}")
        return None


def _get_bars_alphavantage(proxy, timeframe, count):
    """Final fallback — Alpha Vantage free tier"""
    try:
        interval_map = {"5min": "5min", "15min": "15min", "1hour": "60min"}

        if timeframe == "1day":
            func = "TIME_SERIES_DAILY"
            url = (
                f"https://www.alphavantage.co/query?function={func}"
                f"&symbol={proxy}&apikey={ALPHA_VANTAGE_KEY}"
            )
        else:
            interval = interval_map.get(timeframe, "5min")
            func = "TIME_SERIES_INTRADAY"
            url = (
                f"https://www.alphavantage.co/query?function={func}"
                f"&symbol={proxy}&interval={interval}"
                f"&apikey={ALPHA_VANTAGE_KEY}&outputsize=compact"
            )

        r    = requests.get(url, timeout=10)
        data = r.json()

        ts_key = next((k for k in data if "Time Series" in k), None)
        if not ts_key:
            return None

        rows = []
        for ts, vals in data[ts_key].items():
            rows.append({
                "timestamp": pd.to_datetime(ts),
                "open":   float(vals["1. open"]),
                "high":   float(vals["2. high"]),
                "low":    float(vals["3. low"]),
                "close":  float(vals["4. close"]),
                "volume": float(vals["5. volume"]),
            })

        df = pd.DataFrame(rows).set_index("timestamp").sort_index()
        return df.tail(count)

    except Exception as e:
        print(f"   Alpha Vantage error for {proxy}: {e}")
        return None


def get_quote(symbol):
    """Get latest price for a symbol using the proxy"""
    proxy = PROXY_MAP.get(symbol, symbol)
    try:
        import yfinance as yf
        ticker = yf.Ticker(proxy)
        price  = ticker.fast_info.get("lastPrice")
        if price:
            return {"symbol": symbol, "last": price}
    except Exception as e:
        print(f"   Quote error for {symbol}: {e}")
    return None
