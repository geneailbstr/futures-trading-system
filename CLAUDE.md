# CLAUDE.md

## What this is

An automated algorithmic futures trading bot for MNQ and MES micro futures.
Signals are generated locally in Python, then routed to Tradovate via
PickMyTrade webhooks. Built to run an evaluation account under MyFundedFutures
(MFFU) Flex rules. MFFU discontinued Flex on 5 Aug 2026; new accounts are
Builder 25K, and the rule migration is pending (see CHANGELOG.md).

This is a public portfolio repository. Assume anything committed here is read
by a hiring manager.

## Stack

- Python 3.11+, no framework — plain modules, run from the command line
- Market data: tastytrade / DXLink websocket feed (free tier, requires a
  futures-enabled margin account)
- Order routing: PickMyTrade webhook -> Tradovate
- State: local JSON files (`sim_state.json`), no database

## Module map

| File | Responsibility |
|---|---|
| `bot.py` | Entry point and main loop |
| `config.py` | All tunables and toggles. Nothing hardcoded elsewhere. |
| `strategy.py` | Signal generation — EMA cross, RSI, VWAP, ATR, volume filter |
| `risk.py` | Position sizing, stops, MFFU rule enforcement |
| `marketdata.py` | Candle assembly, resampling, forward-fill |
| `tt_marketdata.py` | tastytrade OAuth + DXLink live feed |
| `simulator.py` | Backtest / observe-mode execution |
| `pmt.py` | PickMyTrade payload construction and send |
| `safety.py` | Live-entry gate: kill switch, feed freshness/source, fill-blind stop-risk ledger, instance lock |
| `pmt_bracket_test.py` | Guided Tradovate DEMO test for `dollar_sl` units and `close` behavior |
| `ecocal.py` | Economic calendar blackout windows |
| `sentiment.py` | Sentiment inputs |
| `notify.py` | Alerts |
| `logger.py` | Structured logging |
| `reset_for_live.py` | Monday morning state archiving |

## Strategy rules (do not change without being asked)

- Timeframes: 5-minute and 15-minute
- Entry: EMA crossover confirmed by RSI and VWAP position
- Stops: ATR-based
- Volume filter uses a **rolling median**, multiplier `0.7x` (loosened from an
  initial `1.0x` — see `config.py`). Do not revert to a mean — spike bars
  inflate it.
- `PROFIT_LOCK_PCT = 0.025`, sized to stay under the $750 daily consistency
  ceiling. Changing this changes eval compliance.
- The MFFU consistency rule is a **look-back ratio check at pass-time**, gated
  on the `eval_passed` flag. It is not a live trade blocker. Do not
  "fix" it into one.
- Profit split is 80/20.

## Data feed rules

- DXLink handshake order is fixed:
  `SETUP -> AUTH_STATE AUTHORIZED -> CHANNEL_REQUEST -> CHANNEL_OPENED ->
  FEED_SETUP -> FEED_SUBSCRIPTION`
- tastytrade OAuth bypasses the SDK deliberately and uses direct `httpx`
  calls. Do not "simplify" this back to the SDK.
- Tokens are cached with a 12-minute TTL.
- Front-month contract resolves automatically.
- In-progress live candles produce NaNs — these are filtered, not
  forward-filled. Resampled historical gaps ARE forward-filled. Keep these two
  behaviors separate.
- `USE_REAL_DATA` in `config.py` is the single switch between live and proxy
  data.

## PickMyTrade payload (schema is real, not guessed)

- `action` field value is `"data"`
- Stops use `dollar_sl` / `dollar_tp`
- Token appears both top-level and inside `multiple_accounts[]`
- `order_type` is `"MKT"`
- `quantity_multiplier` is locked at `1`

Any change to this payload must be verified against a real PickMyTrade
response, not assumed.

- `dollar_sl` UNITS ARE UNVERIFIED — PMT's docs contradict each other (USD per
  contract vs. price points). `bot.py` sends points × point value × contracts.
  `SAFETY_SL_MODE` stays `"unverified"` until `pmt_bracket_test.py` has been run
  on a demo account; only change it from those results.

## Safety gate rules

- `safety.py` gates NEW live entries only. Never gate exits, the 15:45
  force-close, or the kill-switch flatten.
- Don't bypass or loosen the gate to get a trade through. If it blocks, the
  reason is logged; fix the cause.
- Proxy data (yfinance/Finnhub/Alpha Vantage) is observe-only. Keep
  `SAFETY_ALLOWED_SOURCES = ["dxlink"]`.
- The ledger is deliberately pessimistic (every stop assumed hit) because the
  bot can't see fills. Don't "optimize" it.
- `safety.py` and `pmt_bracket_test.py` use the standard library only.

## Safety rules for you (Claude)

1. **Never commit secrets.** No API tokens, OAuth credentials, PickMyTrade
   tokens, or account numbers in tracked files. If you find one, stop and tell
   me before doing anything else.
2. **Never place a live order** or remove the observe-mode send block to "test"
   something.
3. Real money is downstream of this code. When touching `risk.py`, `pmt.py`,
   or anything gated on `eval_passed`, explain the change before making it.
4. Don't refactor working modules for style. Fix what I asked about.

## Don'ts

- No new dependencies without asking. The lean dependency list is intentional.
- No async rewrite of the main loop.
- No pandas-heavy vectorization of `strategy.py` — it runs on live streaming
  bars, not a static frame.
- Don't invent backtest results. If you don't have data, say so.

## What good work looks like here

- Tests use recorded candle fixtures, not live network calls
- Every strategy change is accompanied by an explanation of what it does to
  drawdown, not just win rate
- Commit messages say what changed and why
