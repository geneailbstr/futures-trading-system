"""Tests for safety.py - run with:  python3 -m pytest tests/test_safety.py -q"""
from datetime import datetime, timezone

import pytest

import safety as sf

# Tue 2026-11-10 10:00:00 ET (EST, UTC-5) -> 15:00 UTC
NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def s(tmp_path):
    return sf.SafetySettings(halt_file=tmp_path / "HALT", lock_file=tmp_path / ".bot.lock",
                             live_host="trading-vps")


def fresh_feed(s, clock, source="dxlink"):
    feed = sf.FeedGuard(s, clock)
    bar_start = clock() - 300 - 5  # bar closed 5s ago
    for sym in ("MNQ", "MES"):
        feed.on_closed_bar(sym, bar_start, source)
    feed.on_message()
    return feed


def good_intent(**kw):
    base = dict(symbol="MNQ", side="buy", qty=2, price=21000.25, dollar_sl=60.0, dollar_tp=120.0)
    base.update(kw)
    return sf.EntryIntent(**base)


# ---------------- #4 / #5 feed ----------------
def test_fresh_dxlink_feed_is_clean(s, clock):
    assert fresh_feed(s, clock).problems() == []


def test_delayed_feed_blocked(s, clock):
    feed = sf.FeedGuard(s, clock)
    delayed_start = clock() - 15 * 60 - 300  # 15-minute delayed data
    for sym in ("MNQ", "MES"):
        feed.on_closed_bar(sym, delayed_start, "dxlink")
    problems = feed.problems()
    assert len(problems) == 2 and all("delayed or stalled" in p for p in problems)


def test_lag_threshold_edges(s, clock):
    feed = sf.FeedGuard(s, clock)
    limit = s.bar_seconds + s.max_bar_lag_seconds
    feed.on_closed_bar("MNQ", clock() - 300 - limit, "dxlink")       # exactly at limit: ok
    feed.on_closed_bar("MES", clock() - 300 - limit - 1, "dxlink")   # 1s over: blocked
    problems = feed.problems()
    assert len(problems) == 1 and problems[0].startswith("MES")


def test_proxy_source_is_observe_only(s, clock):
    problems = fresh_feed(s, clock, source="yfinance").problems()
    assert len(problems) == 2 and all("observe-only" in p for p in problems)


def test_active_source_failover_blocks(s, clock):
    feed = fresh_feed(s, clock)
    feed.set_active_source("yfinance")
    assert any("active data source 'yfinance'" in p for p in feed.problems())
    feed.set_active_source("DXLink")
    assert feed.problems() == []


def test_message_gap_blocks(s, clock):
    feed = fresh_feed(s, clock)
    clock.t += 31
    # bar still within lag window, but the socket has gone quiet
    assert any("no feed messages" in p for p in feed.problems())


def test_message_gap_skipped_when_never_wired(s, clock):
    feed = sf.FeedGuard(s, clock)
    for sym in ("MNQ", "MES"):
        feed.on_closed_bar(sym, clock() - 305, "dxlink")
    assert feed.problems() == []


def test_dxlink_millisecond_timestamps(s, clock):
    feed = sf.FeedGuard(s, clock)
    ms = int((clock() - 305) * 1000)
    feed.on_closed_bar("/MNQZ26:XCME", ms, "dxlink")
    feed.on_closed_bar("/MESZ26:XCME", ms, "dxlink")
    assert feed.problems() == []


def test_tz_aware_datetime_accepted(s, clock):
    feed = sf.FeedGuard(s, clock)
    dt = datetime.fromtimestamp(clock() - 305, timezone.utc)
    for sym in ("MNQ", "MES"):
        feed.on_closed_bar(sym, dt, "dxlink")
    assert feed.problems() == []


def test_naive_datetime_fails_closed_without_raising(s, clock):
    feed = sf.FeedGuard(s, clock)
    feed.on_closed_bar("MNQ", datetime(2026, 11, 10, 9, 55), "dxlink")
    feed.on_closed_bar("MES", clock() - 305, "dxlink")
    problems = feed.problems()
    assert len(problems) == 1 and "naive datetime" in problems[0]


def test_bar_end_passed_as_start_is_caught(s, clock):
    feed = sf.FeedGuard(s, clock)
    for sym in ("MNQ", "MES"):
        feed.on_closed_bar(sym, clock() - 5, "dxlink")  # wrong: this is the END time
    assert all("in the future" in p for p in feed.problems())


