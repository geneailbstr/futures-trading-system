"""Tests for pmt_bracket_test.py logic - no network."""
import pytest

import pmt_bracket_test as bt
import safety as sf


def test_expected_distances_mnq():
    assert bt.expected_distances(20, 1, "MNQ1!") == {
        "usd_per_contract": 10.0, "usd_per_order": 10.0, "points": 20}
    assert bt.expected_distances(20, 2, "MNQ") == {
        "usd_per_contract": 10.0, "usd_per_order": 5.0, "points": 20}


def test_expected_distances_mes():
    assert bt.expected_distances(20, 1, "MES1!")["usd_per_contract"] == 4.0


# Simulated Tradovate readings for each true PMT behaviour (with a tick of slippage)
SCENARIOS = {
    "usd_per_contract": [("MNQ", 1, 10.25), ("MNQ", 2, 9.75), ("MES", 1, 4.0)],
    "usd_per_order": [("MNQ", 1, 10.0), ("MNQ", 2, 5.25), ("MES", 1, 4.0)],
    "points": [("MNQ", 1, 20.0), ("MNQ", 2, 19.75), ("MES", 1, 20.25)],
}


@pytest.mark.parametrize("truth", SCENARIOS)
def test_verdict_recovers_true_mode(truth):
    obs = [{"symbol": s, "qty": q, "dollar_sl": 20.0, "distance": d} for s, q, d in SCENARIOS[truth]]
    mode, _ = bt.verdict(obs)
    assert mode == truth


def test_verdict_ambiguous_with_single_contract_only():
    mode, notes = bt.verdict([{"symbol": "MNQ", "qty": 1, "dollar_sl": 20.0, "distance": 10.0}])
    assert mode is None and "ambiguous" in notes[-1]


def test_verdict_no_fit():
    mode, notes = bt.verdict([{"symbol": "MNQ", "qty": 1, "dollar_sl": 20.0, "distance": 37.0}])
    assert mode is None and "PMT support" in notes[-1]


def test_verdict_modes_are_valid_safety_modes():
    assert set(SCENARIOS) <= set(sf.SL_MODES)


def test_payloads_pass_lint():
    entry = bt.build_payload("buy", "MNQ", 1, 21000.25, "tok", "DEMO1", dollar_sl=20, dollar_tp=40)
    close = bt.build_payload("close", "MNQ", 0, 0, "tok", "DEMO1")
    for p in (entry, close):
        assert sf.lint_pmt_payload(p) == [], p["data"]
    assert close["dollar_sl"] == 0 and close["dollar_tp"] == 0


def test_payload_is_the_bots_own_pointed_at_demo():
    p = bt.build_payload("buy", "MES", 2, 6000.0, "tok", "DEMO1", dollar_sl=20, dollar_tp=40)
    assert set(p) == set(bt.pmt._build_payload("MES", "buy", 2))   # same schema as the bot
    assert p["token"] == "tok"
    assert p["multiple_accounts"] == [{"token": "tok", "account_id": "DEMO1",
                                       "risk_percentage": 0, "quantity_multiplier": 1}]


def test_redact_hides_every_token():
    p = bt.redact(bt.build_payload("buy", "MNQ1!", 1, 1.0, "SECRET", "DEMO1", 20, 40))
    assert "SECRET" not in str(p)


@pytest.mark.parametrize("account", ["MFFU_EVAL_EXAMPLE", "12345", ""])
def test_refuses_non_demo_accounts(monkeypatch, account):
    monkeypatch.setenv("PICKMYTRADE_WEBHOOK", "https://example.invalid")
    monkeypatch.setenv("PICKMYTRADE_TOKEN", "t")
    monkeypatch.setenv("PMT_DEMO_ACCOUNT_ID", account)
    assert bt.main([]) == 2


def test_refuses_wrong_symbol(monkeypatch):
    monkeypatch.setenv("PICKMYTRADE_WEBHOOK", "https://example.invalid")
    monkeypatch.setenv("PICKMYTRADE_TOKEN", "t")
    monkeypatch.setenv("PMT_DEMO_ACCOUNT_ID", "DEMO1")
    assert bt.main(["--mnq", "NQ1!"]) == 2


def test_full_session_simulated(monkeypatch, tmp_path, capsys):
    """Walk the whole interactive flow with scripted answers; PMT reads points."""
    monkeypatch.setattr(bt, "RESULTS_FILE", tmp_path / "results.json")
    sent = []

    class FakeResp:
        status = 200

        def read(self):
            return b'{"success": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout):
        import json
        sent.append(json.loads(req.data))
        return FakeResp()

    monkeypatch.setattr(bt.urllib.request, "urlopen", fake_urlopen)
    answers = iter([
        "y",                                   # demo flat?
        # U1 MNQ x1
        "21000", "y", "y", "21000", "20980", "21040", "y",
        # C1
        "y", "y", "y",
        # U2 MNQ x2
        "21002", "y", "y", "21002", "20982", "21042", "y",
        # U3 MES x1
        "6000", "y", "y", "6000", "5980", "6040", "y",
        # C2
        "y", "y", "y",
        # C2b
        "y", "y",
        # C3
        "y", "y", "y", "PMT: no open position, ignored",
        # C4 skip
        "n",
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    bt.Runner("https://example.invalid", "SECRET", "DEMO1", dry_run=False).run("MNQ", "MES")

    out = capsys.readouterr().out
    assert 'SAFETY_SL_MODE = "points"' in out
    assert "(point value x contracts)x too wide" in out
    assert [p["data"] for p in sent] == ["buy", "close", "buy", "buy", "close", "close", "close"]
    saved = (tmp_path / "results.json").read_text()
    assert '"sl_mode": "points"' in saved and "SECRET" not in saved
