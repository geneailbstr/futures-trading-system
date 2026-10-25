"""
safety.py - live-trading guardrails for the MFFU micro futures bot.

Drop-in module, standard library only. Covers four go-live items:

  #4  Stale-data guard ........ FeedGuard   refuse entries on old or delayed bars
  #5  Fallback = observe-only . FeedGuard   only allow-listed sources may trade
  #6  Fill-blind protections .. RiskLedger  worst-case P&L, bracket checks,
                                KillSwitch  halt / flatten from any shell
  #7  Single instance ......... InstanceLock + LIVE_HOST check

Every new entry goes through one call before an order is sent:

    decision = gate.check_entry(intent, live=not config.SIMULATION_MODE)
    if decision:
        pmt.send(...)
    else:
        log(f"OBSERVE (blocked): {decision}")   # still log the would-be trade

Exits, EOD closes and stop management are NEVER gated - only new entries.

Kill switch from any shell (works over SSH from your phone):
    python3 safety.py halt             # block new entries
    python3 safety.py halt --flatten   # block entries AND close everything
    python3 safety.py resume           # clear the halt
    python3 safety.py status
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")

# Dollar value of a 1.00 index-point move, per contract (CME contract specs).
POINT_VALUE = {"MNQ": 2.0, "MES": 5.0, "NQ": 20.0, "ES": 50.0}
SL_MODES = ("unverified", "usd_per_contract", "usd_per_order", "points")


def symbol_root(symbol: str) -> str:
    """'/MNQZ26:XCME', 'MNQ1!', 'MNQZ2026' -> 'MNQ'. Longest root wins (MNQ before NQ)."""
    sym = str(symbol).upper().lstrip("/")
    for root in sorted(POINT_VALUE, key=len, reverse=True):
        if sym.startswith(root):
            return root
    return sym


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SafetySettings:
    # --- #4 / #5 feed ---
    allowed_sources: frozenset = frozenset({"dxlink"})
    required_symbols: tuple = ("MNQ", "MES")
    bar_seconds: int = 300                 # primary bar size (5-min)
    max_bar_lag_seconds: int = 90          # grace after the next bar was due
    max_message_gap_seconds: int | None = 30   # any feed event; None disables
    max_clock_skew_seconds: int = 5        # bar "ending in the future" tolerance

    # --- #6 fill-blind risk ---
    daily_loss_budget: float = 500.0       # Builder 25K soft pause is $600
    max_total_micros: int = 20             # Builder 25K cap, MNQ + MES combined
    # How PickMyTrade interprets dollar_sl. PMT's docs disagree with each other,
    # so until a Tradovate Demo test settles it (pmt_bracket_test.py), the
    # default assumes whichever reading gives the LARGEST loss:
    #   "usd_per_contract" - risk = dollar_sl x qty
    #   "usd_per_order"    - risk = dollar_sl
    #   "points"           - stop is dollar_sl PRICE POINTS from entry:
    #                        risk = dollar_sl x point value x qty
    #   "unverified"       - max of the three (default)
    sl_mode: str = "unverified"

    # --- #7 single instance ---
    live_host: str | None = None           # hostname allowed to send live orders
    lock_file: Path = BASE_DIR / ".bot.lock"
    halt_file: Path = BASE_DIR / "HALT"

    def __post_init__(self):
        if self.sl_mode not in SL_MODES:
            raise ValueError(f"sl_mode must be one of {SL_MODES}, got {self.sl_mode!r}")

    @classmethod
    def from_config(cls, cfg) -> "SafetySettings":
        """Build from config.py. Every SAFETY_* name is optional."""
        d = cls()

        def get(name, default):
            return getattr(cfg, name, default)

        live_host = get("LIVE_HOST", None) or os.environ.get("BOT_LIVE_HOST") or None
        return cls(
            allowed_sources=frozenset(
                s.lower() for s in get("SAFETY_ALLOWED_SOURCES", d.allowed_sources)
            ),
            required_symbols=tuple(
                s.upper() for s in get("SAFETY_REQUIRED_SYMBOLS", d.required_symbols)
            ),
            bar_seconds=int(get("SAFETY_BAR_SECONDS", d.bar_seconds)),
            max_bar_lag_seconds=int(get("SAFETY_MAX_BAR_LAG_SECONDS", d.max_bar_lag_seconds)),
            max_message_gap_seconds=get("SAFETY_MAX_MESSAGE_GAP_SECONDS", d.max_message_gap_seconds),
            max_clock_skew_seconds=int(get("SAFETY_MAX_CLOCK_SKEW_SECONDS", d.max_clock_skew_seconds)),
            daily_loss_budget=float(get("SAFETY_DAILY_LOSS_BUDGET", d.daily_loss_budget)),
            max_total_micros=int(get("SAFETY_MAX_TOTAL_MICROS", d.max_total_micros)),
            sl_mode=str(get("SAFETY_SL_MODE", d.sl_mode)).lower(),
            live_host=live_host,
            lock_file=Path(get("SAFETY_LOCK_FILE", d.lock_file)),
            halt_file=Path(get("SAFETY_HALT_FILE", d.halt_file)),
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_epoch(ts) -> float:
    """Epoch seconds from epoch-s, epoch-ms (DXLink) or a tz-aware datetime."""
    if isinstance(ts, datetime):  # also covers pandas.Timestamp
        if ts.tzinfo is None:
            raise ValueError("naive datetime - attach a timezone")
        return ts.timestamp()
    if isinstance(ts, bool):
        raise ValueError("bool is not a timestamp")
    value = float(ts)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timestamp must be a positive finite number")
    return value / 1000.0 if value > 1e11 else value  # >1e11 s is year 5138: must be ms


def _num(value) -> float | None:
    """Finite float or None. Rejects '', None, NaN, bools."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def et_date(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, ET).date().isoformat()


