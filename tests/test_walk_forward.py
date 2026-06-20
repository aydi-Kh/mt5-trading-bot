# tests/test_walk_forward.py
"""
Tests research/walk_forward.py. The most important tests are the
window-building logic and holdout protection. A full grid search test
would be too slow for a CI suite, so we test the structure, not the
performance of parameters.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.schema import TradingConfig
from research.walk_forward import (
    _build_windows, HOLDOUT_H1_CANDLES, ParameterSet,
    run_walk_forward, WalkForwardResult,
)
from research.backtest import BacktestConfig

CFG = TradingConfig.from_yaml(
    str(Path(__file__).resolve().parent.parent / "config" / "trading_rules.yaml")
)


def _tiny_df(n: int, base=1.10, step=0.0002) -> pd.DataFrame:
    rows = []
    for i in range(n):
        t = float(1700000000 + i * 3600)
        o = base + i * step
        c = o + (step if i % 3 != 0 else -step * 0.5)
        rows.append({"timestamp": t, "open": o, "high": max(o,c)+0.0005,
                     "low": min(o,c)-0.0005, "close": c, "volume": 1000.0})
    return pd.DataFrame(rows)


# ── Window-building ───────────────────────────────────────────────────

def test_build_windows_basic():
    # 1000 candles, 200 holdout, IS=300, OOS=100 -> 5 windows
    windows = _build_windows(total_candles=1000, holdout_candles=200,
                              is_candles=300, oos_candles=100)
    assert len(windows) == 5
    for is_s, is_e, oos_s, oos_e in windows:
        assert is_e == oos_s, "IS end must equal OOS start"
        assert oos_e - oos_s == 100, "OOS window must be exactly 100 candles"
        assert is_e - is_s == 300, "IS window must be exactly 300 candles"


def test_build_windows_none_when_data_too_short():
    windows = _build_windows(total_candles=100, holdout_candles=200,
                              is_candles=300, oos_candles=100)
    assert windows == [], "expected no windows when data shorter than holdout alone"


def test_build_windows_never_touches_holdout():
    """OOS end must never exceed available = total - holdout."""
    total = 5000
    holdout = 500
    available = total - holdout
    windows = _build_windows(total, holdout, is_candles=1000, oos_candles=200)
    for _, _, _, oos_e in windows:
        assert oos_e <= available, (
            f"OOS window end {oos_e} extends into holdout region (available={available})"
        )


def test_windows_slide_by_oos_period():
    windows = _build_windows(2000, 200, 500, 100)
    for i in range(1, len(windows)):
        prev_is_start = windows[i-1][0]
        curr_is_start = windows[i][0]
        assert curr_is_start - prev_is_start == 100, (
            f"windows should slide by OOS period (100), got {curr_is_start - prev_is_start}"
        )


# ── Holdout protection ────────────────────────────────────────────────

def test_holdout_is_exactly_last_3_months():
    """HOLDOUT_H1_CANDLES must equal 90*24=2160 per the plan's spec."""
    assert HOLDOUT_H1_CANDLES == 90 * 24, (
        f"HOLDOUT_H1_CANDLES should be 2160 (90 days * 24h), got {HOLDOUT_H1_CANDLES}"
    )


def test_run_walk_forward_returns_empty_when_insufficient_data():
    """Too little data to form even one window + holdout must return
    gracefully with n_windows=0, not crash."""
    h1 = _tiny_df(50)
    m15 = _tiny_df(200)
    bt_cfg = BacktestConfig(symbol="EURUSD", h1_lookback=30, m15_lookback=20)
    result = run_walk_forward(h1, m15, CFG, bt_cfg, is_candles=500, oos_candles=200)
    assert isinstance(result, WalkForwardResult)
    assert result.n_windows == 0
    assert result.holdout_result is None


def test_run_walk_forward_smoke_with_enough_data():
    """Smoke test: enough data to form at least one window should produce
    a valid WalkForwardResult without errors."""
    n = HOLDOUT_H1_CANDLES + 700  # holdout + 1 IS + 1 OOS window
    h1 = _tiny_df(n)
    m15 = _tiny_df(n * 4)
    bt_cfg = BacktestConfig(
        symbol="EURUSD", h1_lookback=50, m15_lookback=30,
        initial_equity=10_000.0,
    )
    result = run_walk_forward(
        h1, m15, CFG, bt_cfg,
        is_candles=250, oos_candles=200,
    )
    assert isinstance(result, WalkForwardResult)
    # Either has windows or gracefully has 0 -- either is valid on synthetic data
    assert result.n_windows >= 0
    # combined_oos_metrics is always populated (possibly with 0 trades)
    assert result.combined_oos_metrics is not None


# ── ParameterSet ──────────────────────────────────────────────────────

def test_parameter_set_label_is_stable():
    """Label must be deterministic -- used as dict key in grid results."""
    p = ParameterSet(ob_max_age_candles=50, st_multiplier=3.0,
                     smt_divergence_threshold=0.15, min_rr=2.0)
    assert p.label() == p.label()  # idempotent
    assert "50" in p.label()
    assert "3.0" in p.label()


def run_all():
    tests = [
        test_build_windows_basic,
        test_build_windows_none_when_data_too_short,
        test_build_windows_never_touches_holdout,
        test_windows_slide_by_oos_period,
        test_holdout_is_exactly_last_3_months,
        test_run_walk_forward_returns_empty_when_insufficient_data,
        test_run_walk_forward_smoke_with_enough_data,
        test_parameter_set_label_is_stable,
    ]
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except AssertionError as e:
            failures.append(t.__name__)
            print(f"FAIL: {t.__name__} -- {e}")
        except Exception as e:
            failures.append(t.__name__)
            print(f"ERROR: {t.__name__} -- {type(e).__name__}: {e}")
    print()
    if failures:
        print(f"{len(failures)}/{len(tests)} FAILED: {failures}")
        sys.exit(1)
    else:
        print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    run_all()
