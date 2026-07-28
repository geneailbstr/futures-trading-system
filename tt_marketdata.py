"""
tt_marketdata.py — REAL tastytrade/DXLink futures data client (production)

Built on the PROVEN manual approach (the SDK's OAuth was broken, so we do it
ourselves). Confirmed working end-to-end 2026-07-08: real MNQ 5-min candles
with historical backfill at correct scale (~29,400).

Pipeline:
  1. Mint OAuth access token directly from /oauth/token (SDK-independent)
  2. Resolve front-month contract via /instruments/futures
  3. Get DXLink quote token + wss url via /api-quote-tokens
  4. Connect DXLink websocket, proper handshake sequence, subscribe to Candle
     with fromTime for historical backfill
  5. Parse COMPACT feed data -> canonical OHLCV DataFrame

Output shape (matches the old yfinance path exactly — drop-in for the bot):
    index name : "timestamp"
    columns    : ["open","high","low","close","volume"]
    returns    : .tail(count), oldest -> newest

Credentials from .env:
    TASTYTRADE_CLIENT_SECRET
    TASTYTRADE_REFRESH_TOKEN
"""

import os
import json
import time
import ssl
import asyncio
import threading
from datetime import datetime, timedelta, timezone

import httpx
import certifi
import pandas as pd
from dotenv import load_dotenv
load_dotenv()

SECRET = os.getenv("TASTYTRADE_CLIENT_SECRET")
REFRESH = os.getenv("TASTYTRADE_REFRESH_TOKEN")
API = "https://api.tastyworks.com"
UA = "trading-bot/1.0"

PRODUCT_ROOT = {"MNQ": "MNQ", "MES": "MES"}
_TF_INTERVAL = {"5min": "5m", "15min": "15m", "1hour": "1h", "1day": "1d"}
_TF_LOOKBACK_HOURS = {"5min": 6, "15min": 18, "1hour": 72, "1day": 24 * 400}

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

# ── token + symbol caches (avoid re-minting on every call) ──
_access_token = None
_access_minted_at = 0.0
_ACCESS_TTL = 12 * 60  # access tokens live 15 min; refresh at 12 to be safe
_symbol_cache = {}     # {"MNQ": "/MNQU26:XCME", ...}
_lock = threading.Lock()


def _mint_access_token(force=False):
    global _access_token, _access_minted_at
    if not force and _access_token and (time.time() - _access_minted_at) < _ACCESS_TTL:
        return _access_token
    if not SECRET or not REFRESH:
        raise RuntimeError("TASTYTRADE_CLIENT_SECRET / TASTYTRADE_REFRESH_TOKEN not set in .env")
    r = httpx.post(f"{API}/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": REFRESH,
        "client_secret": SECRET}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"OAuth token mint failed {r.status_code}: {r.text[:200]}")
    _access_token = r.json()["access_token"]
    _access_minted_at = time.time()
    return _access_token


def _auth_headers():
    return {"Authorization": f"Bearer {_mint_access_token()}", "User-Agent": UA}


def _resolve_front_month(symbol):
    if symbol in _symbol_cache:
        return _symbol_cache[symbol]
    root = PRODUCT_ROOT.get(symbol, symbol)
    r = httpx.get(f"{API}/instruments/futures",
                  params={"product-code[]": root}, headers=_auth_headers(), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"instruments/futures failed {r.status_code}: {r.text[:200]}")
    items = r.json().get("data", {}).get("items", [])
    if not items:
        raise RuntimeError(f"No futures contracts returned for {root}")
    # front month = active month if flagged, else nearest expiration
    actives = [i for i in items if i.get("active-month")]
    pool = actives if actives else items
    pool = sorted(pool, key=lambda x: x.get("expiration-date", ""))
    streamer = pool[0]["streamer-symbol"]
    _symbol_cache[symbol] = streamer
    return streamer


def _get_quote_token():
    r = httpx.get(f"{API}/api-quote-tokens", headers=_auth_headers(), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"api-quote-tokens failed {r.status_code}: {r.text[:200]}")
    d = r.json()["data"]
    return d["token"], d["dxlink-url"]


