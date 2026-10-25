"""
pmt_bracket_test.py - settle two PickMyTrade questions on a Tradovate DEMO account.

  Q1  What does dollar_sl mean?  (USD per contract / USD per order / price points)
  Q2  What does a "close" alert do?  (cancels brackets? closes other symbols too?
      harmless when already flat?)

PMT's own docs disagree on Q1 and are silent on parts of Q2, and the bot can't
read Tradovate on eval accounts - so this script sends real webhooks to a DEMO
account and asks YOU to read the result off the Tradovate screen.

Payloads come from pmt._build_payload, so this tests exactly what the bot
sends. Webhook URL and token are read from .env (PICKMYTRADE_WEBHOOK,
PICKMYTRADE_TOKEN), like the bot. The demo account must be given explicitly so
the MFFU account can never be targeted by accident.

Run during Globex hours (Sun 6pm ET - Fri 5pm ET, not 5-6pm ET), ideally RTH:

    export PMT_DEMO_ACCOUNT_ID=DEMO...     # refuses anything not starting with DEMO
    python3 pmt_bracket_test.py --dry-run  # print payloads, send nothing
    python3 pmt_bracket_test.py

Results are saved to pmt_bracket_results.json (token redacted, gitignored).

Every position this opens is 1-2 micros with a stop attached, and the script
closes everything at the end. Still: keep Tradovate open and watch it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pmt
from safety import POINT_VALUE, lint_pmt_payload, symbol_root

RESULTS_FILE = Path(__file__).resolve().parent / "pmt_bracket_results.json"
TEST_SL = 20.0          # dollar_sl sent on every test entry
TICK = 0.25             # MNQ and MES tick size


# --------------------------------------------------------------------------- #
# Pure logic (unit-tested)
# --------------------------------------------------------------------------- #
def expected_distances(dollar_sl: float, qty: int, symbol: str) -> dict[str, float]:
    """Stop distance in index points under each reading of dollar_sl."""
    pv = POINT_VALUE[symbol_root(symbol)]
    return {
        "usd_per_contract": dollar_sl / pv,
        "usd_per_order": dollar_sl / (pv * qty),
        "points": dollar_sl,
    }


def match_mode(observed: float, dollar_sl: float, qty: int, symbol: str) -> list[str]:
    """Readings consistent with an observed stop distance (2-tick or 15% tolerance)."""
    hits = []
    for mode, dist in expected_distances(dollar_sl, qty, symbol).items():
        if abs(observed - dist) <= max(2 * TICK, 0.15 * dist):
            hits.append(mode)
    return hits


def verdict(observations: list[dict]) -> tuple[str | None, list[str]]:
    """
    observations: [{"symbol", "qty", "dollar_sl", "distance"}...]
    Returns (mode or None, notes). Needs every observation to agree.
    """
    notes, candidates = [], None
    for ob in observations:
        hits = set(match_mode(ob["distance"], ob["dollar_sl"], ob["qty"], ob["symbol"]))
        notes.append(f"{ob['symbol']} x{ob['qty']}: stop {ob['distance']:.2f} pts from entry "
                     f"-> consistent with {sorted(hits) or 'NOTHING'}")
        candidates = hits if candidates is None else candidates & hits
    if not candidates:
        return None, notes + ["No single reading fits every test - send these results to PMT support."]
    if len(candidates) > 1:
        return None, notes + [f"Still ambiguous between {sorted(candidates)} - run the 2-contract test."]
    return candidates.pop(), notes


def build_payload(action: str, symbol: str, qty: int, price: float, token: str, account_id: str,
                  dollar_sl: float = 0.0, dollar_tp: float = 0.0) -> dict:
    """The bot's own payload (pmt._build_payload), pointed at the demo account."""
    payload = pmt._build_payload(symbol, action, qty, dollar_sl=dollar_sl,
                                 dollar_tp=dollar_tp, price=price)
    payload["token"] = token
    for acct in payload["multiple_accounts"]:
        acct["token"] = token
        acct["account_id"] = account_id
    return payload


def redact(payload: dict) -> dict:
    out = json.loads(json.dumps(payload))
    if out.get("token"):
        out["token"] = "***"
    for acct in out.get("multiple_accounts") or []:
        if acct.get("token"):
            acct["token"] = "***"
    return out


