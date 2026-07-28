"""
bot.py — Main trading bot
Run with: python3 bot.py

SIMULATION_MODE (config.py):
  True  -> paper trading, realistic slippage/commission, Rapid rules, no real orders
  False -> live trading via PickMyTrade webhook
"""

import time
import schedule
import pytz
import sys
import os
from datetime import datetime, date

import config
import marketdata
import strategy
import risk
import simulator
import ecocal as cal
import logger
import notify
import sentiment
import pmt

ET = pytz.timezone("America/New_York")


# ─────────────────────────────────────────
# PERSISTENT LOGGING — every diagnostic line that prints to the
# terminal also gets written to a dated file on disk. This means
# closing Terminal, restarting the Mac, or anything else no longer
# loses the day's diagnostic history — it's always recoverable from
# the logs/ folder afterward.
# ─────────────────────────────────────────
LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)


class _TeeOutput:
    """Writes everything to both the real terminal AND a log file"""
    def __init__(self, terminal_stream):
        self.terminal = terminal_stream
        self.log_path = None
        self.log_file = None
        self._open_todays_file()

    def _open_todays_file(self):
        today = datetime.now(ET).strftime("%Y-%m-%d")
        new_path = os.path.join(LOGS_DIR, f"bot_log_{today}.txt")
        if new_path != self.log_path:
            if self.log_file:
                self.log_file.close()
            self.log_path = new_path
            self.log_file = open(self.log_path, "a", buffering=1)  # line-buffered

    def write(self, message):
        self._open_todays_file()  # rolls over to a new file at midnight automatically
        self.terminal.write(message)
        self.log_file.write(message)

    def flush(self):
        self.terminal.flush()
        self.log_file.flush()


sys.stdout = _TeeOutput(sys.stdout)
sys.stderr = _TeeOutput(sys.stderr)


# Globals
risk_manager   = None
sim            = None
market_regime  = "TRENDING"
prev_high      = None
prev_low       = None
gap_detected   = False
session_active = False


def is_trading_day():
    today = date.today()
    if today.weekday() >= 5:
        print("Weekend — no trading today")
        return False
    if today.isoformat() in config.MARKET_HOLIDAYS:
        print("Market holiday — no trading today")
        return False
    return True


def pre_market_setup():
    global risk_manager, sim, market_regime, prev_high, prev_low
    global gap_detected, session_active

    print(f"\n{'='*50}")
    print(f"  BOT WAKING UP — {datetime.now(ET).strftime('%A %B %d %Y')}")
    if config.SIMULATION_MODE:
        print(f"  MODE: SIMULATION ({config.ACCOUNT_PLAN} $25K rules, no real orders)")
    else:
        print(f"  MODE: LIVE ({config.ACCOUNT_PLAN} — PickMyTrade execution)")
    print(f"{'='*50}")

    if not is_trading_day():
        return

    logger.init_log()
    cal.print_todays_schedule()

    if config.SIMULATION_MODE:
        sim = simulator.TradeSimulator()
    else:
        balance      = config.ACCOUNT_SIZE
        risk_manager = risk.RiskManager(balance)
        logger.print_session_header(balance, risk_manager.qualifying_days, risk_manager.total_profit)

    # Load prev day levels using free market data
    for symbol in config.INSTRUMENTS:
        try:
            df_daily = marketdata.get_bars(symbol, "1day", 5)
            if df_daily is not None:
                ph, pl       = strategy.get_prev_day_levels(df_daily)
                prev_high    = ph
                prev_low     = pl
                gap_detected = strategy.detect_gap(df_daily)
                print(f"   {symbol} — Prev High: {ph} | Prev Low: {pl}")
        except Exception as e:
            print(f"   Warning: Could not load {symbol} daily data: {e}")

    # Market regime check
    try:
        df_15min = marketdata.get_bars(config.INSTRUMENTS[0], "15min", 50)
        if df_15min is not None:
            market_regime = strategy.detect_market_regime(df_15min)
            print(f"\nMarket regime: {market_regime}")
            if market_regime == "VOLATILE":
                print("Extreme volatility — sitting out today")
                notify.send_alert("Bot Alert: Volatile Market",
                    "Extreme volatility detected. Bot sitting out today.")
                return
    except Exception as e:
        print(f"Warning: Regime detection failed: {e}")
        market_regime = "TRENDING"

    session_active = True
    mode_label = "PickMyTrade webhook" if not config.SIMULATION_MODE else "Simulation engine"
    print(f"\nPre-market complete — trading begins at {config.MORNING_START} ET")
    print(f"Execution: {mode_label}\n")