# --------------------------------------------------------------------------- #
# #4 + #5  FeedGuard
# --------------------------------------------------------------------------- #
@dataclass
class _SymbolFeed:
    bar_end: float | None = None
    source: str | None = None
    error: str | None = None


class FeedGuard:
    """
    Tracks freshness and source of the bars the strategy is using.

    marketdata.py calls:
      on_message()                      on ANY event received (incl. in-progress candles)
      on_closed_bar(sym, start, src)    when a bar CLOSES; `start` = candle START time
      set_active_source(src)            whenever failover switches sources
    """

    def __init__(self, settings: SafetySettings, clock: Callable[[], float] = time.time):
        self.s = settings
        self.clock = clock
        self._feeds: dict[str, _SymbolFeed] = {}
        self._last_message: float | None = None
        self._active_source: str | None = None

    def _root(self, symbol: str) -> str:
        sym = str(symbol).upper().lstrip("/")
        for root in self.s.required_symbols:
            if sym.startswith(root):
                return root
        return sym

    def set_active_source(self, source: str | None) -> None:
        self._active_source = (source or "").strip().lower() or None

    def on_message(self) -> None:
        self._last_message = self.clock()

    def on_closed_bar(self, symbol: str, bar_start, source: str,
                      bar_seconds: int | None = None) -> None:
        feed = self._feeds.setdefault(self._root(symbol), _SymbolFeed())
        try:
            start = to_epoch(bar_start)
        except (TypeError, ValueError) as exc:
            # Fail closed without crashing the bot: status() will report it.
            feed.error = f"bad bar timestamp {bar_start!r} ({exc})"
            return
        feed.bar_end = start + (bar_seconds or self.s.bar_seconds)
        feed.source = (source or "").strip().lower()
        feed.error = None

    def on_bar_series(self, symbol: str, bar_starts, source: str,
                      bar_seconds: int | None = None) -> None:
        """
        Given every bar START time a data source returned (oldest -> newest),
        record the newest bar that has actually CLOSED. An in-progress bar at
        the end of the series is skipped instead of tripping the clock-skew check.
        """
        bs = bar_seconds or self.s.bar_seconds
        now = self.clock()
        starts = list(bar_starts)
        for raw in reversed(starts):
            try:
                start = to_epoch(raw)
            except (TypeError, ValueError):
                break  # let on_closed_bar record the bad timestamp
            if start + bs <= now + self.s.max_clock_skew_seconds:
                self.on_closed_bar(symbol, start, source, bs)
                return
        if starts:
            self.on_closed_bar(symbol, starts[-1], source, bs)
        else:
            feed = self._feeds.setdefault(self._root(symbol), _SymbolFeed())
            feed.error = f"{source} returned no bars"

    def problems(self) -> list[str]:
        now = self.clock()
        out: list[str] = []
        allowed = self.s.allowed_sources

        if self._active_source is not None and self._active_source not in allowed:
            out.append(f"active data source '{self._active_source}' is observe-only "
                       f"(trading sources: {', '.join(sorted(allowed))})")

        gap_limit = self.s.max_message_gap_seconds
        if gap_limit is not None and self._last_message is not None:
            gap = now - self._last_message
            if gap > gap_limit:
                out.append(f"no feed messages for {gap:.0f}s (limit {gap_limit}s)")

        lag_limit = self.s.bar_seconds + self.s.max_bar_lag_seconds
        for sym in self.s.required_symbols:
            feed = self._feeds.get(sym)
            if feed is None or feed.bar_end is None:
                detail = f" - {feed.error}" if feed and feed.error else ""
                out.append(f"{sym}: no closed bar received yet{detail}")
                continue
            if feed.error:
                out.append(f"{sym}: {feed.error}")
            if feed.source not in allowed:
                out.append(f"{sym}: last bar came from '{feed.source}', which is observe-only")
            lag = now - feed.bar_end
            if lag < -self.s.max_clock_skew_seconds:
                out.append(f"{sym}: last bar ends {-lag:.0f}s in the future - clock skew, "
                           f"or bar END passed where bar START was expected")
            elif lag > lag_limit:
                out.append(f"{sym}: last closed bar ended {lag:.0f}s ago "
                           f"(limit {lag_limit}s) - feed delayed or stalled")
        return out


