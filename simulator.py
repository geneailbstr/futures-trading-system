"""
simulator.py — Realistic paper trading simulation
Models slippage, commissions, and MFFU Flex $25K account rules.
All numbers confirmed from MFFU's own Evaluation/Funded/Payout
Rules screenshots — not estimates.

Key confirmed rules (Flex $25K):
  Eval:   Profit target $1,500 | Max drawdown $1,000 EOD |
          Consistency 50% | Min 2 trading days
  Funded: Max drawdown $1,000 EOD | Consistency gone |
          Max position 2 contracts | 10:1 micro scaling
  Payout: 5 days qualifying at $100+/day | $250 min payout |
          $1,000 max per request | 50% requestable |
          80% profit split (NOT 90%) | $250 net between payouts |
          $100 MLL locks after first payout | Max 5 sim payouts
          then graduates to live funded account
"""

import json
import os
from datetime import datetime, date
import config

STATE_FILE = os.path.join(os.path.dirname(__file__), "sim_state.json")

# ─────────────────────────────────────────
# REALISTIC EXECUTION COSTS
# ─────────────────────────────────────────
SLIPPAGE_TICKS = {
    "MNQ": {"entry": 1, "stop": 2, "target": 0},
    "MES": {"entry": 1, "stop": 2, "target": 0},
}
TICK_SIZE            = {"MNQ": 0.25, "MES": 0.25}
TICK_VALUE           = {"MNQ": 0.50, "MES": 1.25}
COMMISSION_PER_SIDE  = {"MNQ": 0.74, "MES": 0.74}

# ─────────────────────────────────────────
# CONFIRMED MFFU FLEX $25K RULES
# Source: MFFU screenshots reviewed 2026-07-01
# ─────────────────────────────────────────
RAPID_RULES = {
    "starting_balance":           25000,
    "trailing_drawdown":          2000,
    "buffer_required":            1100,
    "min_payout":                 500,
    "daily_profit_cap":           10000,
    "profit_split":               0.90,
    "auto_liq_time":              "16:10",
    "max_micros":                 30,
    "activity_days":              7,
    "consistency_rule":           None,
    "min_qualifying_profit":      None,
    "min_payout_days":            None,
    "drawdown_style":             "intraday_peak",
}

FLEX_RULES = {
    # ── Account basics ──
    "starting_balance":           25000,
    "drawdown_style":             "eod",

    # ── Eval phase (confirmed from Evaluation Rules screenshot) ──
    "eod_max_loss":               1000,     # $1K max drawdown, EOD only
    "eval_profit_target":         1500,     # $1.5K profit target
    "eval_min_trading_days":      2,        # 2 minimum trading days
    "consistency_rule":           0.50,     # 50% cap — eval only, gone when funded
    "trailing_drawdown":          None,     # not trailing — EOD only
    "daily_drawdown":             None,     # none confirmed

    # ── Position limits (same eval and funded) ──
    "max_position_contracts":     2,        # 2 contracts max
    "micro_scaling_ratio":        10,       # 10:1 micro scaling
    "max_micros":                 20,       # 2 x 10 = 20 micros effectively

    # ── Payout rules (confirmed from Payout Rules screenshot) ──
    "days_to_payout":             5,        # 5 qualifying days needed
    "min_qualifying_profit":      100.0,    # $100 minimum per qualifying day
    "min_payout":                 250,      # $250 minimum payout request
    "max_payout_request":         1000,     # $1,000 maximum per payout request
    "net_profit_between_payouts": 250,      # must accumulate $250 net since last payout
    "requestable_pct":            0.50,     # can only request 50% of profit per cycle
    "profit_split":               0.80,     # 80% to trader (NOT 90% — confirmed screenshot)
    "mll_after_first_payout":     100,      # $100 of drawdown locks permanently
                                             # after first payout — effective max
                                             # drawdown becomes $900, not $1,000
    "max_sim_payouts":            5,        # after 5 payouts, graduates to live funded
    "activity_days":              7,        # inactivity rule: must trade once per 7 days

    # ── Other ──
    "news_trading":               True,     # confirmed allowed
    "scaling_rule":               True,     # confirmed present (funded only)
    "buffer":                     None,     # none
    "auto_liq_time":              "16:10",
}

