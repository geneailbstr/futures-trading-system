"""
tradovate.py — Direct Tradovate API connection
Handles authentication, market data, and order placement
"""

import os
import uuid
import requests
import time
import json
import pandas as pd
from datetime import datetime, timedelta
import config

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# ─────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────
BASE_URLS = {
    "demo": {
        "auth":   "https://demo.tradovateapi.com/v1",
        "md":     "https://md.tradovateapi.com/v1",
    },
    "live": {
        "auth":   "https://live.tradovateapi.com/v1",
        "md":     "https://md.tradovateapi.com/v1",
    }
}

ENV          = config.TRADOVATE_ENV
AUTH_URL     = BASE_URLS[ENV]["auth"]
MD_URL       = BASE_URLS[ENV]["md"]

# ─────────────────────────────────────────
# CONNECTION STATE
# ─────────────────────────────────────────
_access_token     = None
_token_expiry     = None
_account_id       = None
_account_spec     = None

# ─────────────────────────────────────────
# AUTHENTICATION
# ─────────────────────────────────────────
def _require_credentials():
    """Fail loudly instead of sending Tradovate a blank auth field.

    Called after _ensure_device_id() so a fresh install's not-yet-persisted
    deviceId doesn't trip this — this check is the safety net for a write
    that silently failed, not the primary path for populating deviceId.
    """
    missing = []
    if not config.TRADOVATE_USERNAME:   missing.append("TRADOVATE_USERNAME")
    if not config.TRADOVATE_PASSWORD:   missing.append("TRADOVATE_PASSWORD")
    if not config.TRADOVATE_SEC:        missing.append("TRADOVATE_SEC")
    if not config.TRADOVATE_DEVICE_ID:  missing.append("TRADOVATE_DEVICE_ID")
    if missing:
        raise RuntimeError(
            "Missing required Tradovate credential(s) in .env: "
            f"{', '.join(missing)}. Copy .env.example to .env and fill "
            "these in before authenticating."
        )


def _ensure_device_id():
    """Return the persisted Tradovate deviceId, minting one on first run.

    Tradovate treats a changing deviceId as a new device, so this must be
    generated once and reused on every run rather than regenerated per
    session.
    """
    if config.TRADOVATE_DEVICE_ID:
        return config.TRADOVATE_DEVICE_ID

    new_id = str(uuid.uuid4())
    with open(_ENV_PATH, "a") as f:
        f.write(f"\nTRADOVATE_DEVICE_ID={new_id}\n")

    config.TRADOVATE_DEVICE_ID = new_id
    os.environ["TRADOVATE_DEVICE_ID"] = new_id
    print(f"⚠️  No TRADOVATE_DEVICE_ID found — generated and saved a new one to .env: {new_id}")
    return new_id


def authenticate():
    """Log into Tradovate and get access token"""
    global _access_token, _token_expiry, _account_id, _account_spec

    device_id = _ensure_device_id()
    _require_credentials()

    payload = {
    "name":        config.TRADOVATE_USERNAME,
    "password":    config.TRADOVATE_PASSWORD,
    "appId":       "Sample App",
    "appVersion":  "1.0",
    "cid":         config.TRADOVATE_CID,
    "deviceId":    device_id,
    "sec":         config.TRADOVATE_SEC
}

    try:
        response = requests.post(
            f"{AUTH_URL}/auth/accesstokenrequest",
            json=payload,
            timeout=10
        )
        data = response.json()

        if "accessToken" not in data:
            print(f"❌ Auth failed: {data}")
            return False

        _access_token  = data["accessToken"]
        _token_expiry  = datetime.now() + timedelta(hours=1)
        print(f"✅ Authenticated with Tradovate ({ENV} environment)")

        # Get account details
        _load_account()
        return True

    except Exception as e:
        print(f"❌ Authentication error: {e}")
        return False


def _load_account():
    """Load account ID and spec from Tradovate"""
    global _account_id, _account_spec

    try:
        accounts = _get("/account/list")
        if accounts:
            # Find MFFU account
            for acc in accounts:
                if config.ACCOUNT_ID and config.ACCOUNT_ID in str(acc.get("name", "")):
                    _account_id   = acc["id"]
                    _account_spec = acc["name"]
                    break
            # Fallback to first account
            if not _account_id:
                _account_id   = accounts[0]["id"]
                _account_spec = accounts[0]["name"]

            print(f"✅ Account loaded: {_account_spec} (ID: {_account_id})")
    except Exception as e:
        print(f"⚠️ Could not load account: {e}")