# --------------------------------------------------------------------------- #
# #6  Fill-blind risk
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EntryIntent:
    symbol: str
    side: str          # "buy" or "sell"
    qty: int
    price: float
    dollar_sl: float
    dollar_tp: float


@dataclass
class OpenPosition:
    symbol: str
    qty: int
    risk_dollars: float
    opened_at: float


class RiskLedger:
    """
    The bot cannot see fills on eval/sim-funded accounts, so P&L here is an
    ESTIMATE. To stay safe it assumes every open trade will hit its stop.

    Call record_entry() after an entry is sent, record_exit() when the bot
    believes a trade closed (its own exit, EOD close, or estimated bracket hit).
    """

    def __init__(self, settings: SafetySettings, clock: Callable[[], float] = time.time):
        self.s = settings
        self.clock = clock
        self.day = et_date(clock())
        self._realized_est = 0.0
        self.open: dict[str, OpenPosition] = {}

    @property
    def realized_est(self) -> float:
        """Estimated realized P&L for the current ET trading day."""
        self._roll_day()
        return self._realized_est

    def _roll_day(self) -> None:
        today = et_date(self.clock())
        if today != self.day:
            self.day = today
            self._realized_est = 0.0
            # self.open is intentionally kept: anything still open across a day
            # boundary means the EOD close failed, so its risk must keep counting.

    def risk_for(self, qty: int, dollar_sl: float, symbol: str) -> float:
        """Dollar loss if this stop is hit, under the configured sl_mode."""
        per_contract = dollar_sl * qty
        per_order = dollar_sl
        mode = self.s.sl_mode
        if mode == "usd_per_contract":
            return per_contract
        if mode == "usd_per_order":
            return per_order
        point_value = POINT_VALUE.get(symbol_root(symbol))
        if point_value is None:
            # Unknown contract: can't convert points safely. Refuse via infinite risk.
            return math.inf
        points = dollar_sl * point_value * qty
        return points if mode == "points" else max(per_contract, per_order, points)

    def open_micros(self) -> int:
        self._roll_day()
        return sum(p.qty for p in self.open.values())

    def open_risk(self) -> float:
        self._roll_day()
        return sum(p.risk_dollars for p in self.open.values())

    def worst_case_pnl(self) -> float:
        self._roll_day()
        return self.realized_est - self.open_risk()

    def record_entry(self, key: str, symbol: str, qty: int, dollar_sl: float) -> str:
        """Book an entry's stop risk. Returns the key actually used: a duplicate
        key gets a suffix instead of silently overwriting (and losing) risk."""
        self._roll_day()
        unique, n = key, 1
        while unique in self.open:
            n += 1
            unique = f"{key}#{n}"
        self.open[unique] = OpenPosition(symbol, int(qty),
                                         self.risk_for(int(qty), float(dollar_sl), symbol),
                                         self.clock())
        return unique

    def record_exit(self, key: str, est_pnl: float) -> bool:
        """Returns False if the key was unknown (P&L is still booked)."""
        self._roll_day()
        self._realized_est += float(est_pnl)
        return self.open.pop(key, None) is not None

    def record_flatten(self) -> float:
        """After a blind flatten, book the worst case: every open stop was hit."""
        self._roll_day()
        loss = self.open_risk()
        self._realized_est -= loss
        self.open.clear()
        return loss

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "realized_est": self.realized_est,
            "open": {k: vars(p).copy() for k, p in self.open.items()},
        }

    def save(self, path) -> None:
        """Atomic write, so a crash mid-save can't leave a half-written file."""
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path, settings: SafetySettings,
             clock: Callable[[], float] = time.time) -> "RiskLedger":
        """Restore today's ledger after a restart. Missing or corrupt file -> fresh ledger."""
        try:
            return cls.from_dict(json.loads(Path(path).read_text()), settings, clock)
        except FileNotFoundError:
            return cls(settings, clock)
        except (ValueError, TypeError, KeyError) as exc:
            print(f"[safety] could not read {path} ({exc}); starting a fresh ledger")
            return cls(settings, clock)

    @classmethod
    def from_dict(cls, data: dict, settings: SafetySettings,
                  clock: Callable[[], float] = time.time) -> "RiskLedger":
        ledger = cls(settings, clock)
        ledger.day = data.get("day", ledger.day)
        ledger._realized_est = float(data.get("realized_est", 0.0))
        ledger.open = {k: OpenPosition(**v) for k, v in data.get("open", {}).items()}
        ledger._roll_day()
        return ledger