def trading_loop():
    global market_regime

    if not session_active:
        return
    if config.SIMULATION_MODE and sim is None:
        return
    if not config.SIMULATION_MODE and risk_manager is None:
        return

    if not strategy.in_trading_window():
        return
    if strategy.past_friday_cutoff():
        return

    # Daily limit checks
    if config.SIMULATION_MODE:
        if config.ACCOUNT_PLAN == "RAPID":
            floor_remaining = sim.remaining_drawdown_buffer()
            if floor_remaining <= 0:
                print(f"SIM: Drawdown floor breached — stopping for today")
                return
            if sim.soft_stop_triggered():
                return
        else:
            cap_room = sim.consistency_cap_remaining()
            if cap_room <= 0:
                print(f"SIM: Flex consistency cap reached for today — stopping new trades")
                return
    else:
        can_trade, reason = risk_manager.check_daily_limits()
        if not can_trade:
            print(f"Paused: {reason}")
            return
        if risk_manager.approaching_daily_limit():
            print("WARNING: Approaching daily loss limit")

    restriction = cal.get_current_restriction()
    if restriction["restricted"] and restriction["action"] == "FULL_STOP":
        print(f"News blackout: {restriction['event']} until {restriction['until']}")
        return

    news_factor = restriction["risk_factor"]

    next_event = cal.next_event_info()
    if next_event and next_event["mins_away"] <= 5:
        print(f"Event in {next_event['mins_away']} min: {next_event['title']} — standing by")
        return

    now_et   = datetime.now(ET)
    time_str = now_et.strftime("%H:%M")

    or_end = f"{config.MORNING_START.split(':')[0]}:{int(config.MORNING_START.split(':')[1]) + config.OPENING_RANGE_MINUTES:02d}"
    if config.MORNING_START <= time_str < or_end:
        return
    if gap_detected and time_str < "09:45":
        print("Gap detected — waiting before trading")
        return

    # Scan instruments using free market data
    signals = {}
    for symbol in config.INSTRUMENTS:
        try:
            df_5min  = marketdata.get_bars(symbol, "5min",  50)
            df_15min = marketdata.get_bars(symbol, "15min", 50)
            if df_5min is None or df_15min is None:
                continue
            signal = strategy.generate_signal(df_5min, df_15min, symbol, prev_high, prev_low)
            signals[symbol] = signal
        except Exception as e:
            print(f"Warning: {symbol} signal error: {e}")
            signals[symbol] = None

    valid_signals = {k: v for k, v in signals.items() if v is not None}
    if not valid_signals:
        return

    if config.USE_CORRELATION and not strategy.signals_agree(valid_signals):
        print("MNQ/MES disagree — skipping both")
        return

    is_mon  = strategy.is_monday()
    is_opex = strategy.is_opex_friday()

    for symbol, signal in valid_signals.items():
        if signal is None:
            continue

        if not sentiment.sentiment_allows_trade(symbol, signal["direction"]):
            print(f"Sentiment blocked {symbol} {signal['direction']}")
            continue

        # Position sizing
        if config.SIMULATION_MODE:
            contracts = _sim_position_size(symbol, signal, market_regime, news_factor, is_mon, is_opex)
        else:
            can_trade, reason = risk_manager.check_daily_limits()
            if not can_trade:
                print(f"Limit hit: {reason}")
                break
            contracts = risk_manager.calculate_position_size(
                symbol=symbol, risk_per_point=signal["risk"], regime=market_regime,
                news_factor=news_factor, is_monday=is_mon, is_opex=is_opex
            )

        if contracts < 1:
            continue

        logger.print_trade_signal(signal, contracts)

        if config.SIMULATION_MODE:
            # Simulate the trade with realistic costs, exit at target1 for now.
            # Pass target2 as the peak estimate — price plausibly traded through
            # there before settling at target1, which is what actually drives
            # Rapid's true intraday HWM/floor, not just the realized exit.
            sim.record_trade(
                symbol      = symbol,
                direction   = signal["direction"],
                signal_entry= signal["entry"],
                exit_price  = signal["target1"],
                quantity    = contracts,
                exit_reason = "target",
                peak_price  = signal["target2"]
            )
        else:
            action  = "buy" if signal["direction"] == "Buy" else "sell"
            # Convert point-distance risk to DOLLAR SL/TP for PMT.
            # signal["risk"] = points from entry to stop (per strategy.py).
            _pv = config.CONTRACT_SPECS.get(symbol, 2.0)
            _dollar_sl = round(signal["risk"] * _pv * contracts, 2)
            _dollar_tp = round(signal["risk"] * config.PROFIT_TARGET_1 * _pv * contracts, 2)
            success = pmt.send_trade(
                symbol=symbol, action=action, quantity=contracts,
                dollar_sl=_dollar_sl, dollar_tp=_dollar_tp
            )
            print(f"Signal {'sent' if success else 'FAILED'} to PickMyTrade for {symbol}")
            logger.print_status(risk_manager, market_regime, restriction)