# --------------------------------------------------------------------------- #
# Interactive runner
# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self, url: str, token: str, account: str, dry_run: bool):
        self.url, self.token, self.account, self.dry = url, token, account, dry_run
        self.log: list[dict] = []
        self.unit_obs: list[dict] = []
        self.close_obs: dict[str, object] = {}

    # ---- io ----
    @staticmethod
    def ask(prompt: str) -> str:
        return input(f"  > {prompt} ").strip()

    def yes(self, prompt: str) -> bool:
        while True:
            a = self.ask(f"{prompt} [y/n]").lower()
            if a in ("y", "n"):
                return a == "y"

    def number(self, prompt: str) -> float:
        while True:
            try:
                return float(self.ask(prompt).replace(",", ""))
            except ValueError:
                print("    (type a number, e.g. 21012.25)")

    def send(self, step: str, payload: dict) -> None:
        problems = lint_pmt_payload(payload)
        if problems:
            raise SystemExit(f"Refusing to send {step}: {problems}")
        print(f"\n  [{step}] {payload['data'].upper()} {payload['quantity']} {payload['symbol']}"
              f"  dollar_sl={payload['dollar_sl']} dollar_tp={payload['dollar_tp']}")
        entry = {"step": step, "payload": redact(payload),
                 "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if self.dry:
            print("    DRY RUN - not sent:\n" + json.dumps(redact(payload), indent=2))
            entry["response"] = "dry-run"
        else:
            if not self.yes("Send it?"):
                raise SystemExit("Stopped by user. Close anything open in Tradovate manually.")
            req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read().decode(errors="replace")[:500]
                    entry["response"] = f"HTTP {resp.status}: {body}"
            except urllib.error.HTTPError as exc:
                entry["response"] = f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}"
            except urllib.error.URLError as exc:
                entry["response"] = f"network error: {exc.reason}"
            print(f"    PMT replied: {entry['response']}")
        self.log.append(entry)

    # ---- tests ----
    def entry_test(self, step: str, symbol: str, qty: int) -> None:
        print(f"\n=== {step}: BUY {qty} {symbol} with dollar_sl={TEST_SL:g}, dollar_tp={2*TEST_SL:g} ===")
        exp = expected_distances(TEST_SL, qty, symbol)
        print("    If PMT reads dollar_sl as...  " +
              " | ".join(f"{m}: stop {d:g} pts away" for m, d in exp.items()))
        price = 1.0 if self.dry else self.number(f"Current {symbol} price shown in Tradovate:")
        self.send(step, build_payload("buy", symbol, qty, price, self.token, self.account,
                                      TEST_SL, 2 * TEST_SL))
        if self.dry:
            return
        print("    In Tradovate: Positions tab (fill price) and Orders tab (working stop + limit).")
        market = self.yes("Did the entry FILL right away (not sitting as a working limit order)?")
        if not market:
            print("    !! PMT treated it as a LIMIT. Cancel it in Tradovate - pmt.py's price handling")
            print("       needs fixing before anything else. Stopping here.")
            self.unit_obs.append({"step": step, "symbol": symbol, "entry_became_limit": True})
            self.report()
            raise SystemExit(1)
        fill = self.number(f"{symbol} average FILL price:")
        stop = self.number(f"{symbol} working STOP order price:")
        target = self.number(f"{symbol} working LIMIT (target) price:")
        oco = self.yes("Are stop and target linked as OCO / bracket (one cancels the other)?")
        ob = {"step": step, "symbol": symbol, "qty": qty, "dollar_sl": TEST_SL, "sent_price": price,
              "fill": fill, "stop": stop, "target": target, "oco": oco,
              "distance": round(fill - stop, 2),
              "distance_from_sent_price": round(price - stop, 2)}
        self.unit_obs.append(ob)
        print(f"    Stop is {ob['distance']:g} pts below fill "
              f"({ob['distance_from_sent_price']:g} below the price sent) -> "
              f"{match_mode(ob['distance'], TEST_SL, qty, symbol) or 'no match'}")

    def close(self, step: str, symbol: str) -> None:
        # Same close the bot sends (pmt.close_position): quantity 0, price 0.
        self.send(step, build_payload("close", symbol, 0, 0, self.token, self.account))

    def run(self, mnq: str, mes: str) -> None:
        print(__doc__.split("Run during")[0])
        print(f"Account: {self.account}   Webhook: {self.url[:40]}...   "
              f"{'(DRY RUN)' if self.dry else ''}")
        if not self.dry and not self.yes("Tradovate DEMO open, account flat, no working orders?"):
            raise SystemExit("Flatten the demo account first, then rerun.")

        # Q1 - units, single contract
        self.entry_test("U1", mnq, 1)

        # Q2a - close cancels brackets?
        print(f"\n=== C1: CLOSE {mnq} while it has a stop + target working ===")
        self.close("C1", mnq)
        if not self.dry:
            self.close_obs["C1_position_flat"] = self.yes(f"{mnq} position now flat?")
            self.close_obs["C1_brackets_cancelled"] = self.yes(
                f"{mnq} stop AND target both gone from working orders?")

        # Q1 - per contract vs per order
        self.entry_test("U2", mnq, 2)
        # Q1 - point-value conversion on a second contract (MNQ still open)
        self.entry_test("U3", mes, 1)

        # Q2b - is close per symbol or account-wide?
        print(f"\n=== C2: CLOSE {mes} only, with {mnq} x2 still open ===")
        self.close("C2", mes)
        if not self.dry:
            self.close_obs["C2_mes_flat"] = self.yes(f"{mes} flat and its stop/target gone?")
            self.close_obs["C2_mnq_untouched"] = self.yes(
                f"{mnq} x2 STILL open with its stop/target still working?")

        print(f"\n=== Closing {mnq} ===")
        self.close("C2b", mnq)
        if not self.dry:
            self.close_obs["C2b_mnq_flat"] = self.yes(f"{mnq} flat, its orders gone?")

        # Q2c - close when already flat
        print(f"\n=== C3: CLOSE {mnq} again while FLAT ===")
        self.close("C3", mnq)
        if not self.dry:
            self.close_obs["C3_no_new_position"] = self.yes("Still flat - no new position opened?")
            self.close_obs["C3_no_new_orders"] = self.yes("No new working orders appeared?")
            self.close_obs["C3_pmt_log"] = self.ask(
                "What does the PMT dashboard log say for this alert? (paste or summarize)")

        # Q2d - orphan brackets after a close PMT didn't send (manual / MFFU liquidation)
        print("\n=== C4 (optional): close a position OUTSIDE PickMyTrade ===")
        if self.dry or self.yes("Run C4? (opens 1 MNQ, you flatten it by hand in Tradovate)"):
            self.entry_test("U4", mnq, 1)
            if not self.dry:
                print("    Now flatten that position MANUALLY in Tradovate (Exit at Mkt / Flatten).")
                self.ask("Press Enter once it's flat...")
                orphans = self.yes("Are the stop and/or target STILL working after the manual close?")
                self.close_obs["C4_orphans_after_manual_close"] = orphans
                if orphans:
                    print("    !! Cancel them by hand now - a leftover leg can open an unplanned position.")
                    self.ask("Press Enter once cancelled...")

        self.report()

    def report(self) -> None:
        obs = [o for o in self.unit_obs if "distance" in o]
        mode, notes = verdict(obs) if obs else (None, ["no stop-distance observations recorded"])
        result = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "account": self.account, "test_dollar_sl": TEST_SL,
                  "sl_mode": mode, "notes": notes, "unit_observations": self.unit_obs,
                  "close_observations": self.close_obs, "log": self.log}
        if not self.dry:
            RESULTS_FILE.write_text(json.dumps(result, indent=2))
        print("\n================ RESULT ================")
        for n in notes:
            print("  " + n)
        print(f"\n  Q1  dollar_sl = {mode or 'UNSETTLED'}")
        if mode:
            print(f"      -> set SAFETY_SL_MODE = \"{mode}\" in config.py")
            print("      -> bot.py sends dollar_sl = stop points x point value x contracts. Under this")
            print(f"         reading that is {BOT_FORMULA_VERDICT[mode]}")
        c = self.close_obs
        if c:
            print("  Q2  close cancels its brackets ....... " + _yn(c.get("C1_brackets_cancelled")))
            print("      close is per-symbol .............. " + _yn(c.get("C2_mnq_untouched")))
            print("      close when flat is harmless ...... " +
                  _yn(c.get("C3_no_new_position") and c.get("C3_no_new_orders")))
            if "C4_orphans_after_manual_close" in c:
                print("      brackets survive a manual close .. " + _yn(c["C4_orphans_after_manual_close"]))
        if not self.dry:
            print(f"\n  Saved: {RESULTS_FILE}")
        print("  Double-check Tradovate is flat with no working orders.")


