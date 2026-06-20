# tests/test_engine.py
"""
Tests execution/engine.py (T1.8). Priority:
1. SL placement: must be below OB low for LONG, above OB high for SHORT
   (the ICT spec requirement; getting the direction wrong silently
   produces a valid-looking order with the SL on the wrong side).
2. 3-candle timeout: trades pending fill for >= max_fill_candles
   must be cancelled, not left open indefinitely.
3. Rejection paths: not-EXECUTE decision, no sizing, no OB.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.bridge import (
    BrokerBridge, CandleData, CloseReason, OrderRequest, OrderResult,
    OrderType, PositionState, TickData,
)
from config.schema import TradingConfig
from execution.engine import (
    ExecutionResult, check_fill_timeouts, submit_trade, _ob_based_sl,
)
from execution.lifecycle import LifecycleStore, TradeLifecycleManager, TradeState
from risk.sizer import (
    SMTAlignment, SizingResult, TrendAgreement, SizingRejectionReason,
    TradeProfile,
)
from signals.order_block import OrderBlock, OBType
from signals.order_block_status import OrderBlockAssessment, OBStatus
from signals.pipeline import TradeDecision, TradeAction

CFG = TradingConfig.from_yaml(
    str(Path(__file__).resolve().parent.parent / "config" / "trading_rules.yaml")
)


# ── Shared fixtures ────────────────────────────────────────────────────────

class FakeBridge(BrokerBridge):
    def __init__(self):
        self.placed: list[OrderRequest] = []
        self.cancelled: list[int] = []
        self.next_ticket = 2000
        self.position_states: dict[int, list[PositionState]] = {}
        self._call_idx: dict[int, int] = {}
        self.place_should_fail = False
        self.cancel_should_fail = False

    def connect(self) -> bool: return True
    def disconnect(self) -> None: pass
    def get_tick(self, s): raise NotImplementedError
    def get_candles(self, s, tf, n): raise NotImplementedError
    def modify_order(self, tid, sl, tp): return True
    def get_all_open_positions(self, magic): return []

    def place_order(self, req):
        self.placed.append(req)
        if self.place_should_fail:
            return OrderResult(success=False, ticket_id=None, fill_price=None, error_message="rejected")
        t = self.next_ticket; self.next_ticket += 1
        return OrderResult(success=True, ticket_id=t, fill_price=req.price, error_message=None)

    def cancel_order(self, tid):
        self.cancelled.append(tid)
        return not self.cancel_should_fail

    def get_position_state(self, tid):
        states = self.position_states.get(tid, [])
        idx = self._call_idx.get(tid, 0)
        state = states[min(idx, len(states)-1)] if states else PositionState(
            ticket_id=tid, symbol="EURUSD", is_open=False,
            current_profit=0.0, close_reason=CloseReason.CONNECTION_LOST,
        )
        self._call_idx[tid] = idx + 1
        return state


def _make_lifecycle(bridge):
    tmp = tempfile.mkdtemp()
    store = LifecycleStore(str(Path(tmp) / "trades.json"))
    return TradeLifecycleManager(bridge=bridge, store=store, magic_number=20260615)


def _approved_sizing(volume=0.05):
    return SizingResult(
        approved=True, volume=volume, profile=TradeProfile.SCALPING,
        realized_rr=3.0, size_pct_of_max=100.0, rejection_reason=None,
    )


def _bull_ob():
    return OrderBlock(
        ob_high=1.10, ob_low=1.08, ob_type=OBType.BULLISH,
        bos_candle_index=5, ob_candle_index=4,
        bos_timestamp=0.0, ob_candle_timestamp=0.0,
    )


def _bear_ob():
    return OrderBlock(
        ob_high=1.12, ob_low=1.10, ob_type=OBType.BEARISH,
        bos_candle_index=5, ob_candle_index=4,
        bos_timestamp=0.0, ob_candle_timestamp=0.0,
    )


def _execute_decision(is_long=True):
    ob = _bull_ob() if is_long else _bear_ob()
    assessment = OrderBlockAssessment(order_block=ob, status=OBStatus.FRESH,
                                      max_retrace_pct=0.0, candles_since_formation=3)
    return TradeDecision(
        action=TradeAction.EXECUTE, symbol="EURUSD",
        ob=ob, ob_assessment=assessment,
        trend_agreement=TrendAgreement.AGREE,
        smt_assessment=None, smt_alignment=SMTAlignment.ALIGNED,
        reject_gate=None, sizing=_approved_sizing(), pass_reason="",
    )


# ── SL placement ──────────────────────────────────────────────────────

def test_sl_below_ob_low_for_long():
    """Core ICT spec: SL must be BELOW ob_low for a LONG trade, not above it."""
    dec = _execute_decision(is_long=True)
    spread = 0.0002
    sl = _ob_based_sl(dec, spread)
    assert sl < dec.ob.ob_low, (
        f"expected SL {sl} < ob_low {dec.ob.ob_low} for LONG -- got wrong side"
    )
    assert abs(sl - (dec.ob.ob_low - spread / 2)) < 1e-9


def test_sl_above_ob_high_for_short():
    """Core ICT spec: SL must be ABOVE ob_high for a SHORT trade."""
    dec = _execute_decision(is_long=False)
    spread = 0.0002
    sl = _ob_based_sl(dec, spread)
    assert sl > dec.ob.ob_high, (
        f"expected SL {sl} > ob_high {dec.ob.ob_high} for SHORT -- got wrong side"
    )
    assert abs(sl - (dec.ob.ob_high + spread / 2)) < 1e-9


def test_sl_returns_zero_for_missing_ob():
    dec = TradeDecision(
        action=TradeAction.EXECUTE, symbol="EURUSD",
        ob=None, ob_assessment=None, trend_agreement=TrendAgreement.AGREE,
        smt_assessment=None, smt_alignment=SMTAlignment.ALIGNED,
        reject_gate=None, sizing=_approved_sizing(), pass_reason="",
    )
    assert _ob_based_sl(dec, 0.0002) == 0.0


# ── submit_trade ──────────────────────────────────────────────────────

def test_submit_sends_correct_order_type_for_long():
    bridge = FakeBridge()
    lm = _make_lifecycle(bridge)
    dec = _execute_decision(is_long=True)
    outcome = submit_trade(decision=dec, lifecycle=lm, cfg=CFG,
                           current_spread=0.0002, entry_price=1.18, tp_price=1.22)
    assert outcome.result == ExecutionResult.SUBMITTED
    assert len(bridge.placed) == 1
    assert bridge.placed[0].order_type == OrderType.LIMIT_BUY
    assert bridge.placed[0].volume == 0.05
    assert bridge.placed[0].sl < 1.08  # below ob_low


def test_submit_sends_correct_order_type_for_short():
    bridge = FakeBridge()
    lm = _make_lifecycle(bridge)
    dec = _execute_decision(is_long=False)
    outcome = submit_trade(decision=dec, lifecycle=lm, cfg=CFG,
                           current_spread=0.0002, entry_price=1.10, tp_price=1.06)
    assert outcome.result == ExecutionResult.SUBMITTED
    assert bridge.placed[0].order_type == OrderType.LIMIT_SELL
    assert bridge.placed[0].sl > 1.12  # above ob_high


def test_submit_skipped_for_pass_decision():
    bridge = FakeBridge()
    lm = _make_lifecycle(bridge)
    pass_dec = TradeDecision(
        action=TradeAction.PASS, symbol="EURUSD", ob=_bull_ob(),
        ob_assessment=None, trend_agreement=None, smt_assessment=None,
        smt_alignment=None, reject_gate=None, sizing=None,
        pass_reason="no fresh OB",
    )
    outcome = submit_trade(decision=pass_dec, lifecycle=lm, cfg=CFG,
                           current_spread=0.0002, entry_price=1.18, tp_price=1.22)
    assert outcome.result == ExecutionResult.SKIPPED_NOT_EXECUTE
    assert len(bridge.placed) == 0


def test_submit_fails_when_broker_rejects():
    bridge = FakeBridge()
    bridge.place_should_fail = True
    lm = _make_lifecycle(bridge)
    dec = _execute_decision(is_long=True)
    outcome = submit_trade(decision=dec, lifecycle=lm, cfg=CFG,
                           current_spread=0.0002, entry_price=1.18, tp_price=1.22)
    assert outcome.result == ExecutionResult.PLACEMENT_FAILED
    assert outcome.trade is None


def test_submit_skipped_when_sizing_not_approved():
    bridge = FakeBridge()
    lm = _make_lifecycle(bridge)
    bad_sizing = SizingResult(
        approved=False, volume=None, profile=TradeProfile.SCALPING,
        realized_rr=1.5, size_pct_of_max=None,
        rejection_reason=SizingRejectionReason.INSUFFICIENT_RR,
    )
    dec = TradeDecision(
        action=TradeAction.EXECUTE, symbol="EURUSD", ob=_bull_ob(),
        ob_assessment=None, trend_agreement=TrendAgreement.AGREE,
        smt_assessment=None, smt_alignment=SMTAlignment.ALIGNED,
        reject_gate=None, sizing=bad_sizing, pass_reason="",
    )
    outcome = submit_trade(decision=dec, lifecycle=lm, cfg=CFG,
                           current_spread=0.0002, entry_price=1.18, tp_price=1.22)
    assert outcome.result == ExecutionResult.SKIPPED_NO_SIZING


# ── check_fill_timeouts ───────────────────────────────────────────────

def test_timeout_cancels_trade_after_max_candles():
    """A trade still OPENED after >= max_fill_candles polls must be cancelled."""
    bridge = FakeBridge()
    lm = _make_lifecycle(bridge)
    dec = _execute_decision(is_long=True)

    outcome = submit_trade(decision=dec, lifecycle=lm, cfg=CFG,
                           current_spread=0.0002, entry_price=1.18, tp_price=1.22)
    ticket = outcome.ticket_id

    # Script broker: never fills (always not-open) -- per T1.6c's design,
    # a never-filled OPENED trade stays OPENED (not auto-CLOSED) until
    # cancel_pending_trade is explicitly called.
    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=False,
                      current_profit=0.0, close_reason=None),
    ]

    # Simulate 3 poll cycles -- cycles_in_opened reaches 3.
    for _ in range(3):
        lm.poll_cycle()

    t = lm._store.load(ticket)
    assert t.cycles_in_opened >= 3, f"expected cycles_in_opened>=3, got {t.cycles_in_opened}"

    timeout_result = check_fill_timeouts(lm, max_fill_candles=3)
    assert ticket in timeout_result.cancelled_ticket_ids, (
        f"expected ticket {ticket} to be cancelled after {t.cycles_in_opened} cycles"
    )


def test_timeout_leaves_trade_open_before_max_candles():
    bridge = FakeBridge()
    lm = _make_lifecycle(bridge)
    dec = _execute_decision(is_long=True)
    outcome = submit_trade(decision=dec, lifecycle=lm, cfg=CFG,
                           current_spread=0.0002, entry_price=1.18, tp_price=1.22)
    ticket = outcome.ticket_id

    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=False,
                      current_profit=0.0, close_reason=None),
    ]

    lm.poll_cycle()  # cycles_in_opened = 1, below threshold of 3
    timeout_result = check_fill_timeouts(lm, max_fill_candles=3)
    assert ticket not in timeout_result.cancelled_ticket_ids
    assert ticket in timeout_result.still_pending_ticket_ids


def test_timeout_handles_race_condition_fill_between_decision_and_cancel():
    """If the trade filled just before cancel_pending_trade is called
    (race), the timeout engine must handle the False return gracefully --
    the trade should appear in cancel_failed, not raise an exception."""
    bridge = FakeBridge()
    bridge.cancel_should_fail = True  # simulate: broker says cancel failed (filled)
    lm = _make_lifecycle(bridge)
    dec = _execute_decision(is_long=True)
    outcome = submit_trade(decision=dec, lifecycle=lm, cfg=CFG,
                           current_spread=0.0002, entry_price=1.18, tp_price=1.22)
    ticket = outcome.ticket_id

    # cancel_pending_trade's internal broker check uses get_position_state
    # -- return is_open=False so it attempts cancel (then cancel_should_fail kicks in)
    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=False,
                      current_profit=0.0, close_reason=None),
    ]
    for _ in range(3):
        lm.poll_cycle()

    timeout_result = check_fill_timeouts(lm, max_fill_candles=3)
    assert ticket in timeout_result.cancel_failed_ticket_ids
    assert ticket not in timeout_result.cancelled_ticket_ids


def run_all():
    tests = [
        test_sl_below_ob_low_for_long,
        test_sl_above_ob_high_for_short,
        test_sl_returns_zero_for_missing_ob,
        test_submit_sends_correct_order_type_for_long,
        test_submit_sends_correct_order_type_for_short,
        test_submit_skipped_for_pass_decision,
        test_submit_fails_when_broker_rejects,
        test_submit_skipped_when_sizing_not_approved,
        test_timeout_cancels_trade_after_max_candles,
        test_timeout_leaves_trade_open_before_max_candles,
        test_timeout_handles_race_condition_fill_between_decision_and_cancel,
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
