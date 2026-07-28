"""
notify.py — Gmail email notifications
Sends daily simulation report and immediate max-payout alert.
Profit split corrected to 80% (confirmed from MFFU Payout Rules screenshot 2026-07-01)
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import config


def send_max_payout_alert(summary):
    """
    Send an IMMEDIATE alert when the $1,000 max payout request is available.
    Called mid-session from record_trade() — doesn't wait for EOD.
    Confirmed rules: $1,000 max request, 80% split = $800 to you.
    """
    try:
        payout_to_you = summary.get("payout_to_you", 800.0)
        payout_count  = summary.get("payout_count", 0)
        max_payouts   = summary.get("max_sim_payouts", 5)
        remaining     = max_payouts - payout_count
        mll_note      = ""
        if payout_count == 0:
            mll_note = "\n⚠️  FIRST PAYOUT: MLL will lock after this request.\n" \
                       "   Effective max drawdown becomes $900 (was $1,000) permanently."

        subject = f"💰 MAX PAYOUT AVAILABLE — ${payout_to_you:,.2f} ready to withdraw"

        body = f"""
💰 MAXIMUM PAYOUT NOW AVAILABLE
{'═'*50}

Your sim-funded account has hit the $1,000
maximum single payout threshold.

Max request amount:   $1,000.00
You receive (80%):    ${payout_to_you:,.2f}
{mll_note}
{'─'*50}
Sim payouts used:     {payout_count} / {max_payouts}
Payouts remaining:    {remaining}
{'─'*50}

ACTION REQUIRED:
Log into myfundedfutures.com → Payouts
Request $1,000 now to receive ${payout_to_you:,.2f}

After {max_payouts} total payouts your account
graduates to a LIVE funded account.
{'═'*50}
        """.strip()

        msg             = MIMEMultipart()
        msg["From"]     = config.GMAIL_ADDRESS
        msg["To"]       = config.NOTIFY_EMAIL
        msg["Subject"]  = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            server.send_message(msg)

        print(f"📧 Max payout alert sent to {config.NOTIFY_EMAIL}")
        return True

    except Exception as e:
        print(f"❌ Max payout alert email failed: {e}")
        return False


def send_simulation_report(summary):
    """Send end of day SIMULATION report"""
    try:
        plan    = summary.get("plan", "FLEX")
        pnl     = summary["daily_pnl"]
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        emoji   = "✅" if pnl >= 0 else "❌"
        subject = f"🧪 SIM Report ({plan}) {summary['date']} — {pnl_str} {emoji}"

        trade_lines = ""
        for t in summary.get("trade_log", []):
            t_emoji = "✅" if t["net_pnl"] > 0 else "❌"
            trade_lines += (
                f"  {t_emoji} {t['time']}  {t['direction']:4s} "
                f"{t['quantity']}x {t['symbol']:4s}  "
                f"fill {t['fill_price']:<8} exit {t['exit_price']:<8} "
                f"comm -${t['commission']:.2f}  net ${t['net_pnl']:+.2f}\n"
            )
        if not trade_lines:
            trade_lines = "  No trades today\n"

        # Eval progress section
        eval_status  = "PASSED ✅" if summary.get("eval_passed") else "In progress"
        eval_pct     = (summary["realized_profit"] /
                        summary.get("eval_profit_target", 1500) * 100
                        if summary.get("eval_profit_target") else 0)

        # Payout section — confirmed 80% split, $1K max
        payout_block = ""
        if summary.get("payout_eligible"):
            max_flag = " ← MAX AVAILABLE 💰" if summary.get("max_payout_reached") else ""
            payout_block = f"""
PAYOUT AVAILABLE
{'─'*50}
Requestable:          ${summary['payout_available']:,.2f}{max_flag}
You receive (80%):    ${summary['payout_to_you']:,.2f}
Max per request:      $1,000.00
Profit since payout:  ${summary.get('profit_since_payout', 0):,.2f}
Sim payouts used:     {summary['payout_count']} / {summary['max_sim_payouts']}
MLL locked:           {'YES — max drawdown now $900' if summary.get('mll_locked') else 'No'}
{'─'*50}"""
        else:
            days_needed = max(0, 5 - summary.get("qualifying_days", 0))
            payout_block = f"""