def check_intent(intent: EntryIntent, ledger: RiskLedger, s: SafetySettings) -> list[str]:
    out: list[str] = []
    side = str(intent.side).lower()
    if side not in ("buy", "sell"):
        out.append(f"side must be 'buy' or 'sell', got {intent.side!r}")

    if symbol_root(intent.symbol) not in POINT_VALUE:
        out.append(f"unknown contract {intent.symbol!r} - no point value to size risk with")

    qty = intent.qty
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
        out.append(f"qty must be a whole number >= 1, got {qty!r}")
        qty = 0

    price = _num(intent.price)
    if price is None or price <= 0:
        out.append(f"price must be a positive number, got {intent.price!r} "
                   f"(empty price = PMT 'Wrong Price' rejection)")

    sl, tp = _num(intent.dollar_sl), _num(intent.dollar_tp)
    if sl is None or sl <= 0:
        out.append(f"dollar_sl must be > 0, got {intent.dollar_sl!r} - no entries without a stop")
    if tp is None or tp <= 0:
        out.append(f"dollar_tp must be > 0, got {intent.dollar_tp!r}")

    if qty:
        total = ledger.open_micros() + qty
        if total > s.max_total_micros:
            out.append(f"would hold {total} micros, cap is {s.max_total_micros}")
        if sl and sl > 0:
            new_risk = ledger.risk_for(qty, sl, intent.symbol)
            worst = ledger.worst_case_pnl() - new_risk
            if worst < -s.daily_loss_budget:
                out.append(f"worst-case day P&L would be ${worst:,.2f} "
                           f"(budget -${s.daily_loss_budget:,.2f}; "
                           f"est. realized ${ledger.realized_est:,.2f}, "
                           f"open risk ${ledger.open_risk():,.2f}, this trade ${new_risk:,.2f})")
    return out


