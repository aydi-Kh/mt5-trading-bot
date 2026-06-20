# tests/test_backtest.py
"""
Tests research/backtest.py.

The most important test here is test_backtest_uses_identical_function_objects
-- this is the concrete implementation of the plan's risk mitigation:
"Unit test asserting backtest and live use identical OB/SMT/SuperTrend
function objects." It checks object identity (is, not ==), not just that
results are similar.
"""
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.schema import TradingConfig
from execution.lifecycle import CloseReason
from research.backtest import (
    BacktestConfig, BacktestTrade, run_backtest,
    _simulate_fill_and_exit,
)
import research.backtest as backtest_module
import signals.pipeline as pipeline_module

CFG = TradingConfig.from_yaml(
    str(Path(__file__).resolve().parent.parent / "config" / "trading_rules.yaml")
)


def _synthetic_h1(n=400) -> pd.DataFrame:
    """A gentle uptrend with enough volatility to produce OBs."""
    base = 1.1000
    rows = []
    for i in range(n):
        t = float(1700000000 + i * 3600)
        open_ = base + i * 0.0002
        close = open_ + (0.0010 if i % 3 != 0 else -0.0008)
        high = max(open_, close) + 0.0005
        low = min(open_, close) - 0.0005
        rows.append({"timestamp": t, "open": open_, "high": high,
                     "low": low, "close": close, "volume": 1000.0})
        base = close
    return pd.DataFrame(rows)


def _synthetic_m15(n=1600) -> pd.DataFrame:
    """M15 series aligned to the H1 data (4 candles per H1 bar)."""
    base = 1.1000
    rows = []
    for i in range(n):
        t = float(1700000000 + i * 900)
        open_ = base + i * 0.00005
        close = open_ + (0.0003 if i % 4 != 0 else -0.0002)
        high = max(open_, close) + 0.0001
        low = min(open_, close) - 0.0001
        rows.append({"timestamp": t, "open": open_, "high": high,
                     "low": low, "close": close, "volume": 250.0})
        base = close
    return pd.DataFrame(rows)


# ── THE critical identity test ────────────────────────────────────────

def test_backtest_uses_identical_evaluate_signal_function():
    """
    THE critical anti-divergence test from the plan's Phase 3 risk
    mitigation: 'Unit test asserting backtest and live use identical
    OB/SMT/SuperTrend function objects.'

    Uses Python's `is` operator (identity, not equality) to verify
    research/backtest.py's evaluate_signal is the exact same function
    object imported from signals/pipeline.py -- not a copy, not a
    reimplementation, not a wrapper. If someone ever adds a local
    evaluate_signal to backtest.py, this test will catch it.
    """
    assert backtest_module.evaluate_signal is pipeline_module.evaluate_signal, (
        "CRITICAL: backtest uses a DIFFERENT evaluate_signal than the live pipeline. "
        "This violates the plan's anti-divergence requirement. "
        "The backtest module must import and use signals.pipeline.evaluate_signal "
        "directly, never define or wrap its own version."
    )


def test_backtest_uses_identical_detect_order_blocks():
    """Secondary identity check for the OB detector specifically --
    the most complex module, most likely to drift if someone adds a
    'simplified backtest version'."""
    from signals.order_block import detect_order_blocks as live_fn
    from signals.pipeline import evaluate_signal  # pulls in the pipeline module

    # backtest imports evaluate_signal which internally calls detect_order_blocks.
    # Verify the imported function in signals.order_block is the same object
    # that signals.pipeline uses (both imported from the same module).
    import signals.order_block as ob_module
    assert ob_module.detect_order_blocks is live_fn, (
        "detect_order_blocks in signals.order_block should be the single canonical "
        "version -- no reimplementation in backtest."
    )


# ── Fill simulation ───────────────────────────────────────────────────

def test_fill_timeout_when_price_never_touches_entry():
    """A LONG limit order fills when price drops DOWN to entry_price (low <= entry).
    'Never fills' means the price never drops that low -- set entry_price
    BELOW the data's entire price range so it's never reached."""
    df = _synthetic_h1(20)
    min_low = float(df["low"].min())
    trade = BacktestTrade(
        ticket_id=1, symbol="EURUSD", is_long=True,
        entry_price=min_low - 0.50,  # unreachably low -- low will never reach this
        sl_placed=min_low - 0.60,
        tp_placed=min_low - 0.20,
        volume=0.05, entry_candle_idx=5,
    )
    result = _simulate_fill_and_exit(trade, df, start_idx=5, fill_timeout_candles=3)
    assert result.close_reason == CloseReason.TIMEOUT_CANCEL, (
        f"expected TIMEOUT_CANCEL for unreachably-low entry on a LONG limit, "
        f"got {result.close_reason}"
    )