RULES = RAPID_RULES if config.ACCOUNT_PLAN == "RAPID" else FLEX_RULES


def _load_state():
    default = {
        "balance":                  RULES["starting_balance"],
        "high_water_mark":          RULES["starting_balance"],
        "realized_profit":          0.0,
        "profit_since_last_payout": 0.0,
        "buffer_cleared":           True,   # Flex has no buffer
        "buffer_cleared_date":      None,
        "last_trade_date":          None,
        "trades":                   [],
        "daily_pnl_history":        {},
        "qualifying_days":          0,
        "qualifying_dates":         [],
        "payout_eligible":          False,
        "total_paid_out":           0.0,
        "payout_count":             0,      # tracks toward max_sim_payouts (5)
        "drawdown_breached":        False,
        "eval_passed":              False,
        "eval_trading_days":        0,
        "eval_trading_days_list":   [],
        "mll_locked":               False,  # True after first payout
        "effective_max_loss":       FLEX_RULES["eod_max_loss"],  # shrinks after first payout
    }
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return {**default, **json.load(f)}
    except Exception:
        pass
    return default


def _save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        print(f"Warning: could not save sim state: {e}")


class TradeSimulator:
    def __init__(self):
        self.state = _load_state()
        self.plan  = config.ACCOUNT_PLAN
        print(f"\n{'='*50}")
        print(f"  SIMULATION MODE — {self.plan} $25K Rules")
        print(f"{'='*50}")
        print(f"  Balance:              ${self.state['balance']:,.2f}")
        print(f"  Realized profit:      ${self.state['realized_profit']:,.2f}")
        print(f"  Profit split:         {RULES['profit_split']*100:.0f}%")
        if self.plan == "FLEX":
            print(f"  Drawdown style:       EOD (closing balance only)")
            print(f"  Effective max loss:   ${self.state['effective_max_loss']:,.0f}"
                  + (" (MLL locked -$100)" if self.state.get("mll_locked") else ""))
            print(f"  Eval passed:          {self.state['eval_passed']}")
            if not self.state["eval_passed"]:
                print(f"  Consistency cap:      {RULES['consistency_rule']*100:.0f}% (eval phase)")
            print(f"  Qualifying days:      {self.state['qualifying_days']} / {RULES['days_to_payout']}")
            print(f"  Payout count:         {self.state['payout_count']} / {RULES['max_sim_payouts']}")
            print(f"  Profit since payout:  ${self.state['profit_since_last_payout']:,.2f} "
                  f"(need $250 to request next)")
        print(f"  Payout eligible:      {self.state['payout_eligible']}")
        print(f"{'='*50}\n")

        # Recover today's trades on same-day restart — avoids losing
        # all morning's activity if bot is restarted before end_of_day()
        today = datetime.now().strftime("%Y-%m-%d")
        self.trades_today = [t for t in self.state.get("trades", [])
                              if t.get("date") == today]
        self.daily_pnl    = sum(t["net_pnl"] for t in self.trades_today)

        if self.trades_today:
            print(f"  ⚠️  Recovered {len(self.trades_today)} trade(s) from earlier "
                  f"today (${self.daily_pnl:+.2f}) — same-day restart\n")


    def simulate_fill(self, symbol, direction, signal_price):
        ticks  = SLIPPAGE_TICKS.get(symbol, {"entry": 1})["entry"]
        size   = TICK_SIZE.get(symbol, 0.25)
        offset = ticks * size
        return round(signal_price + offset if direction == "Buy"
                     else signal_price - offset, 2)


    def simulate_stop_fill(self, symbol, direction, stop_price):
        ticks  = SLIPPAGE_TICKS.get(symbol, {"stop": 2})["stop"]
        size   = TICK_SIZE.get(symbol, 0.25)
        offset = ticks * size
        return round(stop_price - offset if direction == "Buy"
                     else stop_price + offset, 2)


    def calculate_commission(self, symbol, quantity):
        return round(COMMISSION_PER_SIDE.get(symbol, 0.74) * 2 * quantity, 2)


    def mark_unrealized(self, symbol, direction, entry_fill, current_price, quantity):
        point_value = config.CONTRACT_SPECS.get(symbol, 2.0)
        if direction == "Buy":
            unrealized = (current_price - entry_fill) * point_value * quantity
        else:
            unrealized = (entry_fill - current_price) * point_value * quantity

        if self.plan != "RAPID":
            return unrealized

        unrealized_equity = self.state["balance"] + unrealized
        if unrealized_equity > self.state["high_water_mark"]:
            self.state["high_water_mark"] = unrealized_equity
        return unrealized


    def record_trade(self, symbol, direction, signal_entry, exit_price,
                     quantity, exit_reason="target", peak_price=None):
        fill_entry = self.simulate_fill(symbol, direction, signal_entry)
        fill_exit  = (self.simulate_stop_fill(symbol, direction, exit_price)
                      if exit_reason == "stop" else exit_price)

        check_price = peak_price if peak_price is not None else fill_exit
        self.mark_unrealized(symbol, direction, fill_entry, check_price, quantity)

        point_value = config.CONTRACT_SPECS.get(symbol, 2.0)
        raw_pnl    = ((fill_exit - fill_entry) if direction == "Buy"
                      else (fill_entry - fill_exit)) * point_value * quantity
        commission = self.calculate_commission(symbol, quantity)
        net_pnl    = round(raw_pnl - commission, 2)

        self.state["balance"]                  += net_pnl
        self.state["realized_profit"]          += net_pnl
        self.state["profit_since_last_payout"] += net_pnl
        self.daily_pnl                         += net_pnl

        if self.state["balance"] > self.state["high_water_mark"]:
            self.state["high_water_mark"] = self.state["balance"]

        trade = {
            "date":         datetime.now().strftime("%Y-%m-%d"),
            "time":         datetime.now().strftime("%H:%M:%S"),
            "symbol":       symbol,
            "direction":    direction,
            "signal_price": signal_entry,
            "fill_price":   fill_entry,
            "exit_price":   fill_exit,
            "quantity":     quantity,
            "exit_reason":  exit_reason,
            "commission":   commission,
            "raw_pnl":      round(raw_pnl, 2),
            "net_pnl":      net_pnl,
            "balance":      round(self.state["balance"], 2),
        }

        self.trades_today.append(trade)
        self.state["trades"].append(trade)

        emoji = "✅" if net_pnl > 0 else "❌"
        print(f"   {emoji} SIM TRADE: {direction} {quantity} {symbol}")
        print(f"      Signal: {signal_entry} | Fill: {fill_entry} | Exit: {fill_exit}")
        print(f"      Raw P&L: ${raw_pnl:+.2f} | Commission: -${commission:.2f}")
        print(f"      Net P&L: ${net_pnl:+.2f} | Balance: ${self.state['balance']:,.2f}")

        if self.plan == "RAPID":
            self._check_drawdown()
            self._check_daily_cap()
            self._check_rapid_buffer()
        else:
            self._check_consistency_throttle_warning()
            self._check_max_payout_alert()

        # Save after every trade — prevents mid-session restart data loss
        today = datetime.now().strftime("%Y-%m-%d")
        self.state["daily_pnl_history"][today] = round(self.daily_pnl, 2)
        _save_state(self.state)

        return trade


    # ─────────────────────────────────────
    # PAYOUT ALERTS
    # ─────────────────────────────────────
    def _check_max_payout_alert(self):
        """
        Alert when the maximum $1,000 payout request amount is available.
        Confirmed rules: max per request = $1,000, requestable = 50% of profit,
        must have $250 net since last payout, must be post-eval and 5 qualifying days.
        """
        if not self.state["payout_eligible"]:
            return
        if self.state["payout_count"] >= RULES["max_sim_payouts"]:
            return

        profit_since = self.state["profit_since_last_payout"]
        if profit_since < RULES["net_profit_between_payouts"]:
            return

        requestable    = min(
            self.state["realized_profit"] * RULES["requestable_pct"],
            RULES["max_payout_request"]
        )
        payout_to_you  = round(requestable * RULES["profit_split"], 2)

        if requestable >= RULES["max_payout_request"]:
            print(f"\n   💰 MAX PAYOUT AVAILABLE")
            print(f"      Request:    ${RULES['max_payout_request']:,.2f}")
            print(f"      You receive: ${payout_to_you:,.2f} ({RULES['profit_split']*100:.0f}% split)")
            print(f"      Remaining sim payouts: "
                  f"{RULES['max_sim_payouts'] - self.state['payout_count']} of {RULES['max_sim_payouts']}")
            print(f"      ⚠️  After this payout, MLL locks — max drawdown becomes $900\n")


    def simulate_payout(self, amount=None):
        """
        Simulate requesting a payout — updates all state correctly.
        amount: dollars to request (defaults to maximum allowed)
        """
        if not self.state["payout_eligible"]:
            print("Payout not yet eligible")
            return 0

        if self.state["payout_count"] >= RULES["max_sim_payouts"]:
            print("Max sim payouts reached — account graduates to live funded")
            return 0

        if self.state["profit_since_last_payout"] < RULES["net_profit_between_payouts"]:
            print(f"Need ${RULES['net_profit_between_payouts']:,.2f} net since last payout "
                  f"(have ${self.state['profit_since_last_payout']:,.2f})")
            return 0

        max_requestable = min(
            self.state["realized_profit"] * RULES["requestable_pct"],
            RULES["max_payout_request"]
        )

        if amount is None:
            amount = max_requestable
        amount = min(amount, max_requestable)

        if amount < RULES["min_payout"]:
            print(f"Amount ${amount:.2f} below minimum ${RULES['min_payout']}")
            return 0

        payout_to_you = round(amount * RULES["profit_split"], 2)

        # First payout: lock the MLL ($100 permanent drawdown reduction)
        if self.state["payout_count"] == 0:
            self.state["mll_locked"]       = True
            self.state["effective_max_loss"] = (
                RULES["eod_max_loss"] - RULES["mll_after_first_payout"]
            )
            print(f"\n   🔒 MLL locked after first payout")
            print(f"      Effective max drawdown: "
                  f"${self.state['effective_max_loss']:,.0f} (was $1,000)\n")

        self.state["total_paid_out"]           += payout_to_you
        self.state["payout_count"]             += 1
        self.state["profit_since_last_payout"]  = 0.0

        remaining = RULES["max_sim_payouts"] - self.state["payout_count"]
        print(f"   💸 Payout processed: ${payout_to_you:,.2f} to you")
        print(f"      ({RULES['profit_split']*100:.0f}% of ${amount:,.2f} request)")
        print(f"      Total paid out: ${self.state['total_paid_out']:,.2f}")
        print(f"      Sim payouts remaining: {remaining}")
        if remaining == 0:
            print(f"      🎓 Max sim payouts reached — account graduates to live!")

        _save_state(self.state)
        return payout_to_you


    # ─────────────────────────────────────
    # CIRCUIT BREAKERS
    # ─────────────────────────────────────
    def _check_flex_eod_breach(self):
        """EOD max loss check — uses effective_max_loss which shrinks after first payout"""
        if self.plan != "FLEX":
            return False
        floor = RULES["starting_balance"] - self.state["effective_max_loss"]
        if self.state["balance"] <= floor:
            self.state["drawdown_breached"] = True
            print(f"\n   🚨 FLEX EOD MAX LOSS BREACHED (simulated)")
            print(f"      Balance ${self.state['balance']:,.2f} <= floor ${floor:,.2f}")
            print(f"      Effective max loss: ${self.state['effective_max_loss']:,.0f}"
                  + (" (MLL locked)" if self.state.get("mll_locked") else ""))
            print(f"      On a real Flex account this would END the account.\n")
            return True
        return False

    def _check_drawdown(self):
        if self.plan != "RAPID":
            return
        floor = self.state["high_water_mark"] - RULES["trailing_drawdown"]
        if self.state["balance"] <= floor:
            self.state["drawdown_breached"] = True
            print(f"\n   🚨 DRAWDOWN BREACH SIMULATED — balance ${self.state['balance']:,.2f} "
                  f"<= floor ${floor:,.2f}\n")

    def _check_daily_cap(self):
        if self.plan != "RAPID" or not RULES.get("daily_profit_cap"):
            return
        if self.daily_pnl > RULES["daily_profit_cap"]:
            print(f"\n   ⚠️ RAPID DAILY CAP EXCEEDED (simulated)\n")

    def _check_rapid_buffer(self):
        if self.plan != "RAPID" or self.state["buffer_cleared"]:
            return
        required = RULES["starting_balance"] + RULES["buffer_required"]
        if self.state["balance"] >= required:
            self.state["buffer_cleared"]      = True
            self.state["buffer_cleared_date"] = date.today().isoformat()
            self.state["payout_eligible"]     = True
            print(f"\n   🎯 BUFFER CLEARED (simulated) — payout eligible\n")

    def _check_consistency_throttle_warning(self):
        """Eval phase only — gone once eval_passed"""
        if self.state.get("eval_passed", False):
            return
        if RULES["consistency_rule"] is None or self.state["realized_profit"] <= 0:
            return
        cap = self.state["realized_profit"] * RULES["consistency_rule"]
        if self.daily_pnl > cap * 0.8:
            print(f"\n   ⚠️ Approaching FLEX consistency cap (EVAL phase) "
                  f"(today ${self.daily_pnl:.2f} vs cap ${cap:.2f})\n")


    def check_drawdown_floor(self):
        if self.plan == "RAPID":
            return self.state["high_water_mark"] - RULES["trailing_drawdown"]
        return RULES["starting_balance"] - self.state["effective_max_loss"]

    def remaining_drawdown_buffer(self):
        if self.plan == "RAPID":
            return self.state["balance"] - self.check_drawdown_floor()
        return self.state["balance"] - self.check_drawdown_floor()

    def soft_stop_triggered(self):
        if self.plan != "RAPID":
            return False
        soft_stop = RULES["trailing_drawdown"] * config.RAPID_DAILY_SOFT_STOP
        if self.daily_pnl <= -soft_stop:
            print(f"\n   🛑 SOFT STOP TRIGGERED (simulated) — pausing for today\n")
            return True
        return False

    def consistency_cap_remaining(self):
        if self.state.get("eval_passed", False):
            return float("inf")
        if RULES["consistency_rule"] is None or self.state["realized_profit"] <= 0:
            return float("inf")
        cap = self.state["realized_profit"] * RULES["consistency_rule"]
        return max(0, cap - self.daily_pnl)


    # ─────────────────────────────────────
    # END OF DAY
    # ─────────────────────────────────────
    def end_of_day(self):
        today = date.today().isoformat()
        self.state["last_trade_date"] = today
        self.state["daily_pnl_history"][today] = round(self.daily_pnl, 2)

        self._check_flex_eod_breach()

        if self.plan == "FLEX":
            # Track distinct trading days for eval
            if self.trades_today:
                days_list = self.state.setdefault("eval_trading_days_list", [])
                if today not in days_list:
                    days_list.append(today)
                    self.state["eval_trading_days"] = len(days_list)

            # Eval pass check — $1,500 profit + 2 minimum trading days
            if not self.state["eval_passed"]:
                if (self.state["realized_profit"] >= RULES["eval_profit_target"] and
                        self.state["eval_trading_days"] >= RULES["eval_min_trading_days"]):
                    self.state["eval_passed"] = True
                    print(f"\n   🎓 EVAL PASSED (simulated)")
                    print(f"      Profit ${self.state['realized_profit']:,.2f} >= "
                          f"${RULES['eval_profit_target']:,.2f}")
                    print(f"      Trading days {self.state['eval_trading_days']} >= "
                          f"{RULES['eval_min_trading_days']}")
                    print(f"      Consistency rule now OFF — no longer applies\n")

            is_qualifying = self.daily_pnl >= RULES["min_qualifying_profit"]
            if is_qualifying and today not in self.state.get("qualifying_dates", []):
                self.state["qualifying_days"] += 1
                self.state.setdefault("qualifying_dates", []).append(today)
                print(f"🎯 Qualifying day #{self.state['qualifying_days']} recorded")

            # ── Look-back consistency ratio (eval phase only) ──
            # MFFU checks best single day / total profit <= 50% at pass
            # time. A monster day moves the pass target to 2x that day.
            if not self.state.get("eval_passed", False):
                _hist  = self.state.get("daily_pnl_history", {})
                _wins  = {d: p for d, p in _hist.items() if p and p > 0}
                _total = sum(_wins.values())
                _best  = max(_wins.values()) if _wins else 0.0
                if _total > 0:
                    _ratio  = _best / _total
                    _target = max(RULES["eval_profit_target"], 2.0 * _best)
                    _flag   = ("compliant" if _ratio <= RULES["consistency_rule"]
                               else "OVER 50% — pass target moved")
                    print(f"   📏 Consistency: {_ratio*100:.1f}% "
                          f"(best day ${_best:,.2f}) — {_flag}")
                    print(f"   🎯 Pass target: ${_target:,.2f} "
                          f"(${max(0.0, _target - self.state['realized_profit']):,.2f} to go)")

            # Payout eligibility — needs 5 qualifying days, eval passed,
            # $250 net since last payout, under max sim payouts
            self.state["payout_eligible"] = (
                self.state["eval_passed"] and
                self.state["qualifying_days"] >= RULES["days_to_payout"] and
                self.state["profit_since_last_payout"] >= RULES["net_profit_between_payouts"] and
                self.state["payout_count"] < RULES["max_sim_payouts"]
            )
        else:
            is_qualifying = None

        # Payout math — confirmed: 50% requestable, $1,000 max, 80% split
        payout_available   = 0.0
        payout_to_you      = 0.0
        max_payout_reached = False

        if self.state["payout_eligible"]:
            requestable = min(
                self.state["profit_since_last_payout"] * RULES["requestable_pct"],
                RULES["max_payout_request"]
            )
            payout_available   = max(0, requestable)
            payout_to_you      = round(payout_available * RULES["profit_split"], 2)
            max_payout_reached = requestable >= RULES["max_payout_request"]

        _save_state(self.state)

        return {
            "date":                today,
            "plan":                self.plan,
            "daily_pnl":           round(self.daily_pnl, 2),
            "trades":              len(self.trades_today),
            "wins":                len([t for t in self.trades_today if t["net_pnl"] > 0]),
            "losses":              len([t for t in self.trades_today if t["net_pnl"] <= 0]),
            "balance":             round(self.state["balance"], 2),
            "realized_profit":     round(self.state["realized_profit"], 2),
            "profit_since_payout": round(self.state["profit_since_last_payout"], 2),
            "qualifying_days":     self.state.get("qualifying_days", 0),
            "is_qualifying_day":   is_qualifying,
            "payout_eligible":     self.state["payout_eligible"],
            "payout_available":    round(payout_available, 2),
            "payout_to_you":       payout_to_you,
            "max_payout_reached":  max_payout_reached,
            "payout_count":        self.state["payout_count"],
            "max_sim_payouts":     RULES["max_sim_payouts"],
            "eval_passed":         self.state.get("eval_passed", False),
            "eval_trading_days":   self.state.get("eval_trading_days", 0),
            "eval_profit_target":  RULES.get("eval_profit_target"),
            "mll_locked":          self.state.get("mll_locked", False),
            "effective_max_loss":  self.state.get("effective_max_loss",
                                   FLEX_RULES["eod_max_loss"]),
            "trade_log":           self.trades_today,
        }