PMT_ACTIONS = ("buy", "sell", "close")


def lint_pmt_payload(payload: dict) -> list[str]:
    """
    Last check on the exact JSON going to PickMyTrade. Key names follow our PMT
    schema notes (data / price / dollar_sl / dollar_tp / quantity_multiplier /
    token / multiple_accounts). Never echoes token values.
    """
    out: list[str] = []
    action = str(payload.get("data", "")).lower()
    if action not in PMT_ACTIONS:
        out.append(f"'data' must be one of {PMT_ACTIONS}, got {payload.get('data')!r}")

    price = payload.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or _num(price) is None:
        out.append(f"'price' must be a number, got {price!r} (strings/empty = 'Wrong Price')")

    if action in ("buy", "sell"):
        for key in ("dollar_sl", "dollar_tp"):
            val = payload.get(key)
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not (_num(val) or 0) > 0:
                out.append(f"'{key}' must be a number > 0 on entries, got {val!r}")

    if "quantity_multiplier" in payload and payload["quantity_multiplier"] != 1:
        out.append(f"'quantity_multiplier' must be 1, got {payload['quantity_multiplier']!r}")

    if not payload.get("token"):
        out.append("top-level 'token' is missing")
    for i, acct in enumerate(payload.get("multiple_accounts") or []):
        if not isinstance(acct, dict) or not acct.get("token"):
            out.append(f"multiple_accounts[{i}] is missing 'token'")
    return out


# --------------------------------------------------------------------------- #
# #6  Kill switch
# --------------------------------------------------------------------------- #
class KillSwitch:
    """A file on disk: absent = run, 'HALT' = no entries, 'FLATTEN' = close all."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def state(self) -> str | None:
        try:
            first = self.path.read_text().strip().splitlines()[:1]
        except FileNotFoundError:
            return None
        return "flatten" if first and first[0].upper() == "FLATTEN" else "halt"

    def halt(self, flatten: bool = False, reason: str = "") -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.path.write_text(f"{'FLATTEN' if flatten else 'HALT'}\n{stamp} {reason}".rstrip() + "\n")

    def acknowledge_flatten(self, summary: str = "") -> None:
        self.halt(flatten=False, reason=f"flatten done: {summary}")

    def resume(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False


def flatten_all(send_close: Callable[[str], object], symbols: Iterable[str]) -> dict[str, str]:
    """Send a close for every symbol. One attempt each; never raises."""
    results: dict[str, str] = {}
    for sym in symbols:
        try:
            send_close(sym)
            results[sym] = "sent"
        except Exception as exc:  # noqa: BLE001 - must keep going to the next symbol
            results[sym] = f"FAILED: {type(exc).__name__}: {exc}"
    return results


# --------------------------------------------------------------------------- #
# #7  Single instance
# --------------------------------------------------------------------------- #
class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """
    OS file lock: one bot per machine. Released automatically if the process
    dies, so there are no stale locks to clean up after a crash.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> "InstanceLock":
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            holder = fh.read().strip() or "pid unknown"
            fh.close()
            raise AlreadyRunning(f"another bot instance is running ({holder}); lock: {self.path}")
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} host={socket.gethostname()} "
                 f"started={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def host_problem(s: SafetySettings, hostname: str | None = None) -> str | None:
    """Two machines (Mac + VPS) must never both send live orders."""
    host = (hostname or socket.gethostname()).lower()
    if not s.live_host:
        return ("LIVE_HOST is not set - name the one machine allowed to send live orders "
                f"(this one is '{host}')")
    if host != s.live_host.lower():
        return f"this machine is '{host}'; only '{s.live_host}' may send live orders"
    return None


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
@dataclass
class Decision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.allowed

    def __str__(self) -> str:
        return "OK" if self.allowed else " | ".join(self.reasons)


