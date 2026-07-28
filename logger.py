"""
logger.py — Trade logging to CSV and console output
"""

import csv
import os
from datetime import datetime

LOG_FILE = os.path.join(os.path.dirname(__file__), "trades.csv")

HEADERS = [
    "date", "time", "symbol", "direction", "entry",
    "exit", "quantity", "pnl", "balance",
    "daily_pnl", "win_rate", "notes"
]


def init_log():
    """Create CSV file with headers if it doesn't exist"""
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS)
            writer.writeheader()
        print(f"📝 Trade log created: {LOG_FILE}")
    else:
        print(f"📝 Trade log found: {LOG_FILE}")


def log_trade(trade_dict, daily_pnl=0, win_rate=0, notes=""):
    """Append a trade to the CSV log"""
    try:
        row = {
            "date":      trade_dict.get("date", datetime.now().strftime("%Y-%m-%d")),
            "time":      trade_dict.get("time", datetime.now().strftime("%H:%M:%S")),
            "symbol":    trade_dict.get("symbol", ""),
            "direction": trade_dict.get("direction", ""),
            "entry":     trade_dict.get("entry", 0),
            "exit":      trade_dict.get("exit", 0),
            "quantity":  trade_dict.get("quantity", 0),
            "pnl":       trade_dict.get("pnl", 0),
            "balance":   trade_dict.get("balance", 0),
            "daily_pnl": round(daily_pnl, 2),
            "win_rate":  win_rate,
            "notes":     notes,
        }

        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS)
            writer.writerow(row)

    except Exception as e:
        print(f"⚠️ Log write error: {e}")


def print_session_header(balance, qualifying_days, total_profit):
    """Print clean session start banner"""
    print("\n" + "═" * 50)
    print("  🤖 MICRO FUTURES TRADING BOT")
    print("═" * 50)
    print(f"  Account:          ${balance:,.2f}")
    print(f"  Total profit:     ${total_profit:,.2f}")
    print(f"  Qualifying days:  {qualifying_days} / 5")
    print(f"  Session date:     {datetime.now().strftime('%A, %B %d %Y')}")
    print("═" * 50 + "\n")


def print_trade_signal(signal, contracts):
    """Print a trade signal to terminal"""
    direction_emoji = "🟢" if signal["direction"] == "Buy" else "🔴"
    print(f"\n{direction_emoji} SIGNAL DETECTED")
    print(f"   Symbol:    {signal['symbol']}")
    print(f"   Direction: {signal['direction']}")
    print(f"   Entry:     {signal['entry']}")
    print(f"   Stop:      {signal['stop']}")
    print(f"   Target 1:  {signal['target1']}")
    print(f"   Target 2:  {signal['target2']}")
    print(f"   Contracts: {contracts}")
    print(f"   Reason:    {signal['reason']}")


def print_status(rm, regime, restriction):
    """Print current bot status"""
    can_trade, reason = rm.check_daily_limits()
    status_emoji      = "✅" if can_trade else "⛔"

    print(f"\n{'─'*40}")
    print(f"  {status_emoji} Status: {'TRADING' if can_trade else 'PAUSED'}")
    if not can_trade:
        print(f"  Reason: {reason}")
    print(f"  Regime:     {regime}")
    print(f"  Daily P&L:  ${rm.daily_pnl:+.2f}")
    print(f"  Trades:     {rm.trades_today} / 5")
    print(f"  Wins:       {rm.wins_today} | Losses: {rm.losses_today}")
    if restriction["restricted"]:
        print(f"  📰 News: {restriction['event']} ({restriction['action']})")
    print(f"{'─'*40}\n")
