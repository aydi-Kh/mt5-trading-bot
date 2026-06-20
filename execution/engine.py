# execution/engine.py
"""
T1.8 — M5 Execution Engine (5h estimate)

Two responsibilities, kept separate as methods rather than one combined
function because they run at different frequencies in T1.10's main loop:
  1. submit_trade(decision, lifecycle, cfg, account_equity) -- called when
     T1.7 produces a EXECUTE decision: translates it into an OrderRequest
     with ICT-spec SL/TP placement, calls lifecycle.open_trade().
  2. check_fill_timeouts(lifecycle, cfg) -- called every M5 cycle
     regardless of whether a new signal fired: reads pending-fill trades
     from the lifecycle store and cancels any that have been waiting
     longer than max_fill_candles (default 3 per config.execution).

SL/TP placement logic (the new logic in this module):
  - SL: placed BELOW ob_low for LONG (above ob_high for SHORT). This
    matches the ICT spec: "SL below the Bull OB low" / "above Bear OB high".
    A buffer of half the current spread is added so the SL is not placed
    exactly AT the structural level (a common broker-rejection scenario
    for SL orders that are too close to the current price or to a round
    number the broker's system treats as a restricted zone).
  - TP: the caller (T1.7 pipeline) already computed and validated the
    proposed entry/sl/tp via compute_position_size. T1.8 uses those
    values as-is -- it does NOT recompute TP. This maintains a single
    source of truth for R/R validation (the sizer, which already checked
    it satisfies the minimum R/R before reaching here) rather than
    independently recomputing and risking disagreement.

Lot size: taken directly from SizingResult.volume (already computed
by T1.5's position sizer, which accounts for account equity, risk
profile, and SMT-based size scaling). T1.8 does not recompute lot size.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from broker.bridge import OrderRequest, OrderType
from config.schema import TradingConfig
from execution.lifecycle import TradeLifecycleManager, TrackedTrade, TradeState
from signals.pipeline import TradeDecision, TradeAction


class ExecutionResult(Enum):
    SUBMITTED = "submitted"
    SKIPPED_NOT_EXECUTE = "skipped_not_execute"
    SKIPPED_NO_SIZING = "skipped_no_sizing"
    SKIPPED_NO_OB = "skipped_no_ob"
    PLACEMENT_FAILED = "placement_failed"


@dataclass(frozen=True)
class SubmitOutcome:
    result: ExecutionResult
    trade: TrackedTrade | None
    ticket_id: int | None
    sl_placed: float | None
    tp_placed: float | None
    lot_placed: float | None
    reason: str


def _ob_based_sl(decision: TradeDecision, spread: float) -> float:
    """
    Computes SL price from the OB zone boundaries per ICT spec.
    Buffer = half the current spread, added on the side away from entry
    so the SL isn't sitting exactly on the structural level. Half-spread
    (not a full spread) is intentional: the SL is a stop order, not a
    market order, so only one side of the spread applies -- using the
    full spread would overshoot the protective level unnecessarily.

    If the OB zone is not available (shouldn't happen when decision is
    EXECUTE, but guarded defensively), falls back to 0.0 to signal an
    error the caller can detect rather than silently placing a wrong SL.
    """
    if decision.ob is None:
        return 0.0
    if decision.sizing is None or not decision.sizing.approved:
        return 0.0

    if decision.sizing.volume is not None and decision.ob.ob_type.value == "bullish":
        return round(decision.ob.ob_low - spread / 2, 5)
    else:
        return round(decision.ob.ob_high + spread / 2, 5)


AK_MAGIC = 20260615


def submit_trade(
    *,
    decision: TradeDecision,
    lifecycle: TradeLifecycleManager,
    cfg: TradingConfig,
    current_spread: float,
    entry_price: float,
    tp_price: float,
) -> SubmitOutcome:
    """
    Translates a TradeDecision.EXECUTE into a submitted limit order via
    the lifecycle manager. All validation (R/R, OB freshness, trend
    agreement, auto-reject, position sizing) has already happened in
    T1.7 before this point -- this function trusts the decision and
    focuses on the mechanical translation + submission.

    entry_price: current M5 price for the limit order (not necessarily
        the same as the H1 OB level -- the M5 execution price is the
        caller's responsibility to provide from a live tick).
    """
    if decision.action != TradeAction.EXECUTE:
        return SubmitOutcome(
            result=ExecutionResult.SKIPPED_NOT_EXECUTE,
            trade=None, ticket_id=None,
            sl_placed=None, tp_placed=None, lot_placed=None,
            reason="decision.action is not EXECUTE",
        )
    if decision.sizing is None or not decision.sizing.approved:
        return SubmitOutcome(
            result=ExecutionResult.SKIPPED_NO_SIZING,
            trade=None, ticket_id=None,
            sl_placed=None, tp_placed=None, lot_placed=None,
            reason="sizing not approved or missing",
        )
    if decision.ob is None:
        return SubmitOutcome(
            result=ExecutionResult.SKIPPED_NO_OB,
            trade=None, ticket_id=None,
            sl_placed=None, tp_placed=None, lot_placed=None,
            reason="no OB on decision (required for SL placement)",
        )

    is_long = decision.ob.ob_type.value == "bullish"
    sl = _ob_based_sl(decision, current_spread)
    if sl == 0.0:
        return SubmitOutcome(
            result=ExecutionResult.PLACEMENT_FAILED,
            trade=None, ticket_id=None,
            sl_placed=None, tp_placed=None, lot_placed=None,
            reason="SL computation returned 0.0 (OB or sizing missing)",
        )

    lot = decision.sizing.volume
    if lot is None or lot <= 0:
        return SubmitOutcome(
            result=ExecutionResult.PLACEMENT_FAILED,
            trade=None, ticket_id=None,
            sl_placed=None, tp_placed=None, lot_placed=None,
            reason=f"invalid lot size from sizer: {lot}",
        )

    request = OrderRequest(
        symbol=decision.symbol,
        order_type=OrderType.LIMIT_BUY if is_long else OrderType.LIMIT_SELL,
        volume=lot,
        price=entry_price,
        sl=sl,
        tp=tp_price,
        magic_number=AK_MAGIC,
        comment=f"AK_v3_{decision.ob.ob_type.value[:4].upper()}",
    )

    trade = lifecycle.open_trade(request, is_long=is_long)
    if trade is None:
        return SubmitOutcome(
            result=ExecutionResult.PLACEMENT_FAILED,
            trade=None, ticket_id=None,
            sl_placed=sl, tp_placed=tp_price, lot_placed=lot,
            reason="lifecycle.open_trade() returned None (broker rejected)",
        )

    return SubmitOutcome(
        result=ExecutionResult.SUBMITTED,
        trade=trade,
        ticket_id=trade.ticket_id,
        sl_placed=sl,
        tp_placed=tp_price,
        lot_placed=lot,
        reason="",
    )


@dataclass(frozen=True)
class TimeoutOutcome:
    cancelled_ticket_ids: list[int]
    still_pending_ticket_ids: list[int]
    cancel_failed_ticket_ids: list[int]


def check_fill_timeouts(
    lifecycle: TradeLifecycleManager,
    max_fill_candles: int = 3,
) -> TimeoutOutcome:
    """
    Called every M5 cycle. Checks all trades in OPENED (pending fill)
    state and cancels any that have been waiting more than max_fill_candles
    M5 cycles without filling, per config.execution.m5_fill_timeout_candles.

    Uses cycles_in_opened (set by lifecycle.poll_cycle()) rather than
    wall-clock time, so "3 candles" means exactly 3 poll-cycle calls
    have occurred with this trade still in OPENED state -- consistent
    with the M5 candle-based framing in the plan regardless of actual
    wall-clock time between polls (which may drift if MT5 is slow).
    """
    pending = lifecycle.get_trades_pending_fill()

    cancelled: list[int] = []
    still_pending: list[int] = []
    failed: list[int] = []

    for trade in pending:
        if trade.cycles_in_opened >= max_fill_candles:
            success = lifecycle.cancel_pending_trade(trade.ticket_id)
            if success:
                cancelled.append(trade.ticket_id)
            else:
                # cancel_pending_trade returns False if the trade
                # actually filled (race) or broker rejected the cancel.
                # In either case, the next poll_cycle() call will
                # correctly advance its state (OPENED->FILLED or
                # OPENED->CLOSED) -- we don't need to force anything here.
                failed.append(trade.ticket_id)
        else:
            still_pending.append(trade.ticket_id)

    return TimeoutOutcome(
        cancelled_ticket_ids=cancelled,
        still_pending_ticket_ids=still_pending,
        cancel_failed_ticket_ids=failed,
    )