def test_bar_series_skips_in_progress_bar(s, clock):
    feed = sf.FeedGuard(s, clock)
    starts = [clock() - 900, clock() - 600, clock() - 300 - 5, clock() - 5]  # last one still forming
    for sym in ("MNQ", "MES"):
        feed.on_bar_series(sym, starts, "dxlink")
    assert feed.problems() == []


def test_bar_series_all_stale(s, clock):
    feed = sf.FeedGuard(s, clock)
    starts = [clock() - 3600, clock() - 3300]
    for sym in ("MNQ", "MES"):
        feed.on_bar_series(sym, starts, "dxlink")
    assert all("delayed or stalled" in p for p in feed.problems())


def test_bar_series_empty(s, clock):
    feed = sf.FeedGuard(s, clock)
    feed.on_bar_series("MNQ", [], "dxlink")
    feed.on_bar_series("MES", [clock() - 305], "dxlink")
    assert feed.problems() == ["MNQ: no closed bar received yet - dxlink returned no bars"]


def test_missing_symbol_blocks(s, clock):
    feed = sf.FeedGuard(s, clock)
    feed.on_closed_bar("MNQ", clock() - 305, "dxlink")
    assert feed.problems() == ["MES: no closed bar received yet"]


# ---------------- #6 intent + ledger ----------------
def test_good_intent_passes(s, clock):
    assert sf.check_intent(good_intent(), sf.RiskLedger(s, clock), s) == []


@pytest.mark.parametrize("field,value,needle", [
    ("price", "", "price must be a positive number"),
    ("price", float("nan"), "price must be a positive number"),
    ("dollar_sl", 0, "no entries without a stop"),
    ("dollar_sl", None, "no entries without a stop"),
    ("dollar_tp", -5, "dollar_tp must be > 0"),
    ("side", "long", "side must be"),
    ("qty", 0, "qty must be"),
    ("qty", 1.5, "qty must be"),
    ("qty", True, "qty must be"),
])
def test_bad_intents(s, clock, field, value, needle):
    problems = sf.check_intent(good_intent(**{field: value}), sf.RiskLedger(s, clock), s)
    assert any(needle in p for p in problems), problems


def test_contract_cap_counts_open_positions(s, clock):
    ledger = sf.RiskLedger(s, clock)
    ledger.record_entry("a", "MNQ", 19, 1.0)
    problems = sf.check_intent(good_intent(qty=2, dollar_sl=1.0), ledger, s)
    assert any("cap is 20" in p for p in problems)


def test_risk_budget_includes_open_stop_risk(tmp_path, clock):
    s = sf.SafetySettings(sl_mode="usd_per_contract", halt_file=tmp_path / "H")
    ledger = sf.RiskLedger(s, clock)
    ledger.record_exit("x", -200.0)              # est. realized -200
    ledger.record_entry("a", "MES", 2, 60.0)     # open risk 120 (per contract)
    # new trade 2 x 60 = 120 -> worst case -440: allowed
    assert sf.check_intent(good_intent(), ledger, s) == []
    # new trade 2 x 100 = 200 -> worst case -520: blocked
    problems = sf.check_intent(good_intent(dollar_sl=100.0), ledger, s)
    assert any("worst-case day P&L" in p for p in problems)


@pytest.mark.parametrize("mode,symbol,expected", [
    ("usd_per_contract", "MNQ", 240.0),   # 60 x 4
    ("usd_per_order", "MNQ", 60.0),       # 60
    ("points", "MNQ", 480.0),             # 60 pts x $2 x 4
    ("points", "MES", 1200.0),            # 60 pts x $5 x 4
    ("unverified", "MNQ", 480.0),         # worst of the three
    ("unverified", "/MESZ26:XCME", 1200.0),
    ("unverified", "MNQ1!", 480.0),
])
def test_sl_modes(tmp_path, clock, mode, symbol, expected):
    s = sf.SafetySettings(sl_mode=mode, halt_file=tmp_path / "H")
    assert sf.RiskLedger(s, clock).risk_for(4, 60.0, symbol) == expected


def test_unverified_default_blocks_trade_that_per_contract_allows(s, clock):
    # 2 MES at dollar_sl 60: $120 if USD/contract, but $600 if PMT reads points
    intent = good_intent(symbol="MES")
    assert any("worst-case day P&L" in p for p in sf.check_intent(intent, sf.RiskLedger(s, clock), s))


def test_unknown_contract_refused(s, clock):
    problems = sf.check_intent(good_intent(symbol="M2K"), sf.RiskLedger(s, clock), s)
    assert any("unknown contract" in p for p in problems)


def test_symbol_root_prefers_longest():
    assert sf.symbol_root("MNQZ2026") == "MNQ"
    assert sf.symbol_root("NQZ26") == "NQ"
    assert sf.symbol_root("/MES") == "MES"


