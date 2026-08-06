"""
config.py — All bot settings in one place
Change values here to adjust bot behavior
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
# ACCOUNT
# ─────────────────────────────────────────
ACCOUNT_SIZE         = 25000
ACCOUNT_ID           = os.getenv("TRADOVATE_ACCOUNT_ID")
TRADOVATE_USERNAME   = os.getenv("TRADOVATE_USERNAME")
TRADOVATE_PASSWORD   = os.getenv("TRADOVATE_PASSWORD")
TRADOVATE_ENV        = os.getenv("TRADOVATE_ENV", "demo")  # demo or live
TRADOVATE_CID        = int(os.getenv("TRADOVATE_CID") or 8)  # 8 = Tradovate's public "Sample App" cid
TRADOVATE_SEC        = os.getenv("TRADOVATE_SEC")
TRADOVATE_DEVICE_ID  = os.getenv("TRADOVATE_DEVICE_ID")

# ─────────────────────────────────────────
# INSTRUMENTS
# ─────────────────────────────────────────
INSTRUMENTS = ["MNQ", "MES"]

# Contract specs — value per point
CONTRACT_SPECS = {
    "MNQ": 2.0,   # $2 per point
    "MES": 5.0,   # $5 per point
}

# ─────────────────────────────────────────
# RISK MANAGEMENT
# ─────────────────────────────────────────
BASE_RISK             = 0.01     # 1% base risk per trade
MAX_RISK              = 0.02     # 2% hard ceiling
MIN_RISK              = 0.005    # 0.5% hard floor
MAX_TRADES_PER_DAY    = 6
DAILY_LOSS_LIMIT      = 0.02     # 2% max daily loss
PROFIT_LOCK_PCT       = 0.025    # $625/day on 25K — stays under the $750
                                  # consistency ceiling (50% of $1,500
                                  # target) even if the last trade runs hot
CONSECUTIVE_LOSS_STOP = 4        # stop after 4 losses in a row
SLIPPAGE_BUFFER       = 2        # points — accounts for live slippage
MIN_RR_RATIO          = 1.5      # minimum reward:risk ratio

# Adaptive sizing thresholds
ADAPTIVE_LOSS_THRESHOLD = 3      # reduce size after 3 consecutive losses
ADAPTIVE_WIN_THRESHOLD  = 5      # increase size after 5 consecutive wins
ADAPTIVE_LOSS_FACTOR    = 0.5    # multiply risk by this after losses
ADAPTIVE_WIN_FACTOR     = 1.25   # multiply risk by this after wins

# ─────────────────────────────────────────
# STRATEGY — INDICATORS
# ─────────────────────────────────────────
EMA_FAST              = 9
EMA_SLOW              = 21
RSI_PERIOD            = 14
RSI_LONG_MIN          = 45       # RSI must be above this for longs
RSI_LONG_MAX          = 65       # RSI must be below this for longs
RSI_SHORT_MIN         = 35       # RSI must be above this for shorts
RSI_SHORT_MAX         = 55       # RSI must be below this for shorts
VOLUME_MULTIPLIER     = 0.7       # volume must be >= the 20-period MEDIAN
                                  # (loosened from 1.5x then 1.1x — diagnostic
                                  # logs on 2026-06-24/25/26 showed legitimate
                                  # bars consistently rejected against a
                                  # rolling MEAN that outlier spike bars were
                                  # dragging far above what a typical bar
                                  # looks like. vol_avg is now a rolling
                                  # MEDIAN, which is robust to those spikes,
                                  # so 1.0x against the median is a fair bar:
                                  # "at least typical volume", not a true spike
                                  # requirement anymore. Revisit if this proves
                                  # too loose once real trades start flowing.
ATR_PERIOD            = 14
ATR_STOP_MULTIPLIER   = 1.5      # stop = 1.5x ATR from entry
ADX_PERIOD            = 14
ADX_TREND_THRESHOLD   = 25       # ADX above this = trending market
PROFIT_TARGET_1       = 1.5      # first target at 1.5x risk
PROFIT_TARGET_2       = 2.5      # second target at 2.5x risk

# ─────────────────────────────────────────
# STRATEGY — FILTERS
# ─────────────────────────────────────────
OPENING_RANGE_MINUTES = 15       # wait 15 min after open before trading
GAP_THRESHOLD         = 0.005    # 0.5% gap = wait for fill
ROUND_NUMBER_BUFFER   = 10       # avoid entries within 10pts of round numbers
PREV_DAY_BUFFER       = 5        # avoid entries within 5pts of prev high/low
TIMEFRAMES            = ["5min", "15min"]  # both must agree

# ─────────────────────────────────────────
# TRADING HOURS (Eastern Time)
# ─────────────────────────────────────────
MORNING_START         = "09:30"
MORNING_END           = "11:00"
AFTERNOON_START       = "13:30"
AFTERNOON_END         = "15:45"
FORCE_CLOSE_TIME      = "15:45"
PRE_MARKET_WAKE       = "06:00"
REPORT_TIME           = "16:05"

# Reduced size sessions
MONDAY_RISK_FACTOR    = 0.75     # 75% normal size on Mondays
FRIDAY_CUTOFF         = "14:00"  # no new trades after 2pm Friday
OPEX_RISK_FACTOR      = 0.75     # 75% size on options expiration Fridays

# ─────────────────────────────────────────
# MFFU FLEX RULES
# ─────────────────────────────────────────
CONSISTENCY_CAP       = 0.50     # no single day > 50% of total profit
MIN_QUALIFYING_PROFIT = 100.0    # $100 minimum for qualifying day
MIN_PAYOUT_DAYS       = 5        # 5 qualifying days before payout
MIN_PAYOUT_AMOUNT     = 250.0    # minimum payout request
MAX_PAYOUT_PCT        = 0.50     # can only withdraw 50% of profit
OVERNIGHT_HOLDS       = False    # never hold overnight

# ─────────────────────────────────────────
# ECONOMIC CALENDAR
# ─────────────────────────────────────────
TIER1_EVENTS = [
    "FOMC", "Federal Funds Rate", "CPI", "Consumer Price Index",
    "NFP", "Non-Farm Payroll", "GDP", "Gross Domestic Product",
    "Fed Chair", "Powell"
]
TIER1_PRE_MINUTES     = 2
TIER1_POST_MINUTES    = 10

TIER2_EVENTS = [
    "Jobless Claims", "Initial Claims", "PPI", "Producer Price",
    "Retail Sales", "ISM", "JOLTS", "ADP Employment"
]
TIER2_PRE_MINUTES     = 2
TIER2_POST_MINUTES    = 5
TIER2_RISK_FACTOR     = 0.5      # half size during tier 2

# ─────────────────────────────────────────
# INTELLIGENCE
# ─────────────────────────────────────────
USE_SENTIMENT         = False    # turn on week 3
USE_REGIME_DETECTION  = True
USE_CORRELATION       = True
USE_SESSION_STATS     = True

# ─────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────
GMAIL_ADDRESS         = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD    = os.getenv("GMAIL_APP_PASSWORD")
NOTIFY_EMAIL          = os.getenv("NOTIFY_EMAIL")

# ─────────────────────────────────────────
# ANTHROPIC (sentiment layer)
# ─────────────────────────────────────────
ANTHROPIC_API_KEY     = os.getenv("ANTHROPIC_API_KEY")

# ─────────────────────────────────────────
# US MARKET HOLIDAYS 2026
# ─────────────────────────────────────────
MARKET_HOLIDAYS = [
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-07-03",  # Independence Day observed
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
]

# ─────────────────────────────────────────
# SIMULATION MODE
# ─────────────────────────────────────────
SIMULATION_MODE = True

# Observe mode: pull REAL tastytrade futures data even while
# SIMULATION_MODE is True. Lets you watch real signals without sending
# orders (PMT sends stay gated by PMT_TEMPLATE_VERIFIED).
# In live mode (SIMULATION_MODE=False) real data is always used
# regardless of this flag.
USE_REAL_DATA = True   # True = paper trading with realistic costs, no real orders
ACCOUNT_PLAN    = "FLEX" # "FLEX" or "RAPID" — determines which rule set the simulator uses

# ─────────────────────────────────────────
# RAPID-SPECIFIC RISK DEFENSE (only used if ACCOUNT_PLAN = "RAPID")
# (intraday trailing drawdown needs tighter risk than Flex's EOD drawdown)
# ─────────────────────────────────────────
RAPID_RISK_PER_TRADE   = 0.006   # 0.6% per trade (vs 1% on Flex)
RAPID_DAILY_SOFT_STOP  = 0.40    # stop trading for the day at 40% of the
                                  # total drawdown allowance ($800 on $2,000)
RAPID_DD_ROOM_FRACTION = 0.5     # never risk more than 50% of remaining DD room
RAPID_MAX_CONTRACTS    = 3       # hard ceiling while account is unproven

# ─────────────────────────────────────────
# MARKET DATA API KEYS (free tiers)
# ─────────────────────────────────────────
DATABENTO_API_KEY     = os.getenv("DATABENTO_API_KEY")    # primary — real futures data
FINNHUB_API_KEY       = os.getenv("FINNHUB_API_KEY")        # fallback — proxy data
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY")  # final fallback — proxy data

# ─────────────────────────────────────────
# PICKMYTRADE
# ─────────────────────────────────────────
PICKMYTRADE_WEBHOOK   = os.getenv("PICKMYTRADE_WEBHOOK")
# Flip to True ONLY after send_trade()/close_position() payloads are
# rebuilt to match the JSON that PickMyTrade's Generate Alert produces
# (dollar-based SL/TP recommended — scale-independent of proxy data)
PMT_TEMPLATE_VERIFIED = True
