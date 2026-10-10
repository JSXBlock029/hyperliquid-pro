import math

import pytest

import scalp_agent as agent
import scalp_engine as eng
import scalp_stats as stats


# ---------------- indicators ----------------

def test_ema_matches_hand_computation():
    # k = 2/(3+1) = 0.5: 1 -> 1.5 -> 2.25
    assert eng._ema([1.0, 2.0, 3.0], 3) == pytest.approx(2.25)
    assert eng._ema([], 9) == 0.0


def test_stdev_sample():
    assert eng._stdev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.13809, rel=1e-4)
    assert eng._stdev([5]) == 0.0


@pytest.mark.parametrize("closes,expected", [
    (list(range(1, 30)), 100.0),          # only gains
    (list(range(30, 1, -1)), 0.0),        # only losses
    ([1.0] * 5, 50.0),                    # not enough data -> neutral
])
def test_rsi_extremes(closes, expected):
    assert eng._rsi_wilder([float(c) for c in closes]) == pytest.approx(expected)


def test_rsi_is_bounded_on_mixed_series():
    closes = [100 + 3 * math.sin(i / 2) for i in range(60)]
    assert 0 < eng._rsi_wilder(closes) < 100


def test_percentile_nearest_rank():
    data = [5, 1, 4, 2, 3]
    assert eng._pctile_nr(data, 80) == 4
    assert eng._pctile_nr(data, 100) == 5
    assert eng._pctile_nr([], 50) == 0.0