def _sim_position_size(symbol, signal, regime, news_factor, is_monday, is_opex):
    """Position sizing for simulation mode — branches on Flex vs Rapid risk rules"""
    if config.ACCOUNT_PLAN == "RAPID":
        risk_pct = config.RAPID_RISK_PER_TRADE   # 0.6% — tighter for intraday peak floor
    else:
        risk_pct = config.BASE_RISK              # 1% — Flex's EOD drawdown is more forgiving

    if regime == "RANGING":
        risk_pct *= 0.5
    if is_monday:
        risk_pct *= config.MONDAY_RISK_FACTOR
    if is_opex:
        risk_pct *= config.OPEX_RISK_FACTOR
    risk_pct *= news_factor

    risk_amount = sim.state["balance"] * risk_pct

    if config.ACCOUNT_PLAN == "RAPID":
        # Respect remaining intraday drawdown room
        dd_room     = sim.remaining_drawdown_buffer()
        risk_amount = min(risk_amount, dd_room * config.RAPID_DD_ROOM_FRACTION)
        max_contracts = config.RAPID_MAX_CONTRACTS
    else:
        # Flex: respect remaining consistency cap room instead.
        #
        # IMPORTANT: at BASE_RISK (1%), the risk-based contract math
        # alone wants ~20+ contracts on a typical setup — it always
        # overshoots the confirmed 2-contract account ceiling well
        # before the consistency cap is even relevant. That means the
        # contract count is effectively decided by max_position_contracts,
        # not by risk_amount, UNLESS the cap has shrunk so far that even
        # 1 contract's worth of risk doesn't fit — in which case we
        # genuinely should reduce size or skip, not force 2 anyway.
        cap_room      = sim.consistency_cap_remaining()
        max_contracts = simulator.RULES.get("max_position_contracts", 2)

        if cap_room < float("inf"):
            one_contract_risk = signal["risk"] * config.CONTRACT_SPECS.get(symbol, 2.0)
            if cap_room >= one_contract_risk * max_contracts:
                # Full room available — trade the real account max,
                # don't let the risk-based formula understate it
                contracts = max_contracts
                print(f"   SIM position size: {contracts} {symbol} "
                      f"(full cap room, plan {config.ACCOUNT_PLAN})")
                return contracts
            risk_amount = min(risk_amount, cap_room)
        # else: no cap constraint at all (eval passed) — fall through
        # to normal risk-based sizing below, capped at max_contracts

    point_value = config.CONTRACT_SPECS.get(symbol, 2.0)
    contracts   = max(1, int(risk_amount / (signal["risk"] * point_value)))
    contracts   = min(contracts, max_contracts)

    print(f"   SIM position size: {contracts} {symbol} (risk ${risk_amount:.2f}, plan {config.ACCOUNT_PLAN})")
    return contracts