async def _stream(quote_token, dxlink_url, resolved, interval, lookback_hours,
                  count, timeout=30):
    """
    resolved: {bot_symbol: streamer_symbol}
    Returns {bot_symbol: {time_ms: candle_dict}}
    """
    import websockets

    from_time = int((datetime.now(timezone.utc)
                     - timedelta(hours=lookback_hours)).timestamp() * 1000)
    # candle symbol per instrument
    csyms = {bot: f"{strm}{{={interval}}}" for bot, strm in resolved.items()}
    # map candle-symbol -> bot symbol (feed echoes the candle symbol)
    rev = {v: k for k, v in csyms.items()}
    collected = {bot: {} for bot in resolved}
    fields = ["eventType", "eventSymbol", "time", "open", "high", "low", "close", "volume"]
    n = len(fields)

    async with websockets.connect(dxlink_url, ssl=_SSL_CTX) as ws:
        async def send(m): await ws.send(json.dumps(m))

        await send({"type": "SETUP", "channel": 0, "version": "0.1-js/1.0",
                    "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})

        # wait for AUTHORIZED
        authorized = False
        for _ in range(12):
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            t = m.get("type")
            if t == "AUTH_STATE" and m.get("state") == "UNAUTHORIZED":
                await send({"type": "AUTH", "channel": 0, "token": quote_token})
            elif t == "AUTH_STATE" and m.get("state") == "AUTHORIZED":
                authorized = True
                break
        if not authorized:
            raise RuntimeError("DXLink never reached AUTHORIZED")

        # open channel
        await send({"type": "CHANNEL_REQUEST", "channel": 1, "service": "FEED",
                    "parameters": {"contract": "AUTO"}})
        opened = False
        for _ in range(12):
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            if m.get("type") == "CHANNEL_OPENED":
                opened = True
                break
        if not opened:
            raise RuntimeError("DXLink channel never opened")

        # configure + subscribe
        await send({"type": "FEED_SETUP", "channel": 1,
                    "acceptAggregationPeriod": 1, "acceptDataFormat": "COMPACT",
                    "acceptEventFields": {"Candle": fields}})
        await asyncio.wait_for(ws.recv(), timeout=8)  # FEED_CONFIG

        await send({"type": "FEED_SUBSCRIPTION", "channel": 1, "reset": True,
                    "add": [{"type": "Candle", "symbol": cs, "fromTime": from_time}
                            for cs in csyms.values()]})

        deadline = time.time() + timeout
        last_ka = time.time()
        while time.time() < deadline:
            if time.time() - last_ka > 20:
                await send({"type": "KEEPALIVE", "channel": 0})
                last_ka = time.time()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("type") != "FEED_DATA":
                continue
            data = msg.get("data", [])
            if len(data) >= 2 and data[0] == "Candle":
                flat = data[1]
                for i in range(0, len(flat), n):
                    row = flat[i:i+n]
                    if len(row) != n:
                        break
                    rec = dict(zip(fields, row))
                    esym = rec.get("eventSymbol")
                    bot = rev.get(esym)
                    if bot is None:
                        # match by prefix (strip the {=..} suffix)
                        for cs, b in rev.items():
                            if esym and esym.split("{")[0] == cs.split("{")[0]:
                                bot = b
                                break
                    if bot is not None and rec.get("time") is not None:
                        collected[bot][rec["time"]] = rec
            if all(len(collected[b]) >= count + 3 for b in resolved):
                break
    return collected


def _to_df(candle_map, count):
    if not candle_map:
        return None
    rows = []
    for t, c in candle_map.items():
        try:
            o = float(c["open"]); h = float(c["high"])
            l = float(c["low"]); cl = float(c["close"])
            vraw = c.get("volume")
            v = float(vraw) if vraw not in (None, "NaN") else 0.0
        except (TypeError, ValueError, KeyError):
            continue
        # drop the in-progress live candle (NaN OHLC)
        if any(x != x for x in (o, h, l, cl)):  # NaN check
            continue
        rows.append((pd.to_datetime(int(t), unit="ms"), o, h, l, cl, v))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").set_index("timestamp")
    return df[["open", "high", "low", "close", "volume"]].tail(count)


def get_bars_multi(bot_symbols, timeframe, count):
    """Fetch real futures bars for multiple symbols in one DXLink session.
    Returns {bot_symbol: DataFrame or None}."""
    interval = _TF_INTERVAL.get(timeframe, "5m")
    lookback = _TF_LOOKBACK_HOURS.get(timeframe, 6)
    with _lock:
        resolved = {s: _resolve_front_month(s) for s in bot_symbols}
        quote_token, url = _get_quote_token()
        raw = asyncio.run(_stream(quote_token, url, resolved, interval, lookback, count))
    out = {}
    for bot in bot_symbols:
        df = _to_df(raw.get(bot, {}), count)
        if df is not None and not df.empty:
            last = float(df["close"].iloc[-1])
            if bot == "MNQ" and last < 5000:
                print(f"   🚨 MNQ close {last} proxy-scale — rejecting"); df = None
            elif bot == "MES" and last < 2000:
                print(f"   🚨 MES close {last} proxy-scale — rejecting"); df = None
        out[bot] = df
    return out


def get_bars_tastytrade(symbol, timeframe, count):
    """Single-symbol wrapper matching the yfinance-style signature."""
    return get_bars_multi([symbol], timeframe, count).get(symbol)


def selftest():
    print("Pulling MNQ + MES 5min from tastytrade DXLink...\n")
    res = get_bars_multi(["MNQ", "MES"], "5min", 10)
    for sym in ("MNQ", "MES"):
        df = res.get(sym)
        print(f"=== {sym} 5min ===")
        if df is None:
            print("   NO DATA\n")
        else:
            print(df.tail(5))
            print(f"   last close: {df['close'].iloc[-1]}\n")


if __name__ == "__main__":
    selftest()