PAYOUT STATUS
{'─'*50}
Not yet eligible
Qualifying days:      {summary.get('qualifying_days', 0)} / 5
Days still needed:    {days_needed}
Profit since payout:  ${summary.get('profit_since_payout', 0):,.2f} (need $250)
{'─'*50}"""

        mll_note = ""
        if summary.get("mll_locked"):
            mll_note = f"\nNote: MLL locked — effective max drawdown is now $900"

        body = f"""
🧪 SIMULATION MODE — NOT REAL MONEY
{plan} $25K Rules (slippage + commissions modeled)
{'═'*50}
Date: {summary['date']}
{'═'*50}

TRADES TODAY
{'─'*50}
{trade_lines}
{'─'*50}
Trades:   {summary['trades']}
Wins:     {summary['wins']}
Losses:   {summary['losses']}
Day P&L:  ${summary['daily_pnl']:+.2f}
{'─'*50}

ACCOUNT STATUS
{'─'*50}
Balance:          ${summary['balance']:,.2f}
Realized profit:  ${summary['realized_profit']:+,.2f}
Profit split:     80% (you keep 80 cents of every dollar)
{mll_note}
{'─'*50}

EVAL PROGRESS
{'─'*50}
Status:           {eval_status}
Profit target:    ${summary.get('eval_profit_target', 1500):,.2f}
Progress:         ${summary['realized_profit']:,.2f} ({eval_pct:.1f}%)
Trading days:     {summary.get('eval_trading_days', 0)} / 2 required
Qualifying days:  {summary.get('qualifying_days', 0)} / 5
Today qualifies:  {'YES ✅' if summary.get('is_qualifying_day') else 'NO (< $100)'}
{payout_block}

This is a simulation. No real orders placed.
Flip SIMULATION_MODE = False in config.py to go live.
{'═'*50}
        """.strip()

        msg             = MIMEMultipart()
        msg["From"]     = config.GMAIL_ADDRESS
        msg["To"]       = config.NOTIFY_EMAIL
        msg["Subject"]  = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            server.send_message(msg)

        print(f"📧 Simulation report sent to {config.NOTIFY_EMAIL}")
        return True

    except Exception as e:
        print(f"❌ Email failed: {e}")
        return False


def send_daily_report(summary):
    """Send end of day LIVE trading report (when SIMULATION_MODE = False)"""
    try:
        pnl     = summary.get("daily_pnl", 0)
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        emoji   = "✅" if pnl >= 0 else "❌"
        subject = f"🤖 Bot Report {summary.get('date', '')} — {pnl_str} {emoji}"

        trade_lines = ""
        for t in summary.get("trade_log", []):
            t_emoji = "✅" if t["pnl"] > 0 else "❌"
            trade_lines += (
                f"  {t_emoji} {t.get('time', '')}  "
                f"{t.get('direction', ''):4s} {t.get('quantity', '')}x "
                f"{t.get('symbol', ''):4s}  ${t.get('pnl', 0):+.2f}\n"
            )
        if not trade_lines:
            trade_lines = "  No trades today\n"

        body = f"""
🤖 MICRO FUTURES BOT — DAILY REPORT
{'═'*45}
Date: {summary.get('date', '')}

TRADES TODAY
{'─'*45}
{trade_lines}
{'─'*45}
Trades:    {summary.get('trades', 0)}
Wins:      {summary.get('wins', 0)}
Losses:    {summary.get('losses', 0)}
Win rate:  {summary.get('win_rate', 0)}%
Day P&L:   ${summary.get('daily_pnl', 0):+.2f}

ACCOUNT
{'─'*45}
Balance:          ${summary.get('balance', 0):,.2f}
Total profit:     ${summary.get('total_profit', 0):+,.2f}
Qualifying days:  {summary.get('qualifying_days', 0)} / 5
{'─'*45}
        """.strip()

        msg             = MIMEMultipart()
        msg["From"]     = config.GMAIL_ADDRESS
        msg["To"]       = config.NOTIFY_EMAIL
        msg["Subject"]  = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            server.send_message(msg)

        print(f"📧 Daily report sent to {config.NOTIFY_EMAIL}")
        return True

    except Exception as e:
        print(f"❌ Email failed: {e}")
        return False


def send_alert(subject, message):
    """Send a quick alert email"""
    try:
        msg             = MIMEMultipart()
        msg["From"]     = config.GMAIL_ADDRESS
        msg["To"]       = config.NOTIFY_EMAIL
        msg["Subject"]  = subject
        msg.attach(MIMEText(message, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            server.send_message(msg)

        print(f"📧 Alert sent: {subject}")

    except Exception as e:
        print(f"❌ Alert email failed: {e}")