def test_bad_sl_mode_rejected():
    with pytest.raises(ValueError, match="sl_mode"):
        sf.SafetySettings(sl_mode="per_contract")


def test_ledger_rolls_on_et_date_and_keeps_open(s, clock):
    ledger = sf.RiskLedger(s, clock)
    ledger.record_exit("x", -300.0)
    ledger.record_entry("stuck", "MNQ", 1, 50.0)   # unverified: 50 pts x $2 = $100
    clock.t += 24 * 3600
    assert ledger.realized_est == 0.0
    assert ledger.open_risk() == 100.0  # failed EOD close still counts


def test_ledger_et_boundary_not_utc(s):
    # 2026-11-10 23:30 ET = 2026-11-11 04:30 UTC -> still the 10th in ET
    late = datetime(2026, 11, 11, 4, 30, tzinfo=timezone.utc).timestamp()
    assert sf.et_date(late) == "2026-11-10"


def test_ledger_roundtrip(s, clock):
    ledger = sf.RiskLedger(s, clock)
    ledger.record_entry("a", "MNQ", 2, 40.0)
    ledger.record_exit("b", 25.0)
    again = sf.RiskLedger.from_dict(ledger.to_dict(), s, clock)
    assert again.realized_est == 25.0 and again.open_risk() == 160.0


def test_ledger_save_load(s, clock, tmp_path):
    path = tmp_path / "safety_state.json"
    ledger = sf.RiskLedger(s, clock)
    ledger.record_entry("a", "MNQ", 1, 30.0)
    ledger.save(path)
    again = sf.RiskLedger.load(path, s, clock)
    assert again.open_risk() == ledger.open_risk() and not path.with_suffix(".json.tmp").exists()


def test_ledger_load_missing_or_corrupt(s, clock, tmp_path):
    assert sf.RiskLedger.load(tmp_path / "nope.json", s, clock).open == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert sf.RiskLedger.load(bad, s, clock).open == {}


def test_duplicate_entry_key_never_overwrites(s, clock):
    ledger = sf.RiskLedger(s, clock)
    k1 = ledger.record_entry("MNQ-093000", "MNQ", 1, 20.0)
    k2 = ledger.record_entry("MNQ-093000", "MNQ", 1, 20.0)
    k3 = ledger.record_entry("MNQ-093000", "MNQ", 1, 20.0)
    assert (k1, k2, k3) == ("MNQ-093000", "MNQ-093000#2", "MNQ-093000#3")
    assert ledger.open_risk() == 120.0


def test_record_exit_unknown_key_still_books(s, clock):
    ledger = sf.RiskLedger(s, clock)
    assert ledger.record_exit("ghost", -10.0) is False
    assert ledger.realized_est == -10.0


# ---------------- PMT payload lint ----------------
def payload(**kw):
    p = {"data": "buy", "symbol": "MNQ", "price": 21000.25, "dollar_sl": 60, "dollar_tp": 120,
         "quantity_multiplier": 1, "token": "SECRET-TOKEN",
         "multiple_accounts": [{"token": "SECRET-TOKEN", "account_id": "X"}]}
    p.update(kw)
    return p


def test_lint_good_payload():
    assert sf.lint_pmt_payload(payload()) == []


def test_lint_close_needs_no_bracket():
    assert sf.lint_pmt_payload(payload(data="close", dollar_sl=None, dollar_tp=None)) == []


@pytest.mark.parametrize("kw,needle", [
    ({"price": ""}, "'price' must be a number"),
    ({"price": "21000"}, "'price' must be a number"),
    ({"data": "long"}, "'data' must be one of"),
    ({"dollar_sl": 0}, "'dollar_sl' must be a number > 0"),
    ({"dollar_tp": "120"}, "'dollar_tp' must be a number > 0"),
    ({"quantity_multiplier": 2}, "'quantity_multiplier' must be 1"),
    ({"token": ""}, "top-level 'token' is missing"),
    ({"multiple_accounts": [{"account_id": "X"}]}, "multiple_accounts[0] is missing 'token'"),
])
def test_lint_bad_payloads(kw, needle):
    problems = sf.lint_pmt_payload(payload(**kw))
    assert any(needle in p for p in problems), problems


def test_lint_never_leaks_token():
    problems = sf.lint_pmt_payload(payload(price="", multiple_accounts=[{"token": ""}]))
    assert problems and not any("SECRET-TOKEN" in p for p in problems)


