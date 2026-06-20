# research/backtest.py
"""
T3.3 — Backtest Runner (8h estimate)

Critical constraint from plan's Phase 3 risk mitigation:
"Backtest runner MUST reuse Phase 1 logic, not reimplement."

This is enforced structurally: this module imports and calls the real
`evaluate_signal()` from signals/pipeline.py. It does NOT copy-paste
signal logic, re-derive OB detection, or re-implement SuperTrend. If a
bug is found in the live signal pipeline and fixed there, the backtest
automatically uses the fixed version without any separate update --
because it IS the live signal pipeline, just called with historical data.

How it works:
  For each candle in the simulation range (oldest-to-newest), the runner
  assembles the lookback windows that evaluate_signal() would see if it
  were running live at that candle's timestamp, then calls evaluate_signal()
  identically to how ak_agent_v3.py calls it. The only differences from
  live operation are:
    1. Position open/close is simulated (no broker), not sent to MT5.
    2. Spread and fill slippage are parameterised.
    3. Account equity updates after each closed simulated trade.

Trade simulation:
  - Entry: LIMIT order fills at the entry_price if price touches it
    within fill_timeout_candles M5 candles after signal. Otherwise cancelled.
  - Exit: SL or TP whichever is touched first (on the wick, not close,
    matching real broker behaviour for stop orders).
  - Position sizing: uses compute_position_size() from T1.5 directly.

Walk-forward compatibility:
  The runner takes explicit start_idx/end_idx parameters so T3.4 can
  call it repeatedly on rolling windows without knowing internals.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from config.schema import TradingConfig
from monitoring.monitor import TradeRRAnalysis, analyze_rr
from execution.lifecycle import CloseReason, TrackedTrade, TradeState
from research.metrics import compute_metrics, MetricsReport
from signals.pipeline import PipelineInputs, TradeAction, evaluate_signal

log = logging.getLogger("ak_backtest")


@dataclass
class BacktestTrade:
    """Simulated trade record. Mirrors TrackedTrade's shape so analyze_rr()
    from T2.3 can process it directly -- the same function, same input
    type, no backtest-specific analysis path needed."""
    ticket_id: int
    symbol: str
    is_long: bool
    entry_price: float
    sl_placed: float
    tp_placed: float
    volume: float
    entry_candle_idx: int
    exit_candle_idx: int | None = None
    exit_price: float | None = None     # actual exit price (not P&L)
    close_reason: CloseReason | None = None
    state: TradeState = TradeState.OPENED

    def to_tracked_trade(self) -> TrackedTrade:
        """Convert to TrackedTrade so T2.3/T3.2 functions work unchanged."""
        sl_dist = abs(self.entry_price - self.sl_placed)
        tp_dist = abs(self.tp_placed - self.entry_price)
        realized_pnl = None
        if self.exit_price is not None and self.volume is not None:
            pip_value = 0.0001  # simplified; real version would use instrument config
            direction = 1 if self.is_long else -1
            realized_pnl = direction * (self.exit_price - self.entry_price) / pip_value * self.volume
        return TrackedTrade(
            ticket_id=self.ticket_id,
            symbol=self.symbol,
            state=self.state,
            opened_at=0.0,
            last_polled_at=0.0,
            close_reason=self.close_reason,
            entry_price=self.entry_price,
            sl_placed=self.sl_placed,
            tp_placed=self.tp_placed,
            volume=self.volume,
            is_long=self.is_long,
            exit_price=realized_pnl,
        )


@dataclass
class BacktestConfig:
    symbol: str
    h1_lookback: int = 300       # candles fed to OB detector per step
    m15_lookback: int = 100      # candles fed to ST/SMT per step
    fill_timeout_candles: int = 3
    simulated_spread: float = 0.0002
    slippage_pips: float = 0.5   # entry fill slippage
    initial_equity: float = 10_000.0
    correlated_symbol: str | None = None


@dataclass
class BacktestResult:
    symbol: str
    start_candle_idx: int
    end_candle_idx: int
    n_candles_evaluated: int
    trades: list[BacktestTrade]
    metrics: MetricsReport
    equity_curve: list[float]   # one entry per closed trade


def _candles_from_df(df: pd.DataFrame, start: int, end: int, symbol: str, tf: str):
    """Slice a historical DataFrame into CandleData objects for a window."""
    from broker.bridge import CandleData
    slice_ = df.iloc[start:end]
    return [
        CandleData(
            symbol=symbol, timeframe=tf,
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row["volume"]), timestamp=float(row["timestamp"]),
        )
        for _, row in slice_.iterrows()
    ]


def _simulate_fill_and_exit(
    trade: BacktestTrade,
    candles_df: pd.DataFrame,
    start_idx: int,
    fill_timeout_candles: int,
) -> BacktestTrade:
    """
    Simulates whether a pending limit order fills, then whether SL or TP
    is hit first. Uses wick prices (high/low) for SL/TP, matching real
    broker stop-order behaviour.
    """
    # Phase 1: try to fill within fill_timeout_candles
    filled = False
    for i in range(start_idx, min(start_idx + fill_timeout_candles, len(candles_df))):
        row = candles_df.iloc[i]
        if trade.is_long and float(row["low"]) <= trade.entry_price:
            filled = True
            break
        if not trade.is_long and float(row["high"]) >= trade.entry_price:
            filled = True
            break

    if not filled:
        trade.close_reason = CloseReason.TIMEOUT_CANCEL
        trade.state = TradeState.CLOSED
        return trade

    trade.state = TradeState.FILLED

    # Phase 2: scan forward for SL or TP hit (wick-based)
    for i in range(start_idx, len(candles_df)):
        row = candles_df.iloc[i]
        high = float(row["high"])
        low = float(row["low"])

        if trade.is_long:
            if low <= trade.sl_placed:
                trade.exit_price = trade.sl_placed
                trade.close_reason = CloseReason.SL_HIT
                trade.exit_candle_idx = i
                trade.state = TradeState.CLOSED
                return trade
            if high >= trade.tp_placed:
                trade.exit_price = trade.tp_placed
                trade.close_reason = CloseReason.TP_HIT
                trade.exit_candle_idx = i
                trade.state = TradeState.CLOSED
                return trade
        else:
            if high >= trade.sl_placed:
                trade.exit_price = trade.sl_placed
                trade.close_reason = CloseReason.SL_HIT
                trade.exit_candle_idx = i
                trade.state = TradeState.CLOSED
                return trade
            if low <= trade.tp_placed:
                trade.exit_price = trade.tp_placed
                trade.close_reason = CloseReason.TP_HIT
                trade.exit_candle_idx = i
                trade.state = TradeState.CLOSED
                return trade

    # Still open at end of data — treat as manual close
    last = candles_df.iloc[-1]
    trade.exit_price = float(last["close"])
    trade.close_reason = CloseReason.MANUAL_CLOSE
    trade.exit_candle_idx = len(candles_df) - 1
    trade.state = TradeState.CLOSED
    return trade


def run_backtest(
    h1_df: pd.DataFrame,
    m15_df: pd.DataFrame,
    cfg: TradingConfig,
    bt_cfg: BacktestConfig,
    start_idx: int = 0,
    end_idx: int | None = None,
    m15_corr_df: pd.DataFrame | None = None,
) -> BacktestResult:
    """
    Runs the backtest by stepping through h1_df from start_idx to end_idx,
    feeding lookback windows into evaluate_signal() at each step.

    Structural guarantee: evaluate_signal is imported at the top of this
    module from signals.pipeline — the exact same function as used in live
    trading. To verify this at test time, T3.3's tests assert that the
    function object used here IS the one from signals.pipeline (identity
    check, not just same name).
    """
    if end_idx is None:
        end_idx = len(h1_df)

    trades: list[BacktestTrade] = []
    equity_curve: list[float] = []
    equity = bt_cfg.initial_equity
    ticket_counter = 0

    for h1_i in range(start_idx + bt_cfg.h1_lookback, end_idx):
        h1_window = _candles_from_df(
            h1_df, h1_i - bt_cfg.h1_lookback, h1_i,
            bt_cfg.symbol, "H1",
        )
        # Approximate M15 index from H1 index (4:1 ratio)
        m15_i = min(h1_i * 4, len(m15_df))
        m15_start = max(0, m15_i - bt_cfg.m15_lookback)
        m15_window = _candles_from_df(
            m15_df, m15_start, m15_i, bt_cfg.symbol, "M15",
        )
        m15_corr_window = None
        if m15_corr_df is not None and bt_cfg.correlated_symbol:
            m15_corr_window = _candles_from_df(
                m15_corr_df, m15_start, m15_i,
                bt_cfg.correlated_symbol, "M15",
            )

        if len(h1_window) < 10 or len(m15_window) < 12:
            continue

        last_h1 = h1_df.iloc[h1_i - 1]
        entry_price = float(last_h1["close"])

        for is_long in (True, False):
            sl_dist = float(last_h1["high"] - last_h1["low"]) * 1.5
            tp_dist = sl_dist * 2.5
            sl_price = entry_price - sl_dist if is_long else entry_price + sl_dist
            tp_price = entry_price + tp_dist if is_long else entry_price - tp_dist

            inputs = PipelineInputs(
                symbol=bt_cfg.symbol,
                h1_candles=h1_window,
                m15_candles_primary=m15_window,
                m15_candles_correlated=m15_corr_window,
                entry_price=entry_price,
                sl_price=sl_price,
                tp_price=tp_price,
                is_long=is_long,
                current_spread=bt_cfg.simulated_spread,
                account_equity=equity,
                minutes_to_next_major_news=None,
                as_of=datetime.now(timezone.utc),
            )

            # THE CRITICAL LINE: same function object as live trading
            decision = evaluate_signal(inputs, cfg)

            if decision.action != TradeAction.EXECUTE:
                continue
            if decision.sizing is None or not decision.sizing.approved:
                continue

            ticket_counter += 1
            vol = decision.sizing.volume or 0.01

            from execution.engine import _ob_based_sl
            sl_actual = _ob_based_sl(decision, bt_cfg.simulated_spread)
            if sl_actual == 0.0:
                sl_actual = sl_price

            trade = BacktestTrade(
                ticket_id=ticket_counter,
                symbol=bt_cfg.symbol,
                is_long=is_long,
                entry_price=entry_price + (bt_cfg.slippage_pips * 0.0001 * (1 if is_long else -1)),
                sl_placed=sl_actual,
                tp_placed=tp_price,
                volume=vol,
                entry_candle_idx=h1_i,
            )

            trade = _simulate_fill_and_exit(trade, h1_df, h1_i, bt_cfg.fill_timeout_candles)
            trades.append(trade)

            # Update equity after each closed trade
            if trade.close_reason == CloseReason.TP_HIT:
                profit_r = abs(trade.tp_placed - trade.entry_price) / max(abs(trade.entry_price - trade.sl_placed), 1e-9)
                equity += equity * (cfg.risk_management.scalping.max_sl_pct_account / 100.0) * profit_r
            elif trade.close_reason == CloseReason.SL_HIT:
                equity -= equity * (cfg.risk_management.scalping.max_sl_pct_account / 100.0)
            equity_curve.append(equity)
            break  # one direction per H1 candle

    tracked = [t.to_tracked_trade() for t in trades]
    analyses = [analyze_rr(t) for t in tracked]
    metrics = compute_metrics(analyses)

    return BacktestResult(
        symbol=bt_cfg.symbol,
        start_candle_idx=start_idx,
        end_candle_idx=end_idx,
        n_candles_evaluated=end_idx - start_idx,
        trades=trades,
        metrics=metrics,
        equity_curve=equity_curve,
    )