class SafetyGate:
    def __init__(self, settings: SafetySettings, feed: FeedGuard | None = None,
                 ledger: RiskLedger | None = None, kill: KillSwitch | None = None,
                 clock: Callable[[], float] = time.time, hostname: str | None = None):
        self.s = settings
        self.feed = feed or FeedGuard(settings, clock)
        self.ledger = ledger or RiskLedger(settings, clock)
        self.kill = kill or KillSwitch(settings.halt_file)
        self.hostname = hostname

    def check_entry(self, intent: EntryIntent, live: bool) -> Decision:
        """Gate for NEW entries only. live=True also enforces LIVE_HOST."""
        reasons: list[str] = []
        state = self.kill.state()
        if state:
            reasons.append(f"kill switch is set ({state.upper()}) - run "
                           f"'python3 safety.py resume' to clear")
        if live:
            problem = host_problem(self.s, self.hostname)
            if problem:
                reasons.append(problem)
        reasons.extend(self.feed.problems())
        reasons.extend(check_intent(intent, self.ledger, self.s))
        return Decision(not reasons, reasons)

    def poll_kill_switch(self, send_close: Callable[[str], object],
                         symbols: Iterable[str] | None = None) -> dict[str, str] | None:
        """
        Call once per main-loop cycle. If FLATTEN was requested, closes every
        symbol once, books worst-case P&L, flips the file to HALT and returns
        the per-symbol results (send them to notify.py). Otherwise None.
        """
        if self.kill.state() != "flatten":
            return None
        results = flatten_all(send_close, symbols or self.s.required_symbols)
        loss = self.ledger.record_flatten()
        summary = ", ".join(f"{k}={v}" for k, v in results.items())
        self.kill.acknowledge_flatten(f"{summary}; booked worst-case -${loss:,.2f}")
        return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="safety.py", description="Bot kill switch")
    sub = parser.add_subparsers(dest="cmd", required=True)
    halt = sub.add_parser("halt", help="block new entries")
    halt.add_argument("--flatten", action="store_true", help="also close all positions")
    halt.add_argument("--reason", default="manual")
    sub.add_parser("resume", help="clear the halt")
    sub.add_parser("status", help="show halt and lock state")
    args = parser.parse_args(argv)

    s = SafetySettings()
    kill = KillSwitch(s.halt_file)
    if args.cmd == "halt":
        kill.halt(flatten=args.flatten, reason=args.reason)
        what = "HALT + FLATTEN" if args.flatten else "HALT"
        print(f"{what} set at {kill.path}. The bot acts on it at its next loop cycle.")
        if args.flatten:
            print("Confirm flat in the Tradovate / MFFU dashboard - the bot cannot see fills.")
    elif args.cmd == "resume":
        print("Halt cleared." if kill.resume() else "No halt was set.")
    else:
        print(f"kill switch: {kill.state() or 'clear'}")
        try:
            print(f"lock file:   {s.lock_file.read_text().strip() or '(empty)'}")
        except FileNotFoundError:
            print("lock file:   none (bot has not run here)")
        print(f"hostname:    {socket.gethostname()}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