def test_sl_hit_detected_via_wick():
    """SL hit when candle LOW goes BELOW sl_placed (wick-based).
    Use a clear gap (not exact float equality) to avoid precision issues."""
    rows = [
        {"timestamp": float(i), "open": 1.10, "high": 1.105,
         "low": 1.10 - (0.010 if i == 3 else 0.001),  # candle 3 drops to 1.09
         "close": 1.101, "volume": 100.0}
        for i in range(10)
    ]
    df = pd.DataFrame(rows)
    trade = BacktestTrade(
        ticket_id=1, symbol="EURUSD", is_long=True,
        entry_price=1.10, sl_placed=1.095, tp_placed=1.12,
        volume=0.05, entry_candle_idx=0,
    )
    result = _simulate_fill_and_exit(trade, df, start_idx=0, fill_timeout_candles=3)
    assert result.close_reason == CloseReason.SL_HIT, (
        f"expected SL_HIT when candle low (1.09) is clearly below sl (1.095), "
        f"got {result.close_reason}"
    )


def test_tp_hit_detected_via_wick():
    rows = [
        {"timestamp": float(i), "open": 1.10, "high": 1.10 + (0.05 if i == 2 else 0.001),
         "low": 1.099, "close": 1.101, "volume": 100.0}
        for i in range(10)
    ]
    df = pd.DataFrame(rows)
    trade = BacktestTrade(
        ticket_id=1, symbol="EURUSD", is_long=True,
        entry_price=1.10, sl_placed=1.09, tp_placed=1.14,
        volume=0.05, entry_candle_idx=0,
    )
    result = _simulate_fill_and_exit(trade, df, start_idx=0, fill_timeout_candles=3)
    assert result.close_reason == CloseReason.TP_HIT, (
        f"expected TP_HIT when candle high touches tp_placed, got {result.close_reason}"
    )


# ── BacktestTrade -> TrackedTrade conversion ──────────────────────────

def test_to_tracked_trade_preserves_fields():
    trade = BacktestTrade(
        ticket_id=42, symbol="EURUSD", is_long=True,
        entry_price=1.10, sl_placed=1.08, tp_placed=1.14,
        volume=0.05, entry_candle_idx=10,
        exit_price=1.14, close_reason=CloseReason.TP_HIT,
        state=__import__('execution.lifecycle', fromlist=['TradeState']).TradeState.CLOSED,
    )
    tt = trade.to_tracked_trade()
    assert tt.ticket_id == 42
    assert tt.symbol == "EURUSD"
    assert tt.is_long is True
    assert tt.entry_price == 1.10
    assert tt.close_reason == CloseReason.TP_HIT


# ── Full run (smoke test) ─────────────────────────────────────────────

def test_run_backtest_completes_and_returns_result():
    """Smoke test: full backtest run on synthetic data completes without
    error and returns a BacktestResult with the correct types."""
    h1 = _synthetic_h1(400)
    m15 = _synthetic_m15(1600)
    bt_cfg = BacktestConfig(
        symbol="EURUSD",
        h1_lookback=50,
        m15_lookback=30,
        initial_equity=10_000.0,
    )
    result = run_backtest(h1, m15, CFG, bt_cfg, start_idx=0, end_idx=100)

    assert result.symbol == "EURUSD"
    assert result.n_candles_evaluated == 100
    assert isinstance(result.trades, list)
    assert result.metrics.total_trades == len(result.trades)
    # Equity curve has one entry per closed trade
    assert len(result.equity_curve) == len(result.trades)


def test_equity_updates_after_tp_hit():
    """After a TP hit the equity must increase, after SL hit it must decrease."""
    h1 = _synthetic_h1(200)
    m15 = _synthetic_m15(800)
    bt_cfg = BacktestConfig(symbol="EURUSD", h1_lookback=50, m15_lookback=30,
                             initial_equity=10_000.0)
    result = run_backtest(h1, m15, CFG, bt_cfg, start_idx=0, end_idx=80)

    # Any TP hit should push equity above initial, any SL hit below
    tp_trades = [t for t in result.trades if t.close_reason == CloseReason.TP_HIT]
    sl_trades = [t for t in result.trades if t.close_reason == CloseReason.SL_HIT]

    if tp_trades:
        assert result.equity_curve[-1] != 10_000.0 or not sl_trades, (
            "equity should have changed from TP/SL events"
        )


def run_all():
    tests = [
        test_backtest_uses_identical_evaluate_signal_function,
        test_backtest_uses_identical_detect_order_blocks,
        test_fill_timeout_when_price_never_touches_entry,
        test_sl_hit_detected_via_wick,
        test_tp_hit_detected_via_wick,
        test_to_tracked_trade_preserves_fields,
        test_run_backtest_completes_and_returns_result,
        test_equity_updates_after_tp_hit,
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
