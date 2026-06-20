# broker/mt5_bridge.py
"""
T1.6a — MetaTrader5Bridge (6h)

Concrete BrokerBridge implementation wrapping the official `MetaTrader5`
Python package. This package is Windows-only and requires a running MT5
terminal — it cannot be exercised end-to-end outside that environment.

Design choice: import MetaTrader5 lazily inside __init__ rather than at
module level. This lets `broker/mt5_bridge.py` be imported (e.g. by
tests that only check method signatures, or by code that conditionally
selects MT5Bridge vs a future ZeroMQBridge) on a machine that doesn't
have the package installed. Any actual use will still fail loud — just
at instantiation/connect time instead of import time, which is the
correct place for an environment-dependent failure.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from broker.bridge import (
    BrokerBridge,
    BrokerConnectionError,
    CandleData,
    CloseReason,
    OrderRequest,
    OrderResult,
    OrderType,
    PositionState,
    TickData,
)

if TYPE_CHECKING:
    # Only for type checkers — never imported at runtime unless connect()
    # is actually called, per the lazy-import design above.
    import MetaTrader5 as mt5


# Maps our timeframe strings (matching config schema's Literal["H1","M15","M5"]
# plus H4/D1 used by the swing risk profile) to MT5's TIMEFRAME_* constants.
# Defined as a function rather than a module-level dict because it needs
# the `mt5` module object, which only exists after lazy import.
def _timeframe_map(mt5_module) -> dict[str, int]:
    return {
        "M5": mt5_module.TIMEFRAME_M5,
        "M15": mt5_module.TIMEFRAME_M15,
        "H1": mt5_module.TIMEFRAME_H1,
        "H4": mt5_module.TIMEFRAME_H4,
        "D1": mt5_module.TIMEFRAME_D1,
    }


class MetaTrader5Bridge(BrokerBridge):
    """
    T1.6a. Requires Windows + MetaTrader5 terminal running and logged in
    to the target account before connect() is called — this bridge does
    not manage terminal lifecycle or login credentials, only the trading
    API calls once a terminal session already exists.
    """

    def __init__(self) -> None:
        self._mt5 = None  # populated by connect()
        self._tf_map: dict[str, int] = {}
        self._connected = False

    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise BrokerConnectionError(
                "MetaTrader5 package not installed. This bridge requires "
                "Windows with the official MetaTrader5 pip package and a "
                "running, logged-in MT5 terminal."
            ) from exc

        if not mt5.initialize():
            # initialize() returning False is an *expected* failure mode
            # (terminal not running, wrong path, etc) — per the ABC's
            # contract, this returns False rather than raising.
            self._connected = False
            return False

        self._mt5 = mt5
        self._tf_map = _timeframe_map(mt5)
        self._connected = True
        return True

    def disconnect(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
        self._connected = False
        self._mt5 = None

    def _require_connected(self) -> None:
        if not self._connected or self._mt5 is None:
            raise BrokerConnectionError(
                "MetaTrader5Bridge.connect() was not called or failed — "
                "no active terminal connection."
            )

    def get_tick(self, symbol: str) -> TickData:
        self._require_connected()
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise BrokerConnectionError(
                f"symbol_info_tick('{symbol}') returned None — symbol may "
                f"not exist on this broker or Market Watch doesn't have it "
                f"enabled."
            )
        return TickData(
            symbol=symbol,
            bid=tick.bid,
            ask=tick.ask,
            timestamp=float(tick.time),
        )

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[CandleData]:
        self._require_connected()
        if timeframe not in self._tf_map:
            raise ValueError(
                f"Unsupported timeframe '{timeframe}'. Supported: "
                f"{sorted(self._tf_map.keys())}"
            )
        rates = self._mt5.copy_rates_from_pos(symbol, self._tf_map[timeframe], 0, count)
        if rates is None:
            # MT5 returns None (not empty array) on certain failures —
            # surface this distinctly from "zero bars available".
            raise BrokerConnectionError(
                f"copy_rates_from_pos('{symbol}', {timeframe}, 0, {count}) "
                f"returned None — check symbol is in Market Watch and "
                f"history is available for this timeframe."
            )
        # rates is a numpy structured array, oldest first (matches our
        # documented contract on BrokerBridge.get_candles).
        return [
            CandleData(
                symbol=symbol,
                timeframe=timeframe,
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["tick_volume"]),
                timestamp=float(r["time"]),
            )
            for r in rates
        ]

    def place_order(self, request: OrderRequest) -> OrderResult:
        self._require_connected()
        mt5 = self._mt5

        order_type_map = {
            OrderType.LIMIT_BUY: mt5.ORDER_TYPE_BUY_LIMIT,
            OrderType.LIMIT_SELL: mt5.ORDER_TYPE_SELL_LIMIT,
        }
        if request.order_type not in order_type_map:
            return OrderResult(
                success=False,
                ticket_id=None,
                fill_price=None,
                error_message=f"Unsupported order_type: {request.order_type}",
            )

        mt5_request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": request.symbol,
            "volume": request.volume,
            "type": order_type_map[request.order_type],
            "price": request.price,
            "sl": request.sl,
            "tp": request.tp,
            "magic": request.magic_number,
            "comment": request.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        result = mt5.order_send(mt5_request)

        if result is None:
            return OrderResult(
                success=False,
                ticket_id=None,
                fill_price=None,
                error_message=f"order_send returned None. last_error={mt5.last_error()}",
            )
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(
                success=False,
                ticket_id=None,
                fill_price=None,
                error_message=f"retcode={result.retcode} comment={result.comment}",
            )
        return OrderResult(
            success=True,
            ticket_id=result.order,
            fill_price=result.price,
            error_message=None,
        )

    def modify_order(self, ticket_id: int, sl: float, tp: float) -> bool:
        self._require_connected()
        mt5 = self._mt5
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket_id,
            "sl": sl,
            "tp": tp,
        }
        result = mt5.order_send(request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE

    def cancel_order(self, ticket_id: int) -> bool:
        self._require_connected()
        mt5 = self._mt5
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket_id,
        }
        result = mt5.order_send(request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE

    def get_position_state(self, ticket_id: int) -> PositionState:
        self._require_connected()
        mt5 = self._mt5
        positions = mt5.positions_get(ticket=ticket_id)

        if positions:  # non-empty tuple => still open
            pos = positions[0]
            return PositionState(
                ticket_id=ticket_id,
                symbol=pos.symbol,
                is_open=True,
                current_profit=pos.profit,
                close_reason=None,
            )

        # Not in open positions — check deal history to determine why it
        # closed. Without this, T2.4 reconciliation can't distinguish
        # SL_HIT/TP_HIT/MANUAL_CLOSE, which the plan explicitly requires.
        deals = mt5.history_deals_get(position=ticket_id)
        close_reason = self._infer_close_reason(deals, mt5)
        last_profit = deals[-1].profit if deals else 0.0

        return PositionState(
            ticket_id=ticket_id,
            symbol=deals[-1].symbol if deals else "",
            is_open=False,
            current_profit=last_profit,
            close_reason=close_reason,
        )

    @staticmethod
    def _infer_close_reason(deals, mt5) -> CloseReason:
        """
        MT5's deal `reason` field distinguishes SL/TP/manual closes. This
        is the concrete mechanism behind the abstract CloseReason enum —
        kept as a static method since it's pure mapping logic with no
        bridge state dependency, easy to unit test against synthetic
        deal objects without a live connection.
        """
        if not deals:
            # No deal history found for this ticket at all — most likely
            # a connection gap caused us to lose track of it entirely.
            return CloseReason.CONNECTION_LOST

        last_deal = deals[-1]
        reason = getattr(last_deal, "reason", None)

        if reason == mt5.DEAL_REASON_SL:
            return CloseReason.SL_HIT
        if reason == mt5.DEAL_REASON_TP:
            return CloseReason.TP_HIT
        if reason in (mt5.DEAL_REASON_CLIENT, mt5.DEAL_REASON_MOBILE, mt5.DEAL_REASON_WEB):
            return CloseReason.MANUAL_CLOSE
        if reason == mt5.DEAL_REASON_EXPERT:
            # Closed by an EA/script — in our system that's this agent's
            # own timeout-cancel logic (T1.8), not a manual human action.
            return CloseReason.TIMEOUT_CANCEL

        # Unrecognized reason code — don't guess, surface as connection
        # gap so T2.4's reconciliation flags it for manual review rather
        # than silently mis-categorizing it.
        return CloseReason.CONNECTION_LOST

    def get_all_open_positions(self, magic_number: int) -> list[PositionState]:
        self._require_connected()
        mt5 = self._mt5
        positions = mt5.positions_get()
        if positions is None:
            return []
        return [
            PositionState(
                ticket_id=p.ticket,
                symbol=p.symbol,
                is_open=True,
                current_profit=p.profit,
                close_reason=None,
            )
            for p in positions
            if p.magic == magic_number
        ]
