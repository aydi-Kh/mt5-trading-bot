# broker/bridge.py
"""
T1.6 — BrokerBridge abstraction (2h)

Per plan: TradeLifecycleManager (T1.6c) must never import MetaTrader5 or
zmq directly. Swapping the MT5 implementation (T1.6a) for a future ZeroMQ
implementation (T1.6b, deferred per risk note in plan §5) must not require
touching any code above this layer.

This is intentionally a thin, synchronous interface — no async, no
callbacks. T1.6c polls get_position_state() every cycle rather than
trusting a push-based event model, per the explicit risk mitigation in
Phase 1: "never trust only locally-cached state."
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class OrderType(Enum):
    LIMIT_BUY = "limit_buy"
    LIMIT_SELL = "limit_sell"


class CloseReason(Enum):
    SL_HIT = "sl_hit"
    TP_HIT = "tp_hit"
    MANUAL_CLOSE = "manual_close"
    TIMEOUT_CANCEL = "timeout_cancel"
    CONNECTION_LOST = "connection_lost"  # T2.4 reconciliation needs this
    EXECUTION_FAILURE = "execution_failure"  # forced close after a
    # detected execution-layer fault (e.g. modify_order failed repeatedly,
    # broker returned an inconsistent state). This is the concept behind
    # a second AI-generated revision of this plan's "EMERGENCY_CLOSE" as
    # a distinct state machine branch -- deliberately implemented here as
    # a CLOSE REASON instead of a new state, because that revision didn't
    # specify what transitions lead into/out of an EMERGENCY_CLOSE state,
    # and adding an undefined transition would reintroduce the same kind
    # of ambiguity bug already found and fixed in this state machine.
    # distinguished from MANUAL_CLOSE — a dropped connection that later
    # reconnects to find a position already closed is a different failure
    # mode than a human closing it in the terminal, and T2.4's daily
    # reconciliation report should not conflate the two.


@dataclass(frozen=True)
class TickData:
    symbol: str
    bid: float
    ask: float
    timestamp: float  # unix epoch seconds, UTC


@dataclass(frozen=True)
class CandleData:
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: float  # unix epoch seconds, UTC, candle open time


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    order_type: OrderType
    volume: float
    price: float
    sl: float
    tp: float
    magic_number: int
    comment: str


@dataclass(frozen=True)
class OrderResult:
    success: bool
    ticket_id: int | None
    fill_price: float | None
    error_message: str | None


@dataclass(frozen=True)
class PositionState:
    ticket_id: int
    symbol: str
    is_open: bool
    current_profit: float
    close_reason: CloseReason | None  # None while is_open is True


class BrokerConnectionError(RuntimeError):
    """Raised by connect()/disconnect() failures. Callers (T1.6c) should
    treat this as fatal for the current cycle, not retry silently inline —
    retry/backoff policy belongs in the lifecycle manager, not the bridge."""


class BrokerBridge(ABC):
    """
    Abstraction boundary between trading logic (T1.7-T1.10, T1.6c) and the
    concrete broker connection. All methods are synchronous and blocking —
    this matches the existing MetaTrader5 package's calling convention and
    avoids introducing async complexity that the rest of the V3 scope
    (single-process, 5-minute-cycle agent) doesn't need.
    """

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection. Returns True on success. Must not raise
        for an expected failure (e.g. terminal not running) — return False
        and let the caller decide whether that's fatal."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def modify_order(self, ticket_id: int, sl: float, tp: float) -> bool:
        """
        Updates SL/TP on an already-open position (NOT a pending order --
        use cancel_order + place_order to change a pending limit order's
        price/SL/TP instead). Required for breakeven-stop and trailing-
        stop management once a trade is live; this was a genuine gap in
        the original interface design, not present until this method was
        added. Returns True only on confirmed successful modification --
        callers must not assume the new SL/TP is active without checking.
        """
        ...

    @abstractmethod
    def get_tick(self, symbol: str) -> TickData:
        """Raises BrokerConnectionError if symbol unavailable or not connected."""
        ...

    @abstractmethod
    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[CandleData]:
        """
        Returns up to `count` most recent closed candles, oldest first.
        May return fewer than `count` if broker history is shorter —
        callers (e.g. T1.2a OB detector) must check len() rather than
        assume the request was fully satisfied. This is the same
        constraint T3.1 hits at historical-fetch scale (10k bar broker
        limits), just visible here at live-fetch scale too.
        """
        ...

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, ticket_id: int) -> bool:
        """Used by T1.8's 3-candle fill timeout. Returns True if the order
        was successfully cancelled (i.e. it was still pending). Returns
        False if it had already filled or didn't exist — caller must then
        re-check get_position_state() rather than assume cancellation
        implies non-existence."""
        ...

    @abstractmethod
    def get_position_state(self, ticket_id: int) -> PositionState:
        """
        Must be called independently every cycle by T1.6c's lifecycle
        manager — never trust only locally-cached state. This is the
        single most important method on this interface for satisfying
        the "zero residual trades" hard constraint.
        """
        ...

    @abstractmethod
    def get_all_open_positions(self, magic_number: int) -> list[PositionState]:
        """
        Used by T2.4 reconciliation to detect residual/orphaned trades the
        lifecycle manager's internal state doesn't know about (e.g. after
        a crash/restart). This must query the broker directly, not read
        from any local cache the lifecycle manager maintains.
        """
        ...
