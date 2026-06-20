# research/metrics.py
"""
T3.2 — Metrics Engine (5h estimate)

Computes aggregate performance statistics from a list of
TradeRRAnalysis objects (produced by T2.3's analyze_rr()).

Key design constraint from the plan's Phase 3 risk note:
"Unit test asserting backtest and live use identical OB/SMT/SuperTrend
function objects." This module is consumed by BOTH T3.3 (backtest) and
the live monitoring dashboard -- the same MetricsReport dataclass is
returned regardless of whether the input came from historical simulation
or from live trades. This structural equality is the concrete enforcement
of the "no backtest/live divergence" requirement.

Metrics computed:
  WR   -- Win Rate (TP_HIT / total closed trades with known outcome)
  PF   -- Profit Factor (gross wins / gross losses by R, not $P&L,
          since position sizes vary and R-based PF is more meaningful
          for evaluating the strategy independent of account size)
  MaxDD -- Maximum Drawdown (peak-to-trough in R units, not $)
  R-Expectancy -- Expected R per trade = (WR * avg_R_win) - (LR * avg_R_loss)
                  where LR = 1 - WR, avg_R_win = avg realized R on wins,
                  avg_R_loss = 1.0 (SL was hit = 1R lost per definition)
"""
from __future__ import annotations

from dataclasses import dataclass

from monitoring.monitor import TradeRRAnalysis
from execution.lifecycle import CloseReason


@dataclass(frozen=True)
class MetricsReport:
    total_trades: int
    trades_with_known_outcome: int   # excludes TIMEOUT/CONNECTION_LOST
    win_rate_pct: float
    profit_factor: float | None      # None if no losses (avoid div-by-zero)
    max_drawdown_r: float            # peak-to-trough in R units
    r_expectancy: float | None       # None if insufficient data
    avg_r_win: float | None
    avg_r_loss: float                # always 1.0 by definition (SL = 1R)
    consecutive_losses_max: int
    sharpe_r: float | None           # simplified: mean_r / std_r, None if <2 trades


def compute_metrics(analyses: list[TradeRRAnalysis]) -> MetricsReport:
    """
    Computes all metrics from a list of TradeRRAnalysis objects.
    Trades where close_reason is neither TP_HIT nor SL_HIT (e.g.
    TIMEOUT_CANCEL, CONNECTION_LOST, MANUAL_CLOSE) are counted in
    total_trades but excluded from WR/PF/expectancy calculations --
    their outcome doesn't reflect strategy performance.
    """
    total = len(analyses)
    if total == 0:
        return MetricsReport(
            total_trades=0, trades_with_known_outcome=0,
            win_rate_pct=0.0, profit_factor=None, max_drawdown_r=0.0,
            r_expectancy=None, avg_r_win=None, avg_r_loss=1.0,
            consecutive_losses_max=0, sharpe_r=None,
        )

    # Only include trades with a definitive SL or TP outcome
    decided = [a for a in analyses if a.hit_tp or a.hit_sl]
    wins = [a for a in decided if a.hit_tp]
    losses = [a for a in decided if a.hit_sl]

    n_decided = len(decided)
    wr = round(len(wins) / n_decided * 100, 2) if n_decided > 0 else 0.0

    # R values: wins produce target_rr R, losses produce 1.0 R (by definition)
    r_wins = [a.target_rr for a in wins if a.target_rr is not None]
    avg_r_win = round(sum(r_wins) / len(r_wins), 3) if r_wins else None

    gross_wins_r = sum(r_wins)
    gross_losses_r = float(len(losses))  # each loss = exactly 1R
    pf = round(gross_wins_r / gross_losses_r, 3) if gross_losses_r > 0 else None

    # R-expectancy: E[R] per trade
    if avg_r_win is not None and n_decided > 0:
        lr = 1.0 - (len(wins) / n_decided)
        expectancy = round((len(wins) / n_decided) * avg_r_win - lr * 1.0, 3)
    else:
        expectancy = None

    # MaxDD in R units: simulate equity curve as R accumulation
    max_dd = _max_drawdown_r(analyses)

    # Consecutive losses (from full list including wins, in order)
    max_consec = _max_consecutive_losses(analyses)

    # Simplified Sharpe in R: mean / std of per-trade R outcomes
    r_series = _build_r_series(analyses)
    sharpe = _sharpe_r(r_series)

    return MetricsReport(
        total_trades=total,
        trades_with_known_outcome=n_decided,
        win_rate_pct=wr,
        profit_factor=pf,
        max_drawdown_r=max_dd,
        r_expectancy=expectancy,
        avg_r_win=avg_r_win,
        avg_r_loss=1.0,
        consecutive_losses_max=max_consec,
        sharpe_r=sharpe,
    )


def _build_r_series(analyses: list[TradeRRAnalysis]) -> list[float]:
    """Per-trade R outcomes: +target_rr for wins, -1.0 for SL hits,
    0.0 for timeout/unknown (neutral, doesn't distort mean)."""
    result = []
    for a in analyses:
        if a.hit_tp and a.target_rr is not None:
            result.append(a.target_rr)
        elif a.hit_sl:
            result.append(-1.0)
        else:
            result.append(0.0)
    return result


def _max_drawdown_r(analyses: list[TradeRRAnalysis]) -> float:
    series = _build_r_series(analyses)
    if not series:
        return 0.0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in series:
        equity += r
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 3)


def _max_consecutive_losses(analyses: list[TradeRRAnalysis]) -> int:
    max_run = 0
    current = 0
    for a in analyses:
        if a.hit_sl:
            current += 1
            max_run = max(max_run, current)
        elif a.hit_tp:
            current = 0
        # timeout/unknown: don't reset the counter (ambiguous outcome)
    return max_run


def _sharpe_r(r_series: list[float]) -> float | None:
    if len(r_series) < 2:
        return None
    n = len(r_series)
    mean = sum(r_series) / n
    variance = sum((r - mean) ** 2 for r in r_series) / (n - 1)
    std = variance ** 0.5
    if std == 0:
        return None
    return round(mean / std, 3)
