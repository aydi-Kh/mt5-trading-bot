# execution/lifecycle.py
"""
T1.6c — TradeLifecycleManager (6h estimate, critical path spine)

State machine: OPENED -> FILLED -> MONITORED -> CLOSED -> RECONCILED

Core constraint from the plan's Phase 1 risk mitigation: "T1.6c must poll
MT5 position state independently every cycle, never trust only its own
internal state — this is what 'zero residual trades' actually requires."

This means `poll_cycle()` is the central method: it does NOT advance state
based on what the manager *thinks* should have happened (e.g. "I placed
the order, so it must be OPENED"), it advances state based on what
get_position_state() / get_all_open_positions() *report*, every time.

Persistence: each tracked trade is written to a JSON file after every
state transition. This is a deliberately minimal store, not T2.1's actual
"Trade DB schema" (Phase 2, separate task) — using JSON here rather than
inventing a DB schema this task wasn't scoped to design. T2.1 should
consume/migrate this format, not be blocked waiting for it.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from broker.bridge import (
    BrokerBridge,
    CloseReason,
    OrderRequest,
    PositionState,
)


class TradeState(Enum):
    OPENED = "opened"          # order placed, not yet confirmed filled
    FILLED = "filled"          # confirmed open position exists at broker
    MONITORED = "monitored"    # actively being polled (functionally same
                                # as FILLED but distinguishes "just filled
                                # this cycle" from "has been open N cycles",
                                # useful for T1.8's fill-timeout logic which
                                # only applies to OPENED, not MONITORED)
    CLOSED = "closed"          # broker confirms position no longer open
    RECONCILED = "reconciled"  # T2.4 has processed this closure into
                                # the daily reconciliation report


@dataclass
class TrackedTrade:
    ticket_id: int
    symbol: str
    state: TradeState
    opened_at: float
    last_polled_at: float
    close_reason: CloseReason | None = None
    closed_at: float | None = None
    cycles_in_opened: int = 0  # used by T1.8's 3-candle timeout check
    # Execution data (populated by open_trade, exit_price populated on CLOSED)
    entry_price: float | None = None
    sl_placed: float | None = None
    tp_placed: float | None = None
    volume: float | None = None
    is_long: bool | None = None
    exit_price: float | None = None   # last broker-reported profit/price on close

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        d["close_reason"] = self.close_reason.value if self.close_reason else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TrackedTrade":
        return cls(
            ticket_id=d["ticket_id"],
            symbol=d["symbol"],
            state=TradeState(d["state"]),
            opened_at=d["opened_at"],
            last_polled_at=d["last_polled_at"],
            close_reason=CloseReason(d["close_reason"]) if d["close_reason"] else None,
            closed_at=d.get("closed_at"),
            cycles_in_opened=d.get("cycles_in_opened", 0),
            entry_price=d.get("entry_price"),
            sl_placed=d.get("sl_placed"),
            tp_placed=d.get("tp_placed"),
            volume=d.get("volume"),
            is_long=d.get("is_long"),
            exit_price=d.get("exit_price"),
        )


class LifecycleStore:
    """
    Minimal JSON-file persistence. Not T2.1's trade DB — see module
    docstring. Kept as its own class (not inlined into the manager) so
    it can be swapped for a real DB-backed store later without touching
    TradeLifecycleManager's control flow.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write({})

    def _read(self) -> dict[str, dict]:
        with open(self._path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict[str, dict]) -> None:
        # Write to temp file + rename for atomicity — a crash mid-write
        # must never leave a half-written, unparseable store, since that
        # would defeat the entire "track every trade reliably" purpose.
        tmp_path = self._path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(self._path)

    def save(self, trade: TrackedTrade) -> None:
        data = self._read()
        data[str(trade.ticket_id)] = trade.to_dict()
        self._write(data)

    def load_all(self) -> dict[int, TrackedTrade]:
        data = self._read()
        return {int(k): TrackedTrade.from_dict(v) for k, v in data.items()}

    def load(self, ticket_id: int) -> TrackedTrade | None:
        data = self._read()
        raw = data.get(str(ticket_id))
        return TrackedTrade.from_dict(raw) if raw else None


