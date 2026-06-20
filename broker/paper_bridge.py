# broker/paper_bridge.py
"""
T3.5 — Paper Trading Bridge (5h estimate)

PaperBrokerBridge implements BrokerBridge exactly as MetaTrader5Bridge
does, so the T1.6c lifecycle manager, T1.7 pipeline, T1.8 execution
engine and T2.4 reconciliation all work identically in paper mode --
no paper-specific code paths in those modules.

Architecture: delegate get_candles() and get_tick() to a real
MarketDataSource (can be MetaTrader5Bridge or any other BrokerBridge).
This means paper trading receives real live market data but simulates
order execution in memory, which is the correct distinction:

  Real trading:  real data   + real orders   → MT5Bridge (both)
  Paper trading: real data   + simulated orders → PaperBrokerBridge
  Backtesting:   historical data + simulated orders → run_backtest()

Order simulation:
  place_order()  → records the pending order in memory, returns a synthetic
                   ticket ID. Does NOT call any broker API.
  get_position_state() → checks whether the simulated position's SL or TP
                   has been hit by fetching the current tick from the real
                   market data source. If SL/TP hit, marks it closed.
  cancel_order() → removes from pending orders if not yet filled.

Fill simulation: a LIMIT_BUY fills when bid <= order price.
  A LIMIT_SELL fills when ask >= order price. Spread is modelled via
  the current bid/ask from get_tick(), not a fixed assumption.

Limitations (explicit, not hidden):
  - No partial fills simulated.
  - Fill check happens on poll_cycle() calls (same 5s cadence as live),
    not on every tick -- a very fast move could skip a fill that real
    MT5 would have caught. This is a known limitation of discrete polling.
  - P&L is estimated, not exact (uses mid-price, ignores swap/commission).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from broker.bridge import (
    BrokerBridge, BrokerConnectionError,
    CandleData, CloseReason,
    OrderRequest, OrderResult, OrderType,
    PositionState, TickData,
)


class PaperOrderStatus(Enum):
    PENDING = "pending"    # limit not yet filled
    FILLED = "filled"      # limit filled, position open
    CLOSED = "closed"      # SL/TP hit or cancelled


@dataclass
class PaperOrder:
    ticket_id: int
    request: OrderRequest
    status: PaperOrderStatus = PaperOrderStatus.PENDING
    fill_price: float | None = None
    close_price: float | None = None
    close_reason: CloseReason | None = None
    opened_at: float = field(default_factory=time.time)
    closed_at: float | None = None

    @property
    def is_long(self) -> bool:
        return self.request.order_type == OrderType.LIMIT_BUY

    def current_profit_estimate(self, mid_price: float) -> float:
        """Estimated P&L using mid-price. Not commission-adjusted."""
        if self.fill_price is None:
            return 0.0
        direction = 1 if self.is_long else -1
        pip_value = 0.0001
        return direction * (mid_price - self.fill_price) / pip_value * self.request.volume


class PaperBrokerBridge(BrokerBridge):
    """
    Paper trading bridge. Requires a real market_data_source (any
    BrokerBridge that implements get_candles/get_tick, typically
    MetaTrader5Bridge) to be connected before this bridge can serve
    candle or tick data. The market_data_source is only used for data
    retrieval -- its place_order/cancel_order/etc are never called.
    """

    def __init__(self, market_data_source: BrokerBridge) -> None:
        self._data = market_data_source
        self._orders: dict[int, PaperOrder] = {}
        self._next_ticket = 90_000_000  # high range to avoid collision with live tickets
        self._connected = False

    def connect(self) -> bool:
        if not self._data.connect():
            return False
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._data.disconnect()
        self._connected = False

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerConnectionError("PaperBrokerBridge not connected.")

    # ── Market data: delegates to real source ─────────────────────────

    def get_tick(self, symbol: str) -> TickData:
        self._require_connected()
        return self._data.get_tick(symbol)

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[CandleData]:
        self._require_connected()
        return self._data.get_candles(symbol, timeframe, count)

    # ── Order simulation ─────────────────────────────────────────────

    def place_order(self, request: OrderRequest) -> OrderResult:
        self._require_connected()
        ticket = self._next_ticket
        self._next_ticket += 1
        self._orders[ticket] = PaperOrder(ticket_id=ticket, request=request)
        return OrderResult(
            success=True,
            ticket_id=ticket,
            fill_price=None,  # not filled yet; fill happens on next poll
            error_message=None,
        )

    def cancel_order(self, ticket_id: int) -> bool:
        self._require_connected()
        order = self._orders.get(ticket_id)
        if order is None:
            return False
        if order.status == PaperOrderStatus.PENDING:
            order.status = PaperOrderStatus.CLOSED
            order.close_reason = CloseReason.TIMEOUT_CANCEL
            order.closed_at = time.time()
            return True
        return False  # already filled or closed

    def modify_order(self, ticket_id: int, sl: float, tp: float) -> bool:
        self._require_connected()
        order = self._orders.get(ticket_id)
        if order is None or order.status != PaperOrderStatus.FILLED:
            return False
        # Modify in place (OrderRequest is frozen, so swap the request)
        import dataclasses
        new_req = dataclasses.replace(order.request, sl=sl, tp=tp)
        order.request = new_req  # type: ignore[misc]
        return True

    def get_position_state(self, ticket_id: int) -> PositionState:
        """
        Core paper trading simulation: checks whether fill or SL/TP
        conditions are met based on the CURRENT live tick, then updates
        the order status accordingly. Returns a PositionState that the
        lifecycle manager (T1.6c) interprets the same way as a real MT5
        position state.
        """
        self._require_connected()
        order = self._orders.get(ticket_id)
        if order is None:
            return PositionState(
                ticket_id=ticket_id, symbol="UNKNOWN",
                is_open=False, current_profit=0.0,
                close_reason=CloseReason.CONNECTION_LOST,
            )

        if order.status == PaperOrderStatus.CLOSED:
            return PositionState(
                ticket_id=ticket_id, symbol=order.request.symbol,
                is_open=False,
                current_profit=order.close_price or 0.0,
                close_reason=order.close_reason,
            )

        # Fetch current live tick to check fill/exit conditions
        try:
            tick = self._data.get_tick(order.request.symbol)
        except Exception:
            return PositionState(
                ticket_id=ticket_id, symbol=order.request.symbol,
                is_open=False, current_profit=0.0,
                close_reason=CloseReason.CONNECTION_LOST,
            )

        mid = (tick.bid + tick.ask) / 2.0

        if order.status == PaperOrderStatus.PENDING:
            # Check fill: LONG fills when ask (what we buy at) <= limit price
            # SHORT fills when bid (what we sell at) >= limit price
            should_fill = (
                (order.is_long and tick.ask <= order.request.price)
                or (not order.is_long and tick.bid >= order.request.price)
            )
            if should_fill:
                order.fill_price = order.request.price
                order.status = PaperOrderStatus.FILLED
            else:
                return PositionState(
                    ticket_id=ticket_id, symbol=order.request.symbol,
                    is_open=False,  # pending, not yet open position
                    current_profit=0.0, close_reason=None,
                )

        # Check SL/TP on filled position
        assert order.fill_price is not None  # guaranteed by fill logic above

        if order.is_long:
            if tick.bid <= order.request.sl:
                order.status = PaperOrderStatus.CLOSED
                order.close_reason = CloseReason.SL_HIT
                order.close_price = order.request.sl
                order.closed_at = time.time()
                return PositionState(
                    ticket_id=ticket_id, symbol=order.request.symbol,
                    is_open=False,
                    current_profit=order.current_profit_estimate(mid),
                    close_reason=CloseReason.SL_HIT,
                )
            if tick.ask >= order.request.tp:
                order.status = PaperOrderStatus.CLOSED
                order.close_reason = CloseReason.TP_HIT
                order.close_price = order.request.tp
                order.closed_at = time.time()
                return PositionState(
                    ticket_id=ticket_id, symbol=order.request.symbol,
                    is_open=False,
                    current_profit=order.current_profit_estimate(mid),
                    close_reason=CloseReason.TP_HIT,
                )
        else:
            if tick.ask >= order.request.sl:
                order.status = PaperOrderStatus.CLOSED
                order.close_reason = CloseReason.SL_HIT
                order.close_price = order.request.sl
                order.closed_at = time.time()
                return PositionState(
                    ticket_id=ticket_id, symbol=order.request.symbol,
                    is_open=False,
                    current_profit=order.current_profit_estimate(mid),
                    close_reason=CloseReason.SL_HIT,
                )
            if tick.bid <= order.request.tp:
                order.status = PaperOrderStatus.CLOSED
                order.close_reason = CloseReason.TP_HIT
                order.close_price = order.request.tp
                order.closed_at = time.time()
                return PositionState(
                    ticket_id=ticket_id, symbol=order.request.symbol,
                    is_open=False,
                    current_profit=order.current_profit_estimate(mid),
                    close_reason=CloseReason.TP_HIT,
                )

        # Position is open, SL/TP not yet hit
        return PositionState(
            ticket_id=ticket_id, symbol=order.request.symbol,
            is_open=True,
            current_profit=order.current_profit_estimate(mid),
            close_reason=None,
        )

    def get_all_open_positions(self, magic_number: int) -> list[PositionState]:
        self._require_connected()
        result = []
        for ticket, order in self._orders.items():
            if (order.request.magic_number == magic_number
                    and order.status == PaperOrderStatus.FILLED):
                try:
                    tick = self._data.get_tick(order.request.symbol)
                    mid = (tick.bid + tick.ask) / 2.0
                    profit = order.current_profit_estimate(mid)
                except Exception:
                    profit = 0.0
                result.append(PositionState(
                    ticket_id=ticket,
                    symbol=order.request.symbol,
                    is_open=True,
                    current_profit=profit,
                    close_reason=None,
                ))
        return result

    # ── Paper-only query methods ──────────────────────────────────────

    def get_paper_orders(self) -> dict[int, PaperOrder]:
        """Exposes the internal order book for monitoring/logging.
        Not part of the BrokerBridge ABC -- only paper-mode consumers
        that know they have a PaperBrokerBridge should call this."""
        return dict(self._orders)

    def get_paper_summary(self) -> dict:
        """Quick stats for logging/dashboard during paper trading."""
        orders = list(self._orders.values())
        return {
            "total_orders": len(orders),
            "pending": sum(1 for o in orders if o.status == PaperOrderStatus.PENDING),
            "filled": sum(1 for o in orders if o.status == PaperOrderStatus.FILLED),
            "closed": sum(1 for o in orders if o.status == PaperOrderStatus.CLOSED),
            "tp_hits": sum(1 for o in orders if o.close_reason == CloseReason.TP_HIT),
            "sl_hits": sum(1 for o in orders if o.close_reason == CloseReason.SL_HIT),
            "timeouts": sum(1 for o in orders if o.close_reason == CloseReason.TIMEOUT_CANCEL),
        }