def ensure_authenticated():
    """Re-authenticate if token is expired"""
    global _access_token, _token_expiry

    if not _access_token or datetime.now() >= _token_expiry:
        print("🔄 Token expired — re-authenticating...")
        return authenticate()
    return True


# ─────────────────────────────────────────
# HTTP HELPERS
# ─────────────────────────────────────────
def _headers():
    return {
        "Authorization": f"Bearer {_access_token}",
        "Content-Type":  "application/json"
    }


def _get(endpoint, base=None, retries=3):
    """GET request with auto-retry"""
    url = f"{base or AUTH_URL}{endpoint}"
    for attempt in range(retries):
        try:
            ensure_authenticated()
            r = requests.get(url, headers=_headers(), timeout=10)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 401:
                authenticate()
        except Exception as e:
            print(f"⚠️ GET error (attempt {attempt+1}): {e}")
            time.sleep(2 ** attempt)
    return None


def _post(endpoint, payload, base=None, retries=3):
    """POST request with auto-retry"""
    url = f"{base or AUTH_URL}{endpoint}"
    for attempt in range(retries):
        try:
            ensure_authenticated()
            r = requests.post(url, headers=_headers(), json=payload, timeout=10)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 401:
                authenticate()
            else:
                print(f"⚠️ POST error {r.status_code}: {r.text}")
        except Exception as e:
            print(f"⚠️ POST error (attempt {attempt+1}): {e}")
            time.sleep(2 ** attempt)
    return None


# ─────────────────────────────────────────
# MARKET DATA
# ─────────────────────────────────────────
def get_contract_id(symbol):
    """Get Tradovate contract ID for a symbol"""
    data = _get(f"/contract/find?name={symbol}")
    if data and "id" in data:
        return data["id"]
    return None