class TradeLifecycleManager:
    """
    Owns the OPENED -> FILLED -> MONITORED -> CLOSED -> RECONCILED state
    machine for every trade this agent places. Magic number scoping (per
    BrokerBridge.get_all_open_positions(magic_number)) is how this manager
    distinguishes its own trades from manual/other-EA positions on the
    same account — this directly serves the "zero residual KA trades"
    hard constraint by giving T2.4 a magic-number-filtered ground truth
    to reconcile against.
    """

    def __init__(self, bridge: BrokerBridge, store: LifecycleStore, magic_number: int) -> None:
        self._bridge = bridge
        self._store = store
        self._magic_number = magic_number

    def open_trade(
        self,
        request: OrderRequest,
        is_long: bool | None = None,
    ) -> TrackedTrade | None:
        """
        Places the order and begins tracking it in OPENED state. Returns
        None if placement failed outright — caller (T1.7/T1.8) must check
        for None rather than assume a TrackedTrade always comes back.

        Does NOT assume the order is filled just because place_order()
        returned success=True (that only confirms the broker *accepted*
        a pending limit order, not that it filled) — state stays OPENED
        until poll_cycle() confirms via get_position_state().
        """
        result = self._bridge.place_order(request)
        if not result.success or result.ticket_id is None:
            return None

        now = time.time()
        trade = TrackedTrade(
            ticket_id=result.ticket_id,
            symbol=request.symbol,
            state=TradeState.OPENED,
            opened_at=now,
            last_polled_at=now,
            cycles_in_opened=0,
            entry_price=request.price,
            sl_placed=request.sl,
            tp_placed=request.tp,
            volume=request.volume,
            is_long=is_long,
        )
        self._store.save(trade)
        return trade

    def poll_cycle(self) -> list[TrackedTrade]:
        """
        The central method per the plan's risk mitigation. Called once per
        agent cycle. For every trade not yet RECONCILED, queries the broker
        directly — never advances state from local assumption alone.

        Returns the list of trades that changed state this cycle (useful
        for logging/alerting in T2.5, without that module needing to diff
        states itself).
        """
        changed: list[TrackedTrade] = []
        tracked = self._store.load_all()
        now = time.time()

        for ticket_id, trade in tracked.items():
            if trade.state == TradeState.RECONCILED:
                continue  # terminal state, nothing left to poll

            broker_state: PositionState = self._bridge.get_position_state(ticket_id)
            prior_state = trade.state

            # cycles_in_opened counts how many polls this trade has spent
            # waiting to fill, for T1.8's 3-candle timeout. Must check
            # prior_state (captured above, before any transition this
            # cycle) — checking trade.state AFTER the transition below
            # would always be false the moment a pending order fills,
            # since OPENED unconditionally transitions to FILLED in the
            # same poll it's first observed as open at the broker. An
            # earlier version of this method had exactly that bug: the
            # increment condition checked post-transition state and was
            # therefore permanently dead code.
            if prior_state == TradeState.OPENED:
                trade.cycles_in_opened += 1

            if broker_state.is_open:
                if trade.state == TradeState.OPENED:
                    trade.state = TradeState.FILLED
                elif trade.state == TradeState.FILLED:
                    trade.state = TradeState.MONITORED
                # MONITORED stays MONITORED while open — no further transition.
            else:
                # Broker says not open. Two distinct cases that must NOT
                # be conflated:
                #   (a) trade had been FILLED/MONITORED at some point and
                #       has now genuinely closed (SL/TP/manual) -> CLOSED.
                #   (b) trade is still OPENED and has never been observed
                #       as filled -> this is an order still waiting to
                #       fill (or one T1.8 should consider cancelling), NOT
                #       a closed trade. Auto-transitioning this to CLOSED
                #       would be a misclassification: T1.8's 3-candle
                #       timeout logic needs to see this trade still in
                #       OPENED state (and read cycles_in_opened) to decide
                #       whether to call cancel_order() itself. If we jump
                #       straight to CLOSED here, T1.8 never gets that
                #       chance and a stale pending order would silently
                #       look "done" with a fabricated close_reason.
                #
                # Net effect: state stays OPENED (no transition) when
                # prior_state was OPENED and broker reports not-open.
                # cycles_in_opened (incremented above) is what lets T1.8
                # eventually act on it; this method does not decide
                # cancellation timing itself, deliberately (M5 = execution
                # layer per the plan, not lifecycle tracking).
                if prior_state != TradeState.OPENED and trade.state != TradeState.CLOSED:
                    trade.state = TradeState.CLOSED
                    trade.close_reason = broker_state.close_reason
                    trade.closed_at = now
                    # current_profit from broker is the P&L, not a price.
                    # Stored here as the closest available close-time data
                    # without an additional deal-history query; T2.3's
                    # R/R analysis uses it for realized P&L computation.
                    trade.exit_price = broker_state.current_profit

            trade.last_polled_at = now
            self._store.save(trade)

            if trade.state != prior_state:
                changed.append(trade)

        return changed

    def cancel_pending_trade(self, ticket_id: int) -> bool:
        """
        For T1.8's 3-candle fill timeout: explicitly cancels a still-OPENED
        trade and records it as CLOSED with CloseReason.TIMEOUT_CANCEL.

        This is the deliberate counterpart to poll_cycle() no longer
        auto-closing never-filled OPENED trades (see the else-branch
        comment in poll_cycle): T1.8 must call this method itself once it
        decides cycles_in_opened has exceeded the fill window, rather than
        waiting for some future poll to ambiguously resolve the state.

        Returns False (and leaves state untouched) if the trade is not in
        OPENED state, or if the broker reports it actually filled in the
        meantime (race: order filled between T1.8's decision and this
        call) -- callers must check the return value rather than assume
        cancellation always succeeds.
        """
        trade = self._store.load(ticket_id)
        if trade is None:
            raise KeyError(f"No tracked trade with ticket_id={ticket_id}")
        if trade.state != TradeState.OPENED:
            return False

        # Race check: confirm with the broker it hasn't filled in the
        # interval between T1.8's decision and this call, before cancelling.
        current = self._bridge.get_position_state(ticket_id)
        if current.is_open:
            # It filled after all -- do not cancel a live position, and
            # let the normal poll_cycle() path transition it forward
            # (OPENED -> FILLED) on the next poll instead.
            return False

        cancelled = self._bridge.cancel_order(ticket_id)
        if not cancelled:
            return False

        trade.state = TradeState.CLOSED
        trade.close_reason = CloseReason.TIMEOUT_CANCEL
        trade.closed_at = time.time()
        self._store.save(trade)
        return True

    def move_to_breakeven(self, ticket_id: int, breakeven_price: float, current_tp: float) -> bool:
        """
        Moves SL to breakeven_price (typically entry price, sometimes
        entry+spread) while leaving TP unchanged. Only valid for a trade
        already in MONITORED state -- a position must be confirmed open
        with the broker before its SL can be modified, and FILLED/OPENED
        trades have no live position to act on yet.

        This is the actual consumer of BrokerBridge.modify_order(), added
        specifically because there was previously no mechanism anywhere
        in this codebase to adjust a stop after entry (confirmed by
        grepping for modify/breakeven/trail before adding this) -- a real
        functional gap, not cosmetic.

        Returns False without mutating local state if the trade isn't in
        MONITORED state, or if the broker-side modification fails.
        """
        trade = self._store.load(ticket_id)
        if trade is None:
            raise KeyError(f"No tracked trade with ticket_id={ticket_id}")
        if trade.state != TradeState.MONITORED:
            return False

        success = self._bridge.modify_order(ticket_id, sl=breakeven_price, tp=current_tp)
        # Deliberately NOT mutating TrackedTrade on success: SL/TP values
        # are not currently tracked fields on TrackedTrade (only
        # state/timing/close_reason are). If callers need to know the
        # current SL after a breakeven move, that should come from
        # get_position_state() / a live broker query, not a second
        # locally-cached copy of broker-owned data -- the same
        # never-trust-only-local-state principle that governs poll_cycle().
        return success

    def mark_reconciled(self, ticket_id: int) -> None:
        """Called by T2.4 once a CLOSED trade has been processed into the
        daily reconciliation report, moving it to the terminal state so
        poll_cycle() stops querying the broker for it indefinitely."""
        trade = self._store.load(ticket_id)
        if trade is None:
            raise KeyError(f"No tracked trade with ticket_id={ticket_id}")
        if trade.state != TradeState.CLOSED:
            raise ValueError(
                f"Cannot mark ticket {ticket_id} as RECONCILED — current "
                f"state is {trade.state.value}, expected CLOSED. "
                f"Reconciliation must only process fully-closed trades."
            )
        trade.state = TradeState.RECONCILED
        self._store.save(trade)

    def get_trades_pending_fill(self) -> list[TrackedTrade]:
        """Used by T1.8's 3-candle timeout logic to find OPENED trades
        that may need cancellation."""
        return [
            t for t in self._store.load_all().values()
            if t.state == TradeState.OPENED
        ]

    def get_unreconciled_closed_trades(self) -> list[TrackedTrade]:
        """Used by T2.4's daily reconciliation job."""
        return [
            t for t in self._store.load_all().values()
            if t.state == TradeState.CLOSED
        ]

    def find_residual_trades(self) -> list[int]:
        """
        Direct implementation of the 'zero residual trades' constraint:
        queries the broker for ALL open positions under this agent's
        magic number, and returns any ticket_id present at the broker
        but NOT being tracked locally (e.g. after a crash/restart, or a
        bug elsewhere that placed an order without going through
        open_trade()). This is the check T2.4 runs daily, and arguably
        the single most important method in this whole module for
        satisfying the plan's hard constraint.
        """
        broker_positions = self._bridge.get_all_open_positions(self._magic_number)
        tracked_ids = set(self._store.load_all().keys())
        residual = [
            p.ticket_id for p in broker_positions
            if p.ticket_id not in tracked_ids
        ]
        return residual
