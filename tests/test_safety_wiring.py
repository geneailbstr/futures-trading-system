"""
tests/test_safety_wiring.py — the marketdata -> FeedGuard hook.

tastytrade returns a naive-UTC index and yfinance a tz-aware one; both must
land on the right epoch, and an in-progress bar must not trip the guard.
"""


import pandas as pd
import pytest

import marketdata
import safety


@pytest.fixture
def guard(tmp_path):
    s = safety.SafetySettings(halt_file=tmp_path / "HALT", lock_file=tmp_path / ".lock")
    g = safety.FeedGuard(s)
    marketdata.set_feed_guard(g)
    yield g
    marketdata.set_feed_guard(None)


def _bars(index):
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                        index=index)


def _recent_starts(n=4):
    """5-min bar starts ending with one that is still forming."""
    now = pd.Timestamp.now(tz="UTC").floor("5min")
    return pd.date_range(end=now, periods=n, freq="5min")


def test_naive_utc_index_from_tastytrade(guard):
    idx = _recent_starts().tz_localize(None)                 # naive, as tt_marketdata builds it
    for sym in ("MNQ", "MES"):
        marketdata._note_bars(sym, "5min", _bars(idx), "dxlink")
    assert guard.problems() == []


def test_tz_aware_index_from_yfinance_is_observe_only(guard):
    idx = _recent_starts().tz_convert("America/New_York")
    for sym in ("MNQ", "MES"):
        marketdata._note_bars(sym, "5min", _bars(idx), "yfinance")
    problems = guard.problems()
    assert problems and all("observe-only" in p for p in problems)
    assert not any("in the future" in p or "delayed" in p for p in problems)


def test_delayed_feed_detected_through_hook(guard):
    idx = (_recent_starts() - pd.Timedelta(minutes=15)).tz_localize(None)
    for sym in ("MNQ", "MES"):
        marketdata._note_bars(sym, "5min", _bars(idx), "dxlink")
    assert all("delayed or stalled" in p for p in guard.problems())


def test_only_5min_requests_are_tracked(guard):
    idx = (_recent_starts() - pd.Timedelta(hours=3)).tz_localize(None)
    marketdata._note_bars("MNQ", "15min", _bars(idx), "dxlink")
    marketdata._note_bars("MNQ", "1day", _bars(idx), "dxlink")
    assert "MNQ: no closed bar received yet" in guard.problems()


def test_hook_is_a_noop_without_a_guard():
    marketdata.set_feed_guard(None)
    marketdata._note_bars("MNQ", "5min", _bars(_recent_starts()), "dxlink")  # must not raise


def test_bad_frame_never_raises(guard, capsys):
    marketdata._note_bars("MNQ", "5min", pd.DataFrame({"close": [1.0]}, index=["oops"]), "dxlink")
    assert "feed guard update failed" in capsys.readouterr().out
    assert any(p.startswith("MNQ") for p in guard.problems())
