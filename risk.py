"""
risk.py — Risk management and MFFU rule enforcement
All position sizing, daily limits, and consistency tracking
"""

from datetime import datetime, date
import json
import os
import config

# ─────────────────────────────────────────
# STATE FILE — persists across sessions
# ─────────────────────────────────────────
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")


def _load_state():
    """Load persistent state from disk"""
    default = {
        "total_profit":       0.0,
        "qualifying_days":    0,
        "qualifying_dates":   [],
        "consecutive_wins":   0,
        "consecutive_losses": 0,
        "daily_pnl_history":  {},
    }
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return {**default, **json.load(f)}
    except Exception:
        pass
    return default


def _save_state(state):
    """Save persistent state to disk"""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"⚠️ Could not save state: {e}")


# ─────────────────────────────────────────
# DAILY SESSION TRACKER
# ─────────────────────────────────────────
class RiskManager:
    def __init__(self, starting_balance):
        self.starting_balance    = starting_balance
        self.current_balance     = starting_balance
        self.daily_pnl           = 0.0
        self.trades_today        = 0
        self.wins_today          = 0
        self.losses_today        = 0
        self.trade_log           = []
        self.daily_loss_hit      = False
        self.profit_lock_hit     = False
        self.session_date        = date.today().isoformat()

        # Load persistent state
        self.state               = _load_state()
        self.consecutive_wins    = self.state["consecutive_wins"]
        self.consecutive_losses  = self.state["consecutive_losses"]
        self.total_profit        = self.state["total_profit"]
        self.qualifying_days     = self.state["qualifying_days"]
        self.qualifying_dates    = self.state["qualifying_dates"]

        print(f"📊 Risk Manager initialized")
        print(f"   Balance:          ${starting_balance:,.2f}")
        print(f"   Total profit:     ${self.total_profit:,.2f}")
        print(f"   Qualifying days:  {self.qualifying_days} / {config.MIN_PAYOUT_DAYS}")
        print(f"   Daily loss limit: ${starting_balance * config.DAILY_LOSS_LIMIT:,.2f}")


    # ─────────────────────────────────────
    # POSITION SIZING
    # ─────────────────────────────────────
    def calculate_position_size(self, symbol, risk_per_point, regime="TRENDING",
                                 news_factor=1.0, is_monday=False, is_opex=False):
        """
        Calculate how many contracts to trade
        Accounts for regime, news events, day-of-week, adaptive sizing
        """
        # Base risk amount in dollars
        risk_pct    = self._adaptive_risk()
        risk_amount = self.current_balance * risk_pct

        # Apply modifiers
        if regime == "RANGING":
            risk_amount *= 0.5
            print(f"   ⚠️ Ranging market — risk halved")
        if is_monday:
            risk_amount *= config.MONDAY_RISK_FACTOR
        if is_opex:
            risk_amount *= config.OPEX_RISK_FACTOR
        risk_amount *= news_factor

        # Apply consistency rule check
        risk_amount = self._apply_consistency_cap(risk_amount)

        # Calculate contracts
        point_value = config.CONTRACT_SPECS.get(symbol, 2.0)
        contracts   = max(1, int(risk_amount / (risk_per_point * point_value)))

        print(f"   📐 Position size: {contracts} {symbol} contract(s)")
        print(f"      Risk: ${risk_amount:.2f} at {risk_pct*100:.1f}%")
        return contracts


    def _adaptive_risk(self):
        """Adjust risk based on recent performance"""
        base = config.BASE_RISK

        if self.consecutive_losses >= config.ADAPTIVE_LOSS_THRESHOLD:
            adjusted = base * config.ADAPTIVE_LOSS_FACTOR
            print(f"   📉 {self.consecutive_losses} losses — reducing risk to {adjusted*100:.1f}%")
            return max(config.MIN_RISK, adjusted)

        if self.consecutive_wins >= config.ADAPTIVE_WIN_THRESHOLD:
            adjusted = base * config.ADAPTIVE_WIN_FACTOR
            print(f"   📈 {self.consecutive_wins} wins — increasing risk to {adjusted*100:.1f}%")
            return min(config.MAX_RISK, adjusted)

        return base


    def _apply_consistency_cap(self, risk_amount):
        """
        MFFU's actual consistency rule is a LOOK-BACK calculation checked
        at pass-time: best single day's profit / total cumulative profit
        must be <= 50% when you request to pass. It does NOT block trades
        mid-day, and a breach doesn't fail the account — it just means you
        keep trading until later days bring the ratio back under 50%
        (excess profit isn't lost, it carries forward).

        Previous version recalculated a live "max_today" ceiling off
        same-day total_profit and hard-blocked new trades the instant
        daily_pnl crossed it. On day 1, total_profit starts at $0, so that
        ceiling was ~$0 — throttling the very first day to almost nothing
        for no rule-based reason. That doesn't match how MFFU actually
        evaluates the rule, so it's removed here. We still track the ratio
        (see check_consistency_ratio) so you can see where you stand
        before requesting a pass.
        """
        return risk_amount


    # ─────────────────────────────────────
    # TRADE RECORDING
    # ─────────────────────────────────────
    def record_trade(self, symbol, direction, entry, exit_price, quantity, pnl):
        """Record a completed trade"""
        self.daily_pnl        += pnl
        self.current_balance  += pnl
        self.trades_today     += 1

        if pnl > 0:
            self.wins_today          += 1
            self.consecutive_wins    += 1
            self.consecutive_losses   = 0
        else:
            self.losses_today        += 1
            self.consecutive_losses  += 1
            self.consecutive_wins     = 0

        trade = {
            "date":      datetime.now().strftime("%Y-%m-%d"),
            "time":      datetime.now().strftime("%H:%M:%S"),
            "symbol":    symbol,
            "direction": direction,
            "entry":     entry,
            "exit":      exit_price,
            "quantity":  quantity,
            "pnl":       round(pnl, 2),
            "balance":   round(self.current_balance, 2),
        }
        self.trade_log.append(trade)

        emoji = "✅" if pnl > 0 else "❌"
        print(f"   {emoji} Trade closed: {direction} {quantity} {symbol}")
        print(f"      Entry: {entry} | Exit: {exit_price} | P&L: ${pnl:+.2f}")
        print(f"      Daily P&L: ${self.daily_pnl:+.2f}")

        return trade


    # ─────────────────────────────────────
    # CIRCUIT BREAKERS
    # ─────────────────────────────────────
    def check_daily_limits(self):
        """
        Check all circuit breakers
        Returns (can_trade, reason)
        """
        # Max trades
        if self.trades_today >= config.MAX_TRADES_PER_DAY:
            return False, f"Max trades reached ({config.MAX_TRADES_PER_DAY})"

        # Daily loss limit
        loss_limit = self.starting_balance * config.DAILY_LOSS_LIMIT
        if self.daily_pnl <= -loss_limit:
            self.daily_loss_hit = True
            return False, f"Daily loss limit hit (${loss_limit:,.2f})"

        # Profit lock
        profit_lock = self.starting_balance * config.PROFIT_LOCK_PCT
        if self.daily_pnl >= profit_lock:
            self.profit_lock_hit = True
            return False, f"Profit lock triggered (+${profit_lock:,.2f}) — protecting gains"

        # Consecutive loss stop
        if self.consecutive_losses >= config.CONSECUTIVE_LOSS_STOP:
            return False, f"Consecutive loss stop ({config.CONSECUTIVE_LOSS_STOP} in a row)"

        # NOTE: Consistency rule is NOT a daily trade-blocker — see
        # check_consistency_ratio() for the real look-back check used
        # at pass-time.

        return True, "OK"


    def approaching_daily_limit(self):
        """Warn when approaching 80% of daily loss limit"""
        loss_limit = self.starting_balance * config.DAILY_LOSS_LIMIT
        return self.daily_pnl <= -(loss_limit * 0.8)


    def check_consistency_ratio(self):
        """
        MFFU's real consistency check: best single day's profit divided by
        total cumulative profit, evaluated as a look-back at pass-time
        (not a mid-day blocker). MFFU also applies a small leniency buffer
        — roughly 0.2% of account size — so near-50% isn't an automatic
        flag. Returns (ratio, best_day, is_compliant) for display/logging.
        """
        history = self.state.get("daily_pnl_history", {}).copy()
        history[date.today().isoformat()] = self.daily_pnl  # include today

        winning_days = {d: p for d, p in history.items() if p > 0}
        if not winning_days:
            return 0.0, 0.0, True

        total = sum(winning_days.values())
        best_day = max(winning_days.values())
        leniency = self.starting_balance * 0.002  # ~$50 on a 25K account

        if total <= 0:
            return 0.0, best_day, True

        ratio = best_day / total
        is_compliant = (best_day - leniency) <= (total * config.CONSISTENCY_CAP)

        return ratio, best_day, is_compliant


    # ─────────────────────────────────────
    # END OF DAY
    # ─────────────────────────────────────
    def end_of_day(self):
        """
        Process end of day — update qualifying days,
        save persistent state, return summary
        """
        today     = date.today().isoformat()
        is_qualif = self.daily_pnl >= config.MIN_QUALIFYING_PROFIT

        if is_qualif and today not in self.qualifying_dates:
            self.qualifying_days   += 1
            self.qualifying_dates.append(today)
            print(f"🎯 Qualifying day #{self.qualifying_days} recorded!")

        self.total_profit += self.daily_pnl

        # Save state
        self.state.update({
            "total_profit":       self.total_profit,
            "qualifying_days":    self.qualifying_days,
            "qualifying_dates":   self.qualifying_dates,
            "consecutive_wins":   self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "daily_pnl_history":  {
                **self.state.get("daily_pnl_history", {}),
                today: round(self.daily_pnl, 2)
            }
        })
        _save_state(self.state)

        # Payout status
        payout_available    = max(0, self.total_profit * config.MAX_PAYOUT_PCT)
        days_to_payout      = max(0, config.MIN_PAYOUT_DAYS - self.qualifying_days)
        payout_eligible     = (
            self.qualifying_days >= config.MIN_PAYOUT_DAYS and
            payout_available >= config.MIN_PAYOUT_AMOUNT
        )

        # Consistency ratio — informational only, not a trade blocker.
        # Computed AFTER total_profit is updated above so today's day is
        # reflected in both the best-day and total-profit sides of the ratio.
        consistency_ratio, consistency_best_day, consistency_compliant = self.check_consistency_ratio()
        _status = ("compliant" if consistency_compliant
                   else "OVER 50% — pass target moved")
        print(f"   📏 Consistency: {consistency_ratio*100:.1f}% "
              f"(best day ${consistency_best_day:,.2f}) — {_status}")
        _needed = max(1500.0, 2.0 * consistency_best_day)
        print(f"   🎯 Profit needed to pass eval: ${_needed:,.2f} "
              f"(${max(0.0, _needed - self.total_profit):,.2f} to go)")
        if not consistency_compliant:
            print(f"   ⚠️ Consistency ratio {consistency_ratio*100:.1f}% is over 50% "
                  f"(best day ${consistency_best_day:.2f}) — keep trading, this resolves "
                  f"as total profit grows, nothing is blocked or lost")

        summary = {
            "date":              today,
            "daily_pnl":         round(self.daily_pnl, 2),
            "trades":            self.trades_today,
            "wins":              self.wins_today,
            "losses":            self.losses_today,
            "win_rate":          round(self.wins_today / max(1, self.trades_today) * 100, 1),
            "balance":           round(self.current_balance, 2),
            "total_profit":      round(self.total_profit, 2),
            "qualifying_days":   self.qualifying_days,
            "days_to_payout":    days_to_payout,
            "payout_available":  round(payout_available, 2),
            "payout_eligible":   payout_eligible,
            "is_qualifying_day": is_qualif,
            "consistency_ratio":     round(consistency_ratio * 100, 1),
            "consistency_best_day":  round(consistency_best_day, 2),
            "consistency_compliant": consistency_compliant,
            "trade_log":         self.trade_log,
        }

        return summary