def end_of_day():
    global session_active

    if not session_active:
        return
    if config.SIMULATION_MODE and sim is None:
        return
    if not config.SIMULATION_MODE and risk_manager is None:
        return

    print(f"\n{'='*50}")
    print("  END OF DAY")
    print(f"{'='*50}")

    if not config.SIMULATION_MODE:
        pmt.close_all()

    session_active = False
    time.sleep(5)

    if config.SIMULATION_MODE:
        summary = sim.end_of_day()
    else:
        summary = risk_manager.end_of_day()
        for trade in summary.get("trade_log", []):
            logger.log_trade(trade, daily_pnl=summary["daily_pnl"], win_rate=summary["win_rate"])

    print(f"\nDAILY SUMMARY")
    print(f"{'─'*40}")
    print(f"  Trades:   {summary['trades']}")
    print(f"  Day P&L:  ${summary['daily_pnl']:+.2f}")
    print(f"  Balance:  ${summary['balance']:,.2f}")
    print(f"{'─'*40}\n")


def send_report():
    if config.SIMULATION_MODE:
        if sim is None:
            return
        summary = sim.end_of_day()
        notify.send_simulation_report(summary)
    else:
        if risk_manager is None:
            return
        summary = risk_manager.end_of_day()
        notify.send_daily_report(summary)


def health_check():
    pass  # marketdata module is stateless, no reconnect needed


def setup_schedule():
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday"]
    for day in weekdays:
        getattr(schedule.every(), day).at("06:00").do(pre_market_setup)
        getattr(schedule.every(), day).at("15:45").do(end_of_day)
        getattr(schedule.every(), day).at("16:05").do(send_report)

    schedule.every(60).seconds.do(trading_loop)
    schedule.every(5).minutes.do(health_check)

    print("Schedule configured:")
    print("  Pre-market:  06:00 ET weekdays")
    print("  Trading:     Every 60 seconds")
    print("  EOD close:   15:45 ET weekdays")
    print("  Email:       16:05 ET weekdays\n")


def main():
    mode = "SIMULATION" if config.SIMULATION_MODE else "LIVE"
    print("\n" + "="*50)
    print(f"  MICRO FUTURES TRADING BOT — {mode}")
    print(f"  MNQ + MES  |  {config.ACCOUNT_PLAN} $25K rules")
    print("="*50 + "\n")

    missing = []
    if not config.GMAIL_ADDRESS:       missing.append("GMAIL_ADDRESS")
    if not config.GMAIL_APP_PASSWORD:  missing.append("GMAIL_APP_PASSWORD")
    if not config.SIMULATION_MODE and not config.PICKMYTRADE_WEBHOOK:
        missing.append("PICKMYTRADE_WEBHOOK")

    if missing:
        print("ERROR: Missing .env credentials:")
        for m in missing:
            print(f"   - {m}")
        return

    print("All required credentials loaded")
    print(f"Market data source: {marketdata.data_source_status()}")
    if config.FINNHUB_API_KEY:
        print("Finnhub fallback: enabled")
    if config.ALPHA_VANTAGE_API_KEY:
        print("Alpha Vantage fallback: enabled")

    now_et   = datetime.now(ET)
    time_str = now_et.strftime("%H:%M")
    is_week  = now_et.weekday() < 5

    if is_week and "06:00" <= time_str <= "16:00":
        print("Started during market hours — running setup now...")
        pre_market_setup()

    setup_schedule()
    print("Bot running — press Ctrl+C to stop\n")

    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except KeyboardInterrupt:
            print("\nBot stopped by user")
            if session_active and not config.SIMULATION_MODE:
                print("Closing positions via PickMyTrade...")
                pmt.close_all()
            print("Goodbye\n")
            break
        except Exception as e:
            print(f"Unexpected error: {e}")
            print("Restarting in 30 seconds...")
            time.sleep(30)


if __name__ == "__main__":
    main()
