# tests/test_paper_bridge.py
"""
Tests broker/paper_bridge.py. Uses a FakeDataSource that returns
scripted ticks instead of real MT5 data, so tests run without a live
terminal.

Critical test: fill conditions. A LONG limit fills when ask <= price
(we're buying at ask, which must reach our limit). A SHORT fills when
bid >= price. Getting these backwards would cause fills at wrong prices
in paper trading, undermining the simulation's validity.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.bridge import (
    BrokerBridge, BrokerConnectionError,
    CandleData, CloseReason, OrderRequest, OrderResult,
    OrderType, PositionState, TickData,
)
from broker.paper_bridge import PaperBrokerBridge, PaperOrderStatus
from execution.lifecycle import LifecycleStore, TradeLifecycleManager, TradeState


# ── Shared test infrastructure ────────────────────────────────────────

class FakeDataSource(BrokerBridge):
    """Scripted market data: returns pre-set ticks on demand."""

    def __init__(self):
        self._ticks: dict[str, list[TickData]] = {}
        self._tick_idx: dict[str, int] = {}

    def set_ticks(self, symbol: str, ticks: list[TickData]) -> None:
        self._ticks[symbol] = ticks
        self._tick_idx[symbol] = 0

    def get_tick(self, symbol: str) -> TickData:
        ticks = self._ticks.get(symbol, [])
        idx = self._tick_idx.get(symbol, 0)
        if not ticks:
            raise BrokerConnectionError(f"No ticks scripted for {symbol}")
        tick = ticks[min(idx, len(ticks) - 1)]
        self._tick_idx[symbol] = idx + 1
        return tick

    def connect(self): return True
    def disconnect(self): pass
    def get_candles(self, s, tf, n): return []
    def place_order(self, r): raise NotImplementedError
    def cancel_order(self, t): raise NotImplementedError
    def modify_order(self, t, sl, tp): raise NotImplementedError
    def get_position_state(self, t): raise NotImplementedError
    def get_all_open_positions(self, m): return []


def _tick(sym, bid, ask, ts=None) -> TickData:
    return TickData(symbol=sym, bid=bid, ask=ask, timestamp=ts or time.time())


def _limit_buy_req(price=1.10, sl=1.08, tp=1.14, sym="EURUSD") -> OrderRequest:
    return OrderRequest(
        symbol=sym, order_type=OrderType.LIMIT_BUY, volume=0.05,
        price=price, sl=sl, tp=tp, magic_number=20260615, comment="paper_test",
    )


def _limit_sell_req(price=1.12, sl=1.14, tp=1.08, sym="EURUSD") -> OrderRequest:
    return OrderRequest(
        symbol=sym, order_type=OrderType.LIMIT_SELL, volume=0.05,
        price=price, sl=sl, tp=tp, magic_number=20260615, comment="paper_test",
    )


def _make_paper_bridge(ticks: list[TickData], symbol="EURUSD") -> PaperBrokerBridge:
    source = FakeDataSource()
    source.set_ticks(symbol, ticks)
    bridge = PaperBrokerBridge(source)
    bridge.connect()
    return bridge


# ── ABC compliance ────────────────────────────────────────────────────

def test_paper_bridge_is_brokerbridge_subclass():
    source = FakeDataSource()
    bridge = PaperBrokerBridge(source)
    assert isinstance(bridge, BrokerBridge)


def test_connect_and_disconnect():
    bridge = _make_paper_bridge([_tick("EURUSD", 1.09, 1.091)])
    bridge.disconnect()
    try:
        bridge.get_tick("EURUSD")
        assert False, "expected BrokerConnectionError after disconnect"
    except BrokerConnectionError:
        pass


# ── Fill conditions ────────────────────────────────────────────────────

def test_long_fills_when_ask_reaches_limit_price():
    """LONG limit at 1.10 fills when ASK drops to 1.10 or below.
    First tick: ask=1.11 (above limit, no fill).
    Second tick: ask=1.10 (at limit, fills)."""
    bridge = _make_paper_bridge([
        _tick("EURUSD", 1.109, 1.110),  # ask=1.110 > 1.10 -> no fill
        _tick("EURUSD", 1.099, 1.100),  # ask=1.100 <= 1.10 -> fills
        _tick("EURUSD", 1.099, 1.100),  # position open
    ])
    result = bridge.place_order(_limit_buy_req(price=1.10))
    ticket = result.ticket_id

    state1 = bridge.get_position_state(ticket)
    assert state1.is_open is False, "should not fill when ask > limit"

    state2 = bridge.get_position_state(ticket)
    assert state2.is_open is True, "should fill when ask <= limit"


def test_short_fills_when_bid_reaches_limit_price():
    """SHORT limit at 1.12 fills when BID rises to 1.12 or above."""
    bridge = _make_paper_bridge([
        _tick("EURUSD", 1.110, 1.111),  # bid=1.110 < 1.12 -> no fill
        _tick("EURUSD", 1.120, 1.121),  # bid=1.120 >= 1.12 -> fills
        _tick("EURUSD", 1.120, 1.121),
    ])
    result = bridge.place_order(_limit_sell_req(price=1.12))
    ticket = result.ticket_id

    state1 = bridge.get_position_state(ticket)
    assert state1.is_open is False

    state2 = bridge.get_position_state(ticket)
    assert state2.is_open is True


# ── SL/TP detection ───────────────────────────────────────────────────

def test_long_sl_hit_when_bid_falls_below_sl():
    """For a LONG position, SL fires when BID falls to or below sl_placed."""
    bridge = _make_paper_bridge([
        _tick("EURUSD", 1.099, 1.10),   # fills (ask=1.10 <= limit 1.10)
        _tick("EURUSD", 1.10, 1.101),   # open, no exit
        _tick("EURUSD", 1.079, 1.080),  # bid=1.079 <= sl=1.08 -> SL HIT
    ])
    ticket = bridge.place_order(_limit_buy_req(price=1.10, sl=1.08, tp=1.14)).ticket_id

    bridge.get_position_state(ticket)  # fill
    bridge.get_position_state(ticket)  # open
    state = bridge.get_position_state(ticket)  # sl check

    assert state.is_open is False
    assert state.close_reason == CloseReason.SL_HIT


def test_long_tp_hit_when_ask_rises_above_tp():
    """For a LONG position, TP fires when ASK rises to or above tp_placed."""
    bridge = _make_paper_bridge([
        _tick("EURUSD", 1.099, 1.10),   # fills
        _tick("EURUSD", 1.10, 1.101),   # open
        _tick("EURUSD", 1.139, 1.14),   # ask=1.14 >= tp=1.14 -> TP HIT
    ])
    ticket = bridge.place_order(_limit_buy_req(price=1.10, sl=1.08, tp=1.14)).ticket_id

    bridge.get_position_state(ticket)
    bridge.get_position_state(ticket)
    state = bridge.get_position_state(ticket)

    assert state.is_open is False
    assert state.close_reason == CloseReason.TP_HIT


def test_short_sl_hit_when_ask_rises_above_sl():
    bridge = _make_paper_bridge([
        _tick("EURUSD", 1.120, 1.121),  # fills (bid=1.12 >= limit 1.12)
        _tick("EURUSD", 1.120, 1.121),  # open
        _tick("EURUSD", 1.139, 1.14),   # ask=1.14 >= sl=1.14 -> SL HIT
    ])
    ticket = bridge.place_order(_limit_sell_req(price=1.12, sl=1.14, tp=1.08)).ticket_id
    bridge.get_position_state(ticket)
    bridge.get_position_state(ticket)
    state = bridge.get_position_state(ticket)
    assert state.close_reason == CloseReason.SL_HIT


# ── Cancel before fill ────────────────────────────────────────────────

def test_cancel_pending_order_returns_true():
    bridge = _make_paper_bridge([_tick("EURUSD", 1.12, 1.13)])
    ticket = bridge.place_order(_limit_buy_req(price=1.09)).ticket_id
    # Price hasn't dropped to fill yet
    result = bridge.cancel_order(ticket)
    assert result is True
    order = bridge.get_paper_orders()[ticket]
    assert order.status == PaperOrderStatus.CLOSED
    assert order.close_reason == CloseReason.TIMEOUT_CANCEL


def test_cancel_already_filled_returns_false():
    bridge = _make_paper_bridge([
        _tick("EURUSD", 1.099, 1.10),  # fills
        _tick("EURUSD", 1.10, 1.101),
    ])
    ticket = bridge.place_order(_limit_buy_req(price=1.10)).ticket_id
    bridge.get_position_state(ticket)  # fill
    result = bridge.cancel_order(ticket)
    assert result is False, "cannot cancel an already-filled position"


# ── get_all_open_positions ────────────────────────────────────────────

def test_get_all_open_positions_only_returns_filled():
    bridge = _make_paper_bridge([
        _tick("EURUSD", 1.099, 1.10),  # fills order 1
        _tick("EURUSD", 1.20, 1.21),   # order 2 pending (price too high for fill)
        _tick("EURUSD", 1.099, 1.10),  # for get_all_open_positions call
    ])
    t1 = bridge.place_order(_limit_buy_req(price=1.10)).ticket_id
    t2 = bridge.place_order(_limit_buy_req(price=1.05)).ticket_id  # unfillable at current price

    bridge.get_position_state(t1)  # fills t1

    open_pos = bridge.get_all_open_positions(20260615)
    tickets = [p.ticket_id for p in open_pos]
    assert t1 in tickets, "filled order must appear in open positions"
    assert t2 not in tickets, "pending order must not appear in open positions"


# ── Lifecycle integration ─────────────────────────────────────────────

def test_paper_bridge_integrates_with_lifecycle_manager():
    """Confirms PaperBrokerBridge works as a drop-in for TradeLifecycleManager
    -- the critical structural guarantee of T3.5."""
    source = FakeDataSource()
    source.set_ticks("EURUSD", [
        _tick("EURUSD", 1.099, 1.10),   # tick 1: fills on poll 1
        _tick("EURUSD", 1.10, 1.101),   # tick 2: open on poll 2
        _tick("EURUSD", 1.139, 1.14),   # tick 3: TP hit on poll 3
        _tick("EURUSD", 1.139, 1.14),   # tick 4: for get_all_open_positions
    ])
    bridge = PaperBrokerBridge(source)
    bridge.connect()

    tmp = tempfile.mkdtemp()
    store = LifecycleStore(str(Path(tmp) / "paper.json"))
    lm = TradeLifecycleManager(bridge=bridge, store=store, magic_number=20260615)

    trade = lm.open_trade(_limit_buy_req(price=1.10, sl=1.08, tp=1.14), is_long=True)
    assert trade is not None

    lm.poll_cycle()  # OPENED -> FILLED (fill tick consumed)
    t = store.load(trade.ticket_id)
    assert t.state == TradeState.FILLED

    lm.poll_cycle()  # FILLED -> MONITORED
    t = store.load(trade.ticket_id)
    assert t.state == TradeState.MONITORED

    lm.poll_cycle()  # TP hit -> CLOSED
    t = store.load(trade.ticket_id)
    assert t.state == TradeState.CLOSED
    assert t.close_reason == CloseReason.TP_HIT


def test_get_paper_summary_counts_correctly():
    bridge = _make_paper_bridge([
        _tick("EURUSD", 1.099, 1.10),
        _tick("EURUSD", 1.099, 1.10),
        _tick("EURUSD", 1.099, 1.10),
        _tick("EURUSD", 1.139, 1.14),
    ])
    t1 = bridge.place_order(_limit_buy_req(price=1.10, tp=1.14)).ticket_id
    t2 = bridge.place_order(_limit_buy_req(price=1.05)).ticket_id   # won't fill

    bridge.get_position_state(t1)   # fills t1
    bridge.get_position_state(t1)   # open
    bridge.get_position_state(t1)   # open
    bridge.get_position_state(t1)   # TP hit

    summary = bridge.get_paper_summary()
    assert summary["tp_hits"] == 1
    assert summary["pending"] == 1


def run_all():
    tests = [
        test_paper_bridge_is_brokerbridge_subclass,
        test_connect_and_disconnect,
        test_long_fills_when_ask_reaches_limit_price,
        test_short_fills_when_bid_reaches_limit_price,
        test_long_sl_hit_when_bid_falls_below_sl,
        test_long_tp_hit_when_ask_rises_above_tp,
        test_short_sl_hit_when_ask_rises_above_sl,
        test_cancel_pending_order_returns_true,
        test_cancel_already_filled_returns_false,
        test_get_all_open_positions_only_returns_filled,
        test_paper_bridge_integrates_with_lifecycle_manager,
        test_get_paper_summary_counts_correctly,
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