# ---------------- kill switch ----------------
def test_kill_switch_states(s):
    kill = sf.KillSwitch(s.halt_file)
    assert kill.state() is None
    kill.halt()
    assert kill.state() == "halt"
    kill.halt(flatten=True)
    assert kill.state() == "flatten"
    assert kill.resume() is True and kill.state() is None
    assert kill.resume() is False


def test_halt_blocks_entries(s, clock):
    gate = sf.SafetyGate(s, feed=fresh_feed(s, clock), clock=clock, hostname="trading-vps")
    assert gate.check_entry(good_intent(), live=True)
    gate.kill.halt()
    decision = gate.check_entry(good_intent(), live=True)
    assert not decision and "kill switch" in str(decision)


def test_flatten_poll_closes_books_worst_case_and_acks(s, clock):
    gate = sf.SafetyGate(s, feed=fresh_feed(s, clock), clock=clock)
    gate.ledger.record_entry("a", "MNQ", 2, 50.0)
    sent = []
    assert gate.poll_kill_switch(sent.append) is None   # nothing requested yet
    gate.kill.halt(flatten=True)
    results = gate.poll_kill_switch(sent.append)
    assert sent == ["MNQ", "MES"] and results == {"MNQ": "sent", "MES": "sent"}
    assert gate.ledger.realized_est == -200.0 and gate.ledger.open == {}
    assert gate.kill.state() == "halt"                 # entries stay blocked
    assert gate.poll_kill_switch(sent.append) is None  # not repeated


def test_flatten_survives_send_errors(s, clock):
    gate = sf.SafetyGate(s, clock=clock)
    gate.kill.halt(flatten=True)

    def send(sym):
        if sym == "MNQ":
            raise ConnectionError("PMT down")

    results = gate.poll_kill_switch(send)
    assert results["MNQ"].startswith("FAILED: ConnectionError") and results["MES"] == "sent"


def test_cli_halt_resume(tmp_path, monkeypatch):
    real = sf.SafetySettings
    monkeypatch.setattr(sf, "SafetySettings",
                        lambda: real(halt_file=tmp_path / "HALT", lock_file=tmp_path / ".lock"))
    assert sf._cli(["halt", "--flatten"]) == 0
    assert (tmp_path / "HALT").read_text().startswith("FLATTEN")
    assert sf._cli(["resume"]) == 0
    assert not (tmp_path / "HALT").exists()


# ---------------- #7 single instance ----------------
def test_instance_lock_blocks_second_copy(s):
    with sf.InstanceLock(s.lock_file):
        with pytest.raises(sf.AlreadyRunning, match="pid="):
            sf.InstanceLock(s.lock_file).acquire()
    with sf.InstanceLock(s.lock_file):   # released -> can take it again
        pass


def test_host_check():
    s = sf.SafetySettings(live_host="trading-vps")
    assert sf.host_problem(s, "trading-vps") is None
    assert sf.host_problem(s, "TRADING-VPS") is None
    assert "only 'trading-vps'" in sf.host_problem(s, "genes-macbook-pro.local")
    assert "LIVE_HOST is not set" in sf.host_problem(sf.SafetySettings(), "anything")


def test_live_mode_enforces_host_sim_does_not(s, clock):
    gate = sf.SafetyGate(s, feed=fresh_feed(s, clock), clock=clock, hostname="genes-mac")
    assert not gate.check_entry(good_intent(), live=True)
    assert gate.check_entry(good_intent(), live=False)


def test_gate_collects_every_reason(s, clock):
    gate = sf.SafetyGate(s, clock=clock, hostname="genes-mac")   # no bars at all
    gate.kill.halt()
    decision = gate.check_entry(good_intent(price=""), live=True)
    text = str(decision)
    for needle in ("kill switch", "only 'trading-vps'", "MNQ: no closed bar", "price must be"):
        assert needle in text


# ---------------- config loading ----------------
def test_from_config_reads_overrides_and_env(monkeypatch):
    class Cfg:
        SAFETY_ALLOWED_SOURCES = ["DXLink"]
        SAFETY_DAILY_LOSS_BUDGET = 450
        SAFETY_MAX_MESSAGE_GAP_SECONDS = None
        SAFETY_SL_MODE = "USD_PER_CONTRACT"

    monkeypatch.setenv("BOT_LIVE_HOST", "trading-vps")
    s = sf.SafetySettings.from_config(Cfg)
    assert s.allowed_sources == frozenset({"dxlink"})
    assert s.daily_loss_budget == 450.0
    assert s.max_message_gap_seconds is None
    assert s.live_host == "trading-vps"
    assert s.required_symbols == ("MNQ", "MES")
    assert s.sl_mode == "usd_per_contract"
    assert sf.SafetySettings().sl_mode == "unverified"
