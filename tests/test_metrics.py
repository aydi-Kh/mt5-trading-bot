# tests/test_metrics.py
"""
Tests research/metrics.py. Focus: correctness of R-based metrics with
known synthetic trade sequences, and correct handling of timeout/unknown
outcomes (excluded from WR/PF/expectancy, do not reset consecutive-loss
counter).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.lifecycle import CloseReason
from monitoring.monitor import TradeRRAnalysis
from research.metrics import compute_metrics, MetricsReport


def _win(rr=2.0, sym="EURUSD") -> TradeRRAnalysis:
    return TradeRRAnalysis(
        ticket_id=1, symbol=sym, is_long=True,
        entry_price=1.10, sl_placed=1.08, tp_placed=1.14,
        exit_pnl=20.0, close_reason=CloseReason.TP_HIT.value,
        sl_distance=0.02, tp_distance=0.04, target_rr=rr,
        realized_pnl=20.0, hit_tp=True, hit_sl=False,
    )


def _loss(sym="EURUSD") -> TradeRRAnalysis:
    return TradeRRAnalysis(
        ticket_id=2, symbol=sym, is_long=True,
        entry_price=1.10, sl_placed=1.08, tp_placed=1.14,
        exit_pnl=-10.0, close_reason=CloseReason.SL_HIT.value,
        sl_distance=0.02, tp_distance=0.04, target_rr=2.0,
        realized_pnl=-10.0, hit_tp=False, hit_sl=True,
    )


def _timeout() -> TradeRRAnalysis:
    return TradeRRAnalysis(
        ticket_id=3, symbol="EURUSD", is_long=True,
        entry_price=1.10, sl_placed=1.08, tp_placed=1.14,
        exit_pnl=0.0, close_reason=CloseReason.TIMEOUT_CANCEL.value,
        sl_distance=0.02, tp_distance=0.04, target_rr=2.0,
        realized_pnl=0.0, hit_tp=False, hit_sl=False,
    )


# ── Empty / single trade ──────────────────────────────────────────────

def test_empty_input_returns_zero_report():
    r = compute_metrics([])
    assert r.total_trades == 0
    assert r.win_rate_pct == 0.0
    assert r.profit_factor is None
    assert r.r_expectancy is None


def test_single_win():
    r = compute_metrics([_win(rr=3.0)])
    assert r.total_trades == 1
    assert r.win_rate_pct == 100.0
    assert r.profit_factor is None  # no losses -> PF undefined
    assert r.avg_r_win == 3.0
    assert r.max_drawdown_r == 0.0  # never went negative


def test_single_loss():
    r = compute_metrics([_loss()])
    assert r.total_trades == 1
    assert r.win_rate_pct == 0.0
    assert r.profit_factor == 0.0
    assert r.max_drawdown_r == 1.0  # lost 1R
    assert r.consecutive_losses_max == 1


# ── Win rate ─────────────────────────────────────────────────────────

def test_win_rate_50_pct():
    r = compute_metrics([_win(), _loss()])
    assert r.win_rate_pct == 50.0
    assert r.trades_with_known_outcome == 2


def test_timeout_excluded_from_win_rate():
    """A timeout doesn't count as a win or loss for WR purposes."""
    r = compute_metrics([_win(), _timeout(), _loss()])
    assert r.total_trades == 3
    assert r.trades_with_known_outcome == 2  # only win + loss
    assert r.win_rate_pct == 50.0  # 1 win / 2 decided


# ── Profit Factor ─────────────────────────────────────────────────────

def test_profit_factor_2r_win_vs_1r_loss():
    """2R win, 1R loss -> PF = 2.0 / 1.0 = 2.0"""
    r = compute_metrics([_win(rr=2.0), _loss()])
    assert r.profit_factor is not None
    assert abs(r.profit_factor - 2.0) < 1e-9


def test_profit_factor_none_when_no_losses():
    r = compute_metrics([_win(), _win()])
    assert r.profit_factor is None


# ── R-Expectancy ──────────────────────────────────────────────────────

def test_r_expectancy_positive_with_2r_and_50pct_wr():
    """E[R] = 0.5 * 2.0 - 0.5 * 1.0 = 0.5"""
    r = compute_metrics([_win(rr=2.0), _loss()])
    assert r.r_expectancy is not None
    assert abs(r.r_expectancy - 0.5) < 1e-6


def test_r_expectancy_negative_with_1r_and_33pct_wr():
    """E[R] = 0.333 * 1.0 - 0.667 * 1.0 = -0.333"""
    r = compute_metrics([_win(rr=1.0), _loss(), _loss()])
    assert r.r_expectancy is not None
    assert r.r_expectancy < 0


# ── MaxDD ─────────────────────────────────────────────────────────────

def test_max_drawdown_r_three_losses_then_win():
    """3 consecutive losses then a win: equity goes -3R before recovering.
    MaxDD should be 3.0R."""
    trades = [_loss(), _loss(), _loss(), _win(rr=2.0)]
    r = compute_metrics(trades)
    assert abs(r.max_drawdown_r - 3.0) < 1e-9


def test_max_drawdown_r_zero_for_all_wins():
    r = compute_metrics([_win(rr=2.0), _win(rr=3.0)])
    assert r.max_drawdown_r == 0.0


# ── Consecutive losses ────────────────────────────────────────────────

def test_consecutive_losses_max():
    trades = [_win(), _loss(), _loss(), _loss(), _win(), _loss()]
    r = compute_metrics(trades)
    assert r.consecutive_losses_max == 3


def test_timeout_does_not_reset_consecutive_loss_counter():
    """A timeout mid-loss-run must not reset the counter -- its outcome
    is unknown, so it shouldn't break the streak."""
    trades = [_loss(), _timeout(), _loss()]
    r = compute_metrics(trades)
    assert r.consecutive_losses_max >= 2, (
        f"expected consec_losses >= 2 (timeout should not reset counter), "
        f"got {r.consecutive_losses_max}"
    )


# ── Sharpe ───────────────────────────────────────────────────────────

def test_sharpe_none_for_single_trade():
    r = compute_metrics([_win()])
    assert r.sharpe_r is None


def test_sharpe_positive_for_consistent_wins():
    trades = [_win(rr=2.0)] * 10
    r = compute_metrics(trades)
    # All same R -> std=0 -> Sharpe undefined (None) because no variance
    assert r.sharpe_r is None


def test_sharpe_meaningful_for_mixed():
    trades = [_win(rr=2.0), _loss(), _win(rr=3.0), _loss(), _win(rr=2.0)]
    r = compute_metrics(trades)
    assert r.sharpe_r is not None


def run_all():
    tests = [
        test_empty_input_returns_zero_report,
        test_single_win,
        test_single_loss,
        test_win_rate_50_pct,
        test_timeout_excluded_from_win_rate,
        test_profit_factor_2r_win_vs_1r_loss,
        test_profit_factor_none_when_no_losses,
        test_r_expectancy_positive_with_2r_and_50pct_wr,
        test_r_expectancy_negative_with_1r_and_33pct_wr,
        test_max_drawdown_r_three_losses_then_win,
        test_max_drawdown_r_zero_for_all_wins,
        test_consecutive_losses_max,
        test_timeout_does_not_reset_consecutive_loss_counter,
        test_sharpe_none_for_single_trade,
        test_sharpe_positive_for_consistent_wins,
        test_sharpe_meaningful_for_mixed,
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
