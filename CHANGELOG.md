# Changelog

All notable changes to this project. Dates are when the change landed on `main`.

## [Unreleased]

### Added
- **`safety.py` live-entry gate.** Every new live entry is checked right before
  `pmt.py`, and exits are never gated. Entries are refused when:
  - the kill switch is set
  - the newest closed 5-min bar is stale or delayed
  - the bars came from a proxy source (yfinance / Finnhub / Alpha Vantage), which is now observe-only
  - today's total stop risk would pass the daily loss limit
  - price, stop or target is missing, or size would pass 20 micros
  - the machine isn't `BOT_LIVE_HOST`
- **Kill switch:** `python3 safety.py halt [--flatten] | resume | status`. It works over
  SSH, `bot.py` checks it every 15 s, and a flatten sends an email alert.
- **Single-instance lock**, so two copies of the bot can't run on one machine.
- **Fill-blind risk ledger.** The bot can't see fills on eval accounts, so it assumes
  every stop it sends is hit. The ledger persists to `safety_state.json` across same-day
  restarts.
- **`pmt_bracket_test.py`:** a guided Tradovate demo test that settles how PickMyTrade
  reads `dollar_sl` (USD per contract, USD per order, or price points). It also checks
  whether a `close` cancels brackets, whether it's per symbol, and whether it's safe when
  already flat. It refuses non-`DEMO` accounts.
- **Observe-mode logging:** `SAFETY (observe): live mode would block …`, for validating
  the feed before going live.
- **GitHub Actions CI** on every push and PR: tests on Python 3.12 and 3.13, a dry run of
  the demo test, and `scrub_check.sh` over the full commit history.
- 88 new tests (131 total).

### Changed
- `pmt.py` reads the account ID from `.env` only, and refuses to send if
  `TRADOVATE_ACCOUNT_ID` is unset. It previously fell back to a hardcoded account number.
- `scrub_check.sh` hard-fails on real account-number shapes and flags `os.getenv()`
  defaults containing an account ID. That's the pattern that let the hardcoded number
  above slip through.
- `bot.py`: a failed EOD/shutdown `close_all()` now sends an email alert, and the ledger
  keeps that day's open risk.

### Known issues
- **`dollar_sl` units are unverified.** `bot.py` sends
  `stop points × point value × contracts`, which is correct only if PickMyTrade reads it
  as USD for the whole order. If PMT reads it per contract or as price points, real stops
  are wider than intended. Until `pmt_bracket_test.py` is run, `SAFETY_SL_MODE =
  "unverified"` sizes risk at the worst case. That blocks most 2-contract MES entries.
- **MFFU discontinued the Flex plan on 5 Aug 2026.** The rules in `config.py`,
  `risk.py`, `simulator.py` and `notify.py` still model Flex. Builder 25K is next.
- `schedule` fires at the host machine's local time, so ET session times are only right
  on a machine set to ET. DST ends 1 Nov 2026.

## 2026-08-05
- Rotated a leaked Tradovate secret and stopped hardcoding auth payload fields.
- Added `strategy.py` tests on real recorded MNQ bars.
- Removed dead `tradovate.py` and its orphaned env vars.
- Rewrote the README to match the actual architecture and gating.
- Fixed a stale `VOLUME_MULTIPLIER` comment that contradicted the code.

## 2026-07-28
- Initial public commit.
- Added `RiskManager` unit tests (sizing, circuit breakers).
- Fixed `.env.example` variable names. Added the risk state file to `.gitignore`.
- Removed a false-positive pattern from the secret scanner.