def test_features_on_steady_uptrend():
    closes = [100 + i for i in range(40)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    f = eng._features(closes, highs, lows)
    assert f["last"] == 139
    assert f["ema9"] > f["ema21"] and f["ema_spread_bps"] > 0
    # true range per bar = max(2, |h - prev_close| = 2, |l - prev_close| = 0) = 2
    assert f["atr_bps"] == pytest.approx(2 / 139 * 10000)


# ---------------- order book ----------------

def _book(bids, asks):
    return {"levels": [[{"px": p, "sz": s} for p, s in bids], [{"px": p, "sz": s} for p, s in asks]]}


def test_ob_imbalance_mid_spread_and_skew():
    mid, spread, imb = eng._ob_imbalance(_book([(99.9, 3), (99.8, 1)], [(100.1, 1)]))
    assert mid == pytest.approx(100.0)
    assert spread == pytest.approx(20.0)        # 0.2 / 100 in bps
    assert imb == pytest.approx((4 - 1) / 5)


def test_ob_imbalance_empty_book_is_neutral():
    assert eng._ob_imbalance({"levels": []}) == (0.0, 0.0, 0.0)
    assert eng._ob_imbalance(_book([], [(1, 1)])) == (0.0, 0.0, 0.0)


@pytest.mark.parametrize("side,imb,expected", [
    ("BUY", 0.5, "favors"), ("BUY", -0.5, "against"), ("BUY", 0.1, "neutral"),
    ("SELL", 0.5, "against"), ("SELL", -0.5, "favors"),
])
def test_book_pressure_is_relative_to_side(side, imb, expected):
    assert eng._book_pressure(side, imb) == expected


# ---------------- pre-trade gate ----------------

BASE = {"realized_vol": 0.01, "ret_z": 1.0, "ema_spread_bps": 5.0}


def test_gate_passes_trend_aligned_move():
    ok, side, _ = eng._gate(dict(BASE), mid=100.0, spread_bps=2.0)
    assert ok and side == "BUY"


@pytest.mark.parametrize("override,mid,spread", [
    ({}, 0.0, 2.0),                                  # no mid
    ({}, 100.0, eng.MAX_SPREAD_BPS + 0.1),           # spread too wide
    ({"realized_vol": eng.MAX_REALIZED_VOL_PCT * 2}, 100.0, 2.0),
    ({"ret_z": eng.MIN_RET_Z / 2}, 100.0, 2.0),      # too quiet
    ({"ret_z": -1.0}, 100.0, 2.0),                   # return disagrees with up-trend
])
def test_gate_blocks(override, mid, spread):
    ok, side, why = eng._gate({**BASE, **override}, mid=mid, spread_bps=spread)
    assert not ok and side == "" and why


# ---------------- LLM decision validation ----------------

GOOD = {"enter": True, "size_pct": 0.4, "stop_bps": 20, "tp_bps": 40, "reason": "ok"}


def test_coerce_accepts_valid_plan():
    d = agent._coerce(dict(GOOD))
    assert d.enter and d.size_pct == 0.4 and d.stop_bps == 20 and d.tp_bps == 40


@pytest.mark.parametrize("bad", [
    {"size_pct": 0}, {"size_pct": 1.5}, {"stop_bps": 2}, {"stop_bps": 500},
    {"tp_bps": 0}, {"tp_bps": 10},                    # tp below stop: reward:risk < 1
    {"size_pct": float("nan")}, {"stop_bps": "lots"},
])
def test_coerce_refuses_out_of_range_momentum_plans(bad):
    assert not agent._coerce({**GOOD, **bad}).enter


def test_coerce_allows_zero_tp_only_for_time_managed_exits():
    plan = {**GOOD, "tp_bps": 0}
    assert not agent._coerce(plan).enter
    assert agent._coerce(plan, allow_zero_tp=True).enter


@pytest.mark.parametrize("value", ["false", "False", "no", "0", "", None, 0, [], "maybe"])
def test_coerce_never_enters_on_a_non_true_enter_value(value):
    """Regression: bool("false") is True, so a model replying "enter": "false" opened a trade."""
    assert not agent._coerce({**GOOD, "enter": value}).enter


@pytest.mark.parametrize("value", [True, "true", "TRUE", "yes", 1])
def test_coerce_accepts_explicit_true_values(value):
    assert agent._coerce({**GOOD, "enter": value}).enter


def test_coerce_rejects_non_object():
    assert not agent._coerce(["enter"]).enter
    assert not agent._coerce(None).enter


@pytest.mark.parametrize("text,expected", [
    ('{"enter": true}', {"enter": True}),
    ('Sure! ```json\n{"enter": false, "reason": "flat"}\n```', {"enter": False, "reason": "flat"}),
    ("no json here", None),
    ("{broken", None),
])
def test_extract_json(text, expected):
    assert agent._extract_json(text) == expected


# ---------------- stats ----------------

def test_bucket_stats():
    s = stats._bucket_stats([{"pnl_usd": 2}, {"pnl_usd": -1}, {"pnl_usd": 3}])
    assert s["trades"] == 3 and s["wins"] == 2 and s["pnl"] == 4
    assert s["win_rate"] == pytest.approx(0.6667, abs=1e-4)
    assert s["best"] == 3 and s["worst"] == -1


def test_bucket_stats_empty():
    assert stats._bucket_stats([])["trades"] == 0


def test_read_exits_skips_garbage(tmp_path, monkeypatch):
    log = tmp_path / "paper.jsonl"
    log.write_text('{"kind":"exit","pnl_usd":1}\nnot json\n{"kind":"entry"}\n\n{"kind":"exit","pnl_usd":-2}\n')
    monkeypatch.setattr(stats, "LOG", log)
    assert [r["pnl_usd"] for r in stats._read_exits()] == [1, -2]


@pytest.mark.parametrize("reply,expected", [
    ('{"exit": "false", "reason": "hold"}', False),   # regression: used to close the position
    ('{"exit": true, "reason": "lock gain"}', True),
    ('{"exit": "no"}', False),
    ("garbage", False),
])
def test_decide_exit_parses_strictly(monkeypatch, reply, expected):
    from types import SimpleNamespace
    monkeypatch.setattr(agent.llm_router, "chat",
                        lambda *a, **k: SimpleNamespace(text=reply, provider="test", model="m"))
    assert agent.decide_exit({"signal": "gigafade", "pnl_state": "flat"})["exit"] is expected


def test_decide_exit_holds_when_no_provider(monkeypatch):
    monkeypatch.setattr(agent.llm_router, "chat", lambda *a, **k: None)
    assert agent.decide_exit({"signal": "momentum"})["exit"] is False
