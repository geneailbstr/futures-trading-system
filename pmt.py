"""
pmt.py — PickMyTrade webhook execution (rebuilt to match PMT's real schema)

Payload shape confirmed from a PickMyTrade "Generate Alert" template
(Tradovate, MFFU Flex $25K, dollar-based SL/TP). Key differences from the
old guessed payload:
  - action field is "data" (values: "buy" / "sell" / "close"), not "action"
  - stop/target are DOLLAR amounts: dollar_sl / dollar_tp  (scale-independent —
    this is what lets a proxy-fed strategy trade real MNQ/MES safely)
  - order type is "MKT"
  - token comes from .env (PICKMYTRADE_TOKEN), appears top-level AND inside
    multiple_accounts[].token
  - account_id identifies the MFFU account
  - price/date are TradingView template vars in the dashboard sample; since we
    are NOT TradingView we send real values (or omit) — never the literal
    "{{close}}" string.

Safety: nothing sends unless config.PMT_TEMPLATE_VERIFIED is True. Flip that
in config.py only after the live manual test order confirms fills land correctly
in Tradovate.
"""

import os
import requests
import config

# Token is a live credential — loaded from environment, never hardcoded.
PMT_TOKEN = os.getenv("PICKMYTRADE_TOKEN")

# Default MFFU account id; can be overridden per call if you ever run multiple.
DEFAULT_ACCOUNT_ID = os.getenv("TRADOVATE_ACCOUNT_ID", "MFFUEVFLX602266002")

# PickMyTrade endpoint timeout (seconds)
_TIMEOUT = 10


def _preflight(require_sltp=False, dollar_sl=None, dollar_tp=None):
    """
    Shared guard checks. Returns (ok: bool, reason: str).
    """
    if not getattr(config, "PMT_TEMPLATE_VERIFIED", False):
        return False, ("PMT_TEMPLATE_VERIFIED is False — refusing to send. "
                       "Confirm a live manual test order fills correctly in "
                       "Tradovate, then set PMT_TEMPLATE_VERIFIED = True in config.py")
    if not config.PICKMYTRADE_WEBHOOK:
        return False, "PICKMYTRADE_WEBHOOK not set in .env"
    if not PMT_TOKEN:
        return False, "PICKMYTRADE_TOKEN not set in .env"
    if require_sltp:
        if not dollar_sl or dollar_sl <= 0:
            return False, f"dollar_sl must be > 0 (got {dollar_sl})"
        if not dollar_tp or dollar_tp <= 0:
            return False, f"dollar_tp must be > 0 (got {dollar_tp})"
    return True, "OK"


def _build_payload(symbol, data_action, quantity,
                   dollar_sl=0, dollar_tp=0, order_type="MKT", price=0):
    """
    Construct the PMT payload matching the confirmed schema.
    dollar_sl / dollar_tp are dollar risk/target amounts (scale-independent).
    """
    return {
        "strategy_name":        "mffu-bot",
        "symbol":               symbol,
        # We are not TradingView — omit template vars. PMT fills market price
        # on a MKT order; sending an empty string avoids the literal "{{close}}".
        "date":                 "",
        "data":                 data_action,          # "buy" | "sell" | "close"
        "quantity":             int(quantity),
        "risk_percentage":      0,                     # bot controls sizing, not PMT
        "price":                round(float(price), 2),   # PMT wants a numeric price
        "stp_limit_stp_price":  0,
        "tp":                   0,
        "percentage_tp":        0,
        "dollar_tp":            round(float(dollar_tp), 2),
        "sl":                   0,
        "percentage_sl":        0,
        "dollar_sl":            round(float(dollar_sl), 2),
        "trail":                0,
        "trail_stop":           0,
        "trail_trigger":        0,
        "trail_freq":           0,
        "update_tp":            False,
        "update_sl":            False,
        "breakeven":            0,
        "breakeven_offset":     0,
        "token":                PMT_TOKEN,
        "pyramid":              False,                 # never stack same-direction
        "same_direction_ignore": False,
        "reverse_order_close":  True,                  # flip auto-closes opposite
        "order_type":           order_type,
        "multiple_accounts": [
            {
                "token":              PMT_TOKEN,
                "account_id":         DEFAULT_ACCOUNT_ID,
                "risk_percentage":    0,
                "quantity_multiplier": 1,              # must stay 1 — bot owns size
            }
        ],
    }


def _post(payload, label):
    """POST a payload to the PMT webhook, with consistent logging."""
    try:
        resp = requests.post(config.PICKMYTRADE_WEBHOOK, json=payload, timeout=_TIMEOUT)
        if resp.status_code == 200:
            print(f"✅ PMT {label} sent OK")
            return True
        print(f"❌ PMT {label} HTTP {resp.status_code}: {resp.text[:300]}")
        return False
    except Exception as e:
        print(f"❌ PMT {label} webhook error: {e}")
        return False


def send_trade(symbol, action, quantity, dollar_sl, dollar_tp, order_type="MKT", price=None):
    """
    Send an entry order to PickMyTrade.

    symbol      : "MNQ" or "MES"
    action      : "buy" or "sell"  (case-insensitive)
    quantity    : integer contracts (bot-calculated; PMT multiplier is 1)
    dollar_sl   : stop-loss as a DOLLAR amount (> 0)
    dollar_tp   : take-profit as a DOLLAR amount (> 0)
    """
    data_action = action.strip().lower()
    if data_action not in ("buy", "sell"):
        print(f"❌ send_trade: invalid action '{action}' (want buy/sell)")
        return False

    ok, reason = _preflight(require_sltp=True, dollar_sl=dollar_sl, dollar_tp=dollar_tp)
    if not ok:
        print(f"🚫 send_trade blocked — {reason}")
        return False

    # PMT requires a numeric price field even for market orders.
    if price is None:
        try:
            import tt_marketdata
            _df = tt_marketdata.get_bars_tastytrade(symbol, '5min', 3)
            price = float(_df['close'].iloc[-1]) if _df is not None and len(_df) else 0
        except Exception as _e:
            print(f'   ⚠️ could not fetch price for PMT payload: {_e}')
            price = 0
    payload = _build_payload(symbol, data_action, quantity,
                             dollar_sl=dollar_sl, dollar_tp=dollar_tp,
                             order_type=order_type, price=price)
    print(f"→ PMT ENTRY: {data_action.upper()} {quantity} {symbol} "
          f"| SL ${dollar_sl:.2f} | TP ${dollar_tp:.2f}")
    return _post(payload, f"entry {data_action} {symbol}")


def close_position(symbol):
    """Send a flatten/close for one symbol."""
    ok, reason = _preflight()
    if not ok:
        print(f"🚫 close_position blocked — {reason}")
        return False

    payload = _build_payload(symbol, "close", quantity=0)
    print(f"→ PMT CLOSE: {symbol}")
    return _post(payload, f"close {symbol}")


def close_all():
    """Close all configured instruments (used at 15:45 force-close)."""
    results = []
    for symbol in config.INSTRUMENTS:
        results.append(close_position(symbol))
    return all(results)
