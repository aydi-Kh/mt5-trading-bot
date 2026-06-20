# tests/test_lifecycle.py
"""
Tests execution/lifecycle.py against a scriptable FakeBridge that returns
pre-programmed PositionState sequences across multiple poll_cycle() calls.

This is deliberately a multi-cycle test suite, not single-call — the real
bug found during implementation (cycles_in_opened permanently stuck at 0,
and a dead elif branch) only manifests when you trace state across several
consecutive polls, exactly as the live agent will do every 5 minutes.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.bridge import (
    BrokerBridge, OrderRequest, OrderResult, OrderType,
    PositionState, CloseReason, CandleData, TickData,
)
from execution.lifecycle import TradeLifecycleManager, LifecycleStore, TradeState


class FakeBridge(BrokerBridge):
    """
    Minimal scriptable BrokerBridge. `position_states` maps ticket_id to
    a list of PositionState objects consumed one-per-poll-call (in order);
    once exhausted, repeats the last value. `next_ticket_id` controls what
    place_order() hands back.
    """

    def __init__(self):
        self.position_states: dict[int, list[PositionState]] = {}
        self._call_index: dict[int, int] = {}
        self.next_ticket_id = 1000
        self.placed_orders: list[OrderRequest] = []
        self.all_open_positions: list[PositionState] = []
        self.modify_order_calls: list[tuple[int, float, float]] = []
        self.modify_order_should_succeed = True

    def modify_order(self, ticket_id: int, sl: float, tp: float) -> bool:
        self.modify_order_calls.append((ticket_id, sl, tp))
        return self.modify_order_should_succeed

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass

    def get_tick(self, symbol: str) -> TickData:
        raise NotImplementedError("not exercised by lifecycle tests")

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[CandleData]:
        raise NotImplementedError("not exercised by lifecycle tests")

    def place_order(self, request: OrderRequest) -> OrderResult:
        self.placed_orders.append(request)
        ticket = self.next_ticket_id
        self.next_ticket_id += 1
        return OrderResult(success=True, ticket_id=ticket, fill_price=request.price, error_message=None)

    def cancel_order(self, ticket_id: int) -> bool:
        return True

    def get_position_state(self, ticket_id: int) -> PositionState:
        states = self.position_states.get(ticket_id, [])
        idx = self._call_index.get(ticket_id, 0)
        if not states:
            raise KeyError(f"FakeBridge has no scripted states for ticket {ticket_id}")
        state = states[min(idx, len(states) - 1)]
        self._call_index[ticket_id] = idx + 1
        return state

    def get_all_open_positions(self, magic_number: int) -> list[PositionState]:
        return self.all_open_positions


def _make_manager(bridge: FakeBridge) -> tuple[TradeLifecycleManager, str]:
    tmp_dir = tempfile.mkdtemp()
    store_path = str(Path(tmp_dir) / "trades.json")
    store = LifecycleStore(store_path)
    manager = TradeLifecycleManager(bridge=bridge, store=store, magic_number=20260615)
    return manager, store_path


def _order_request(symbol="EURUSD") -> OrderRequest:
    return OrderRequest(
        symbol=symbol, order_type=OrderType.LIMIT_BUY, volume=0.1,
        price=1.1000, sl=1.0980, tp=1.1040, magic_number=20260615, comment="test",
    )


def test_open_trade_starts_in_opened_state():
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    assert trade is not None
    assert trade.state == TradeState.OPENED
    assert trade.cycles_in_opened == 0


def test_open_trade_returns_none_on_placement_failure():
    bridge = FakeBridge()
    bridge.place_order = lambda req: OrderResult(success=False, ticket_id=None, fill_price=None, error_message="rejected")
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    assert trade is None


def test_never_filled_order_stays_opened_not_misclassified_as_closed():
    """
    This directly tests the Option-2 fix: a pending limit order that
    never fills must NOT be auto-classified as CLOSED by poll_cycle just
    because the broker reports is_open=False. That would fabricate a
    close_reason for an order that was never actually a live position.
    State must remain OPENED so T1.8's timeout logic gets a chance to
    explicitly decide (via cancel_pending_trade) what happens to it.
    """
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=False, current_profit=0.0, close_reason=None),
    ]
    manager.poll_cycle()
    t = manager._store.load(ticket)
    assert t.state == TradeState.OPENED, (
        f"expected OPENED to persist for a never-filled order, got {t.state} "
        f"-- auto-closing here would fabricate a close_reason for a trade "
        f"that was never actually live"
    )
    assert t.close_reason is None
    assert t.cycles_in_opened == 1


def test_cancel_pending_trade_closes_with_timeout_reason():
    """T1.8's counterpart to the above: once it decides a pending order
    has exceeded the fill window, calling cancel_pending_trade must
    explicitly transition OPENED -> CLOSED with TIMEOUT_CANCEL."""
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    # Confirm still not filled at the moment T1.8 checks (race-check inside cancel_pending_trade).
    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=False, current_profit=0.0, close_reason=None),
    ]
    result = manager.cancel_pending_trade(ticket)
    assert result is True, "expected successful cancellation of a still-pending, never-filled order"
    t = manager._store.load(ticket)
    assert t.state == TradeState.CLOSED
    assert t.close_reason == CloseReason.TIMEOUT_CANCEL
    assert t.closed_at is not None


def test_cancel_pending_trade_refuses_if_already_filled():
    """Race condition: T1.8 decided to cancel, but the order filled in
    the meantime. cancel_pending_trade must detect this via its own
    broker check and refuse to cancel a live position."""
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    # Broker now reports it filled (is_open=True) by the time cancel is attempted.
    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=0.0, close_reason=None),
    ]
    result = manager.cancel_pending_trade(ticket)
    assert result is False, "expected cancel_pending_trade to refuse cancelling a position that filled in the meantime"
    t = manager._store.load(ticket)
    assert t.state == TradeState.OPENED, "state must be untouched by a refused cancellation"


def test_cancel_pending_trade_no_op_on_non_opened_state():
    """Calling cancel_pending_trade on a trade that's already FILLED (or
    any non-OPENED state) must be a safe no-op, not an error and not a
    state mutation -- defends against a caller invoking it out of order."""
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=0.0, close_reason=None),
    ]
    manager.poll_cycle()  # OPENED -> FILLED
    t_before = manager._store.load(ticket)
    assert t_before.state == TradeState.FILLED

    result = manager.cancel_pending_trade(ticket)
    assert result is False
    t_after = manager._store.load(ticket)
    assert t_after.state == TradeState.FILLED, "state must be unchanged"


def test_fill_then_monitor_then_close_via_sl():
    """Re-run with position_states scripted so the FIRST poll already
    shows is_open=True (order filled immediately), to test the actual
    FILLED -> MONITORED -> CLOSED path cleanly, separate from the
    pending-order ambiguity surfaced in the previous test."""
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=0.0, close_reason=None),   # cycle 1: filled
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=5.0, close_reason=None),   # cycle 2: still open
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=False, current_profit=-12.0, close_reason=CloseReason.SL_HIT),  # cycle 3: SL hit
    ]

    changed1 = manager.poll_cycle()
    t1 = manager._store.load(ticket)
    assert t1.state == TradeState.FILLED, f"expected FILLED after cycle 1, got {t1.state}"
    assert len(changed1) == 1 and changed1[0].ticket_id == ticket

    changed2 = manager.poll_cycle()
    t2 = manager._store.load(ticket)
    assert t2.state == TradeState.MONITORED, f"expected MONITORED after cycle 2, got {t2.state}"

    changed3 = manager.poll_cycle()
    t3 = manager._store.load(ticket)
    assert t3.state == TradeState.CLOSED, f"expected CLOSED after cycle 3, got {t3.state}"
    assert t3.close_reason == CloseReason.SL_HIT
    assert t3.closed_at is not None


def test_cycles_in_opened_increments_while_pending_then_stops():
    """Directly targets the bug found during implementation: the counter
    must actually increment across consecutive polls while state is
    OPENED, not stay frozen at 0."""
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    # Keep it open (filled) for 3 cycles so we pass through OPENED exactly
    # once on cycle 1 (the only cycle where prior_state==OPENED), then
    # FILLED/MONITORED afterward — cycles_in_opened should end at 1, not 0.
    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=0.0, close_reason=None),
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=1.0, close_reason=None),
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=2.0, close_reason=None),
    ]
    manager.poll_cycle()
    manager.poll_cycle()
    manager.poll_cycle()
    t = manager._store.load(ticket)
    assert t.cycles_in_opened == 1, (
        f"expected cycles_in_opened==1 (incremented exactly once, on the "
        f"single cycle where the trade was still OPENED before filling), "
        f"got {t.cycles_in_opened} -- this is the exact counter that was "
        f"found permanently stuck at 0 due to a post-transition state check bug"
    )


def test_mark_reconciled_requires_closed_state():
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    try:
        manager.mark_reconciled(ticket)
        assert False, "expected ValueError -- trade is still OPENED, not CLOSED"
    except ValueError as e:
        assert "OPENED" in str(e) or "opened" in str(e)


def test_mark_reconciled_moves_closed_to_reconciled():
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=0.0, close_reason=None),
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=False, current_profit=-5.0, close_reason=CloseReason.TIMEOUT_CANCEL),
    ]
    manager.poll_cycle()  # OPENED -> FILLED
    manager.poll_cycle()  # FILLED -> CLOSED
    t = manager._store.load(ticket)
    assert t.state == TradeState.CLOSED

    manager.mark_reconciled(ticket)
    t2 = manager._store.load(ticket)
    assert t2.state == TradeState.RECONCILED


def test_reconciled_trades_are_skipped_in_poll_cycle():
    """Once RECONCILED, poll_cycle must not call get_position_state again
    -- this avoids unbounded broker API calls for trades that are fully done."""
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=0.0, close_reason=None),
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=False, current_profit=2.0, close_reason=CloseReason.MANUAL_CLOSE),
    ]
    manager.poll_cycle()  # OPENED -> FILLED
    manager.poll_cycle()  # FILLED -> CLOSED
    manager.mark_reconciled(ticket)

    calls_before = bridge._call_index.get(ticket, 0)
    manager.poll_cycle()  # should be a no-op for this ticket
    calls_after = bridge._call_index.get(ticket, 0)
    assert calls_after == calls_before, (
        f"expected no additional get_position_state calls for a RECONCILED "
        f"trade, but call count went from {calls_before} to {calls_after}"
    )


def test_find_residual_trades_detects_untracked_broker_position():
    """Directly tests the 'zero residual trades' constraint: a position
    open at the broker under our magic number, but never passed through
    open_trade() (simulating e.g. a crash/restart losing local state)."""
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)

    # Simulate: ticket 9999 exists at the broker but was never tracked locally.
    bridge.all_open_positions = [
        PositionState(ticket_id=9999, symbol="GBPUSD", is_open=True, current_profit=3.0, close_reason=None),
    ]
    residual = manager.find_residual_trades()
    assert residual == [9999], f"expected [9999] as residual, got {residual}"


def test_find_residual_trades_excludes_tracked_positions():
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    bridge.all_open_positions = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=0.0, close_reason=None),
    ]
    residual = manager.find_residual_trades()
    assert residual == [], f"expected no residuals for a tracked ticket, got {residual}"


def test_store_persists_across_manager_instances():
    """Confirms LifecycleStore actually persists to disk, not just
    in-memory -- a restart scenario must be able to reload tracked trades."""
    bridge = FakeBridge()
    tmp_dir = tempfile.mkdtemp()
    store_path = str(Path(tmp_dir) / "trades.json")

    store1 = LifecycleStore(store_path)
    manager1 = TradeLifecycleManager(bridge=bridge, store=store1, magic_number=20260615)
    trade = manager1.open_trade(_order_request())
    ticket = trade.ticket_id

    # Simulate restart: brand new store + manager instance, same file path.
    store2 = LifecycleStore(store_path)
    manager2 = TradeLifecycleManager(bridge=bridge, store=store2, magic_number=20260615)
    reloaded = store2.load(ticket)
    assert reloaded is not None, "expected trade to survive a simulated restart via the JSON store"
    assert reloaded.state == TradeState.OPENED


def test_move_to_breakeven_succeeds_on_monitored_trade():
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=0.0, close_reason=None),
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=15.0, close_reason=None),
    ]
    manager.poll_cycle()  # OPENED -> FILLED
    manager.poll_cycle()  # FILLED -> MONITORED
    t = manager._store.load(ticket)
    assert t.state == TradeState.MONITORED

    result = manager.move_to_breakeven(ticket, breakeven_price=1.1000, current_tp=1.1040)
    assert result is True
    assert bridge.modify_order_calls == [(ticket, 1.1000, 1.1040)]


def test_move_to_breakeven_rejected_on_opened_state():
    """A trade that hasn't even been confirmed filled has no live
    position for the broker to modify -- must reject, not silently
    attempt the call."""
    bridge = FakeBridge()
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    result = manager.move_to_breakeven(ticket, breakeven_price=1.1000, current_tp=1.1040)
    assert result is False
    assert bridge.modify_order_calls == [], "modify_order must not be called for a non-MONITORED trade"


def test_move_to_breakeven_propagates_broker_failure():
    bridge = FakeBridge()
    bridge.modify_order_should_succeed = False
    manager, _ = _make_manager(bridge)
    trade = manager.open_trade(_order_request())
    ticket = trade.ticket_id

    bridge.position_states[ticket] = [
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=0.0, close_reason=None),
        PositionState(ticket_id=ticket, symbol="EURUSD", is_open=True, current_profit=15.0, close_reason=None),
    ]
    manager.poll_cycle()
    manager.poll_cycle()

    result = manager.move_to_breakeven(ticket, breakeven_price=1.1000, current_tp=1.1040)
    assert result is False, "expected broker-side modify failure to propagate as False, not be swallowed"


def run_all():
    tests = [
        test_open_trade_starts_in_opened_state,
        test_open_trade_returns_none_on_placement_failure,
        test_never_filled_order_stays_opened_not_misclassified_as_closed,
        test_cancel_pending_trade_closes_with_timeout_reason,
        test_cancel_pending_trade_refuses_if_already_filled,
        test_cancel_pending_trade_no_op_on_non_opened_state,
        test_fill_then_monitor_then_close_via_sl,
        test_cycles_in_opened_increments_while_pending_then_stops,
        test_mark_reconciled_requires_closed_state,
        test_mark_reconciled_moves_closed_to_reconciled,
        test_reconciled_trades_are_skipped_in_poll_cycle,
        test_find_residual_trades_detects_untracked_broker_position,
        test_find_residual_trades_excludes_tracked_positions,
        test_store_persists_across_manager_instances,
        test_move_to_breakeven_succeeds_on_monitored_trade,
        test_move_to_breakeven_rejected_on_opened_state,
        test_move_to_breakeven_propagates_broker_failure,
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