BOT_FORMULA_VERDICT = {
    "usd_per_order": "CORRECT - no change needed.",
    "usd_per_contract": "WRONG - real stops are `contracts`x too wide. Drop the x contracts.",
    "points": "WRONG - real stops are (point value x contracts)x too wide. "
              "Send stop points instead.",
}


def _yn(v) -> str:
    return {True: "YES", False: "NO", None: "not tested"}.get(v, str(v))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--mnq", default="MNQ", help="MNQ symbol as the bot sends it (default MNQ)")
    ap.add_argument("--mes", default="MES", help="MES symbol as the bot sends it (default MES)")
    ap.add_argument("--dry-run", action="store_true", help="print payloads, send nothing")
    args = ap.parse_args(argv)

    url = os.environ.get("PICKMYTRADE_WEBHOOK", "")   # config.py has already loaded .env
    token = os.environ.get("PICKMYTRADE_TOKEN", "")
    account = os.environ.get("PMT_DEMO_ACCOUNT_ID", "")
    if args.dry_run:
        url, token, account = url or "https://example.invalid/webhook", token or "dry", account or "DEMO0000"
    missing = [n for n, v in (("PICKMYTRADE_WEBHOOK", url), ("PICKMYTRADE_TOKEN", token),
                              ("PMT_DEMO_ACCOUNT_ID", account)) if not v]
    if missing:
        print(f"Set {', '.join(missing)} first (.env, or see top of this file).")
        return 2
    if not account.upper().startswith("DEMO"):
        print(f"Refusing: '{account}' doesn't look like a Tradovate DEMO account (DEMO...). "
              f"Never run this on the MFFU account.")
        return 2
    for sym in (args.mnq, args.mes):
        if symbol_root(sym) not in ("MNQ", "MES"):
            print(f"Refusing: {sym!r} isn't MNQ or MES.")
            return 2
    try:
        Runner(url, token, account, args.dry_run).run(args.mnq, args.mes)
    except (KeyboardInterrupt, EOFError):
        print("\n\nStopped. CHECK TRADOVATE: close any open test position and cancel its orders.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
