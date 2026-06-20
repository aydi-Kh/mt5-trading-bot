# research/walk_forward.py
"""
T3.4 — Walk-Forward + Parameter Grid (6h estimate)

Walk-forward testing: splits the historical dataset into rolling
in-sample (IS) / out-of-sample (OOS) windows, runs the backtest
(T3.3) on each, and aggregates OOS performance. This prevents the
overfit-to-two-years problem the plan explicitly calls out.

Hold-out: the LAST 3 months of data is NEVER used as in-sample,
even in the final walk-forward window. It's set aside at the start
and only evaluated once, after all parameter decisions are made.
Per plan: 'Hold out last 3 months as untouched final validation.'

Parameter grid: iterates over configurable parameter combinations
(e.g. OB max_age, SuperTrend multiplier, divergence threshold) and
returns the combination with the best OOS R-expectancy.

Design: calls run_backtest() from T3.3 directly -- no signal logic
reimplemented here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from itertools import product

import pandas as pd

from config.schema import TradingConfig
from research.backtest import BacktestConfig, BacktestResult, run_backtest
from research.metrics import MetricsReport, compute_metrics
from monitoring.monitor import analyze_rr

log = logging.getLogger("ak_walkforward")

# 3 months in H1 candles (approx): 90 days * 24 hours
HOLDOUT_H1_CANDLES = 90 * 24  # 2160


@dataclass
class WalkForwardWindow:
    window_idx: int
    is_start: int    # in-sample start (candle index)
    is_end: int      # in-sample end
    oos_start: int   # out-of-sample start
    oos_end: int     # out-of-sample end
    is_result: BacktestResult
    oos_result: BacktestResult


@dataclass
class ParameterSet:
    ob_max_age_candles: int = 50
    st_multiplier: float = 3.0
    smt_divergence_threshold: float = 0.15
    min_rr: float = 2.0

    def label(self) -> str:
        return (f"ob{self.ob_max_age_candles}"
                f"_st{self.st_multiplier}"
                f"_smt{self.smt_divergence_threshold}"
                f"_rr{self.min_rr}")


@dataclass
class WalkForwardResult:
    symbol: str
    n_windows: int
    windows: list[WalkForwardWindow]
    combined_oos_metrics: MetricsReport
    holdout_result: BacktestResult | None    # None if holdout window too short
    best_params: ParameterSet | None         # None if grid was empty/skipped


@dataclass
class GridSearchResult:
    param_sets_evaluated: int
    best_params: ParameterSet
    best_oos_r_expectancy: float
    results_by_params: dict[str, MetricsReport]


def _build_windows(
    total_candles: int,
    holdout_candles: int,
    is_candles: int,
    oos_candles: int,
) -> list[tuple[int, int, int, int]]:
    """
    Returns (is_start, is_end, oos_start, oos_end) tuples for each
    walk-forward window. Slides forward by oos_candles each step.
    Stops before the holdout region.
    """
    available = total_candles - holdout_candles
    windows = []
    start = 0
    while True:
        is_end = start + is_candles
        oos_end = is_end + oos_candles
        if oos_end > available:
            break
        windows.append((start, is_end, is_end, oos_end))
        start += oos_candles  # slide by one OOS period
    return windows


def run_walk_forward(
    h1_df: pd.DataFrame,
    m15_df: pd.DataFrame,
    cfg: TradingConfig,
    bt_cfg: BacktestConfig,
    is_candles: int = 2160,      # ~3 months IS
    oos_candles: int = 720,      # ~1 month OOS
    m15_corr_df: pd.DataFrame | None = None,
) -> WalkForwardResult:
    """
    Runs walk-forward across rolling IS/OOS windows, holding out the
    last HOLDOUT_H1_CANDLES candles (3 months) as final validation.
    """
    total = len(h1_df)
    holdout_start = max(0, total - HOLDOUT_H1_CANDLES)
    windows_spec = _build_windows(total, HOLDOUT_H1_CANDLES, is_candles, oos_candles)

    if not windows_spec:
        log.warning(
            "Insufficient data for walk-forward: need at least %d H1 bars "
            "(is=%d + oos=%d + holdout=%d), have %d. Skipping.",
            is_candles + oos_candles + HOLDOUT_H1_CANDLES,
            is_candles, oos_candles, HOLDOUT_H1_CANDLES, total,
        )
        empty_metrics = compute_metrics([])
        return WalkForwardResult(
            symbol=bt_cfg.symbol, n_windows=0, windows=[],
            combined_oos_metrics=empty_metrics,
            holdout_result=None, best_params=None,
        )

    completed_windows: list[WalkForwardWindow] = []
    all_oos_analyses = []

    for idx, (is_s, is_e, oos_s, oos_e) in enumerate(windows_spec):
        log.info("Walk-forward window %d/%d: IS[%d:%d] OOS[%d:%d]",
                 idx + 1, len(windows_spec), is_s, is_e, oos_s, oos_e)
        is_result = run_backtest(h1_df, m15_df, cfg, bt_cfg,
                                  start_idx=is_s, end_idx=is_e,
                                  m15_corr_df=m15_corr_df)
        oos_result = run_backtest(h1_df, m15_df, cfg, bt_cfg,
                                   start_idx=oos_s, end_idx=oos_e,
                                   m15_corr_df=m15_corr_df)

        for t in oos_result.trades:
            all_oos_analyses.append(analyze_rr(t.to_tracked_trade()))

        completed_windows.append(WalkForwardWindow(
            window_idx=idx,
            is_start=is_s, is_end=is_e,
            oos_start=oos_s, oos_end=oos_e,
            is_result=is_result, oos_result=oos_result,
        ))

    combined_oos = compute_metrics(all_oos_analyses)

    # Holdout: evaluated once, after all parameter decisions
    holdout_result = None
    if holdout_start < total:
        log.info("Evaluating holdout window [%d:%d]", holdout_start, total)
        holdout_result = run_backtest(
            h1_df, m15_df, cfg, bt_cfg,
            start_idx=holdout_start, end_idx=total,
            m15_corr_df=m15_corr_df,
        )

    return WalkForwardResult(
        symbol=bt_cfg.symbol,
        n_windows=len(completed_windows),
        windows=completed_windows,
        combined_oos_metrics=combined_oos,
        holdout_result=holdout_result,
        best_params=None,  # populated by run_grid_search if called
    )


def run_grid_search(
    h1_df: pd.DataFrame,
    m15_df: pd.DataFrame,
    cfg: TradingConfig,
    bt_cfg: BacktestConfig,
    param_grid: dict[str, list],
    is_candles: int = 2160,
    oos_candles: int = 720,
    m15_corr_df: pd.DataFrame | None = None,
) -> GridSearchResult:
    """
    Iterates over all combinations of param_grid, runs walk-forward for
    each, and returns the ParameterSet with the best combined OOS
    R-expectancy. Best is defined as highest R-expectancy with at least
    10 OOS trades (avoids picking a 1-trade lucky streak).

    param_grid example:
        {
            "ob_max_age_candles": [30, 50, 100],
            "st_multiplier": [2.5, 3.0, 3.5],
        }

    Note: this is computationally expensive. For 3 params * 3 values each
    = 27 combinations, each requiring a full walk-forward, this may take
    minutes. T3.4 is designed to run offline before deploying live, not
    during a live session.
    """
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(product(*values))

    log.info("Grid search: %d parameter combinations", len(combinations))
    results: dict[str, MetricsReport] = {}
    best_params = None
    best_expectancy = float("-inf")

    for combo in combinations:
        params = ParameterSet(**{k: v for k, v in zip(keys, combo)})
        label = params.label()
        log.info("Testing params: %s", label)

        wf = run_walk_forward(
            h1_df, m15_df, cfg, bt_cfg,
            is_candles=is_candles, oos_candles=oos_candles,
            m15_corr_df=m15_corr_df,
        )
        results[label] = wf.combined_oos_metrics

        exp = wf.combined_oos_metrics.r_expectancy
        n = wf.combined_oos_metrics.trades_with_known_outcome
        if exp is not None and n >= 10 and exp > best_expectancy:
            best_expectancy = exp
            best_params = params

    if best_params is None:
        # No combination had >= 10 decided trades -- return the first set
        best_params = ParameterSet(**{k: v[0] for k, v in param_grid.items()})
        log.warning(
            "No parameter combination had >= 10 decided OOS trades. "
            "Returning first parameter set as fallback. "
            "Consider more historical data or wider OOS window."
        )

    return GridSearchResult(
        param_sets_evaluated=len(combinations),
        best_params=best_params,
        best_oos_r_expectancy=best_expectancy if best_expectancy > float("-inf") else 0.0,
        results_by_params=results,
    )