def get_bars(symbol, timeframe="5min", count=100):
    """
    Fetch historical bars for a symbol
    Returns a pandas DataFrame with OHLCV data
    """
    try:
        # Map timeframe to Tradovate units
        unit_map = {
            "1min":  ("Minute", 1),
            "5min":  ("Minute", 5),
            "15min": ("Minute", 15),
            "1hour": ("Hour", 1),
            "1day":  ("Day", 1),
        }
        unit, unit_size = unit_map.get(timeframe, ("Minute", 5))

        contract_id = get_contract_id(symbol)
        if not contract_id:
            print(f"❌ Could not find contract for {symbol}")
            return None

        payload = {
            "symbol":       symbol,
            "contractId":   contract_id,
            "elementSize":  unit_size,
            "elementSizeUnit": unit,
            "withHistogram": False,
            "asFarAsTimestamp": (
                datetime.utcnow() - timedelta(days=5)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        }

        data = _get(
            f"/md/getChart?symbol={symbol}&chartDescription=%7B%22underlyingType%22%3A%22Minute%22%2C%22elementSize%22%3A{unit_size}%2C%22elementSizeUnit%22%3A%22UnderlyingUnits%22%2C%22withHistogram%22%3Afalse%7D&timeRange=%7B%22asMuchAsElements%22%3A{count}%7D",
            base=MD_URL
        )

        if not data or "bars" not in data:
            return None

        bars = data["bars"]
        df = pd.DataFrame(bars)
        df.columns = ["timestamp", "open", "high", "low", "close", "volume"] if len(df.columns) == 6 else df.columns
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        df = df.astype(float)
        return df

    except Exception as e:
        print(f"❌ Error fetching bars for {symbol}: {e}")
        return None


def get_quote(symbol):
    """Get current bid/ask/last price for a symbol"""
    try:
        data = _get(f"/md/getQuote?symbol={symbol}", base=MD_URL)
        if data:
            return {
                "symbol": symbol,
                "bid":    data.get("bid", 0),
                "ask":    data.get("ask", 0),
                "last":   data.get("price", 0),
                "volume": data.get("volume", 0),
            }
    except Exception as e:
        print(f"❌ Quote error for {symbol}: {e}")
    return None


# ─────────────────────────────────────────
# ACCOUNT INFO
# ─────────────────────────────────────────
def get_account_balance():
    """Get current account cash balance"""
    try:
        data = _get(f"/cashBalance/getcashbalancesnapshot?accountId={_account_id}")
        if data:
            return float(data.get("cashBalance", config.ACCOUNT_SIZE))
    except Exception as e:
        print(f"❌ Balance error: {e}")
    return config.ACCOUNT_SIZE


def get_open_positions():
    """Get all currently open positions"""
    try:
        data = _get(f"/position/list?accountId={_account_id}")
        if data:
            return [p for p in data if p.get("netPos", 0) != 0]
        return []
    except Exception as e:
        print(f"❌ Position error: {e}")
        return []


def get_todays_pnl():
    """Get today's realized P&L"""
    try:
        data = _get(f"/account/list")
        if data:
            for acc in data:
                if acc.get("id") == _account_id:
                    return float(acc.get("dayPnl", 0))
    except Exception as e:
        print(f"❌ P&L error: {e}")
    return 0.0


# ─────────────────────────────────────────
# ORDER PLACEMENT
# ─────────────────────────────────────────
def place_bracket_order(symbol, direction, quantity, stop_price, target_price):
    """
    Place a bracket order (entry + stop + target)
    direction: 'Buy' or 'Sell'
    """
    try:
        contract_id = get_contract_id(symbol)
        if not contract_id:
            print(f"❌ No contract ID for {symbol}")
            return None

        # Entry order
        entry = _post("/order/placeorder", {
            "accountSpec":  _account_spec,
            "accountId":    _account_id,
            "action":       direction,
            "symbol":       symbol,
            "orderQty":     quantity,
            "orderType":    "Market",
            "isAutomated":  True
        })

        if not entry or "orderId" not in entry:
            print(f"❌ Entry order failed for {symbol}")
            return None

        order_id = entry["orderId"]
        print(f"✅ {direction} {quantity} {symbol} — Order ID: {order_id}")

        # Stop loss order
        stop_action = "Sell" if direction == "Buy" else "Buy"
        _post("/order/placeorder", {
            "accountSpec":  _account_spec,
            "accountId":    _account_id,
            "action":       stop_action,
            "symbol":       symbol,
            "orderQty":     quantity,
            "orderType":    "Stop",
            "stopPrice":    round(stop_price, 2),
            "isAutomated":  True
        })

        # Take profit order (first target — half position)
        _post("/order/placeorder", {
            "accountSpec":  _account_spec,
            "accountId":    _account_id,
            "action":       stop_action,
            "symbol":       symbol,
            "orderQty":     max(1, quantity // 2),
            "orderType":    "Limit",
            "price":        round(target_price, 2),
            "isAutomated":  True
        })

        return order_id

    except Exception as e:
        print(f"❌ Order placement error: {e}")
        return None


def close_all_positions():
    """Force close all open positions — used at EOD"""
    positions = get_open_positions()
    if not positions:
        print("✅ No open positions to close")
        return

    for pos in positions:
        symbol   = pos.get("symbol", "")
        net_pos  = pos.get("netPos", 0)
        if net_pos == 0:
            continue

        action   = "Sell" if net_pos > 0 else "Buy"
        quantity = abs(net_pos)

        result = _post("/order/placeorder", {
            "accountSpec": _account_spec,
            "accountId":   _account_id,
            "action":      action,
            "symbol":      symbol,
            "orderQty":    quantity,
            "orderType":   "Market",
            "isAutomated": True
        })

        if result:
            print(f"✅ Closed {quantity} {symbol}")
        else:
            print(f"❌ Failed to close {symbol} — retry manually!")


def cancel_all_orders():
    """Cancel all pending orders"""
    try:
        orders = _get(f"/order/list?accountId={_account_id}")
        if not orders:
            return
        for order in orders:
            if order.get("ordStatus") in ["Working", "Accepted"]:
                _post("/order/cancelorder", {"orderId": order["id"]})
        print("✅ All pending orders cancelled")
    except Exception as e:
        print(f"❌ Cancel orders error: {e}")
