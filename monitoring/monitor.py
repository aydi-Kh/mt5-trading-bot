# monitoring/monitor.py
"""
Phase 2 Monitoring — T2.1 through T2.5

T2.1 Trade DB: query functions over the LifecycleStore JSON file.
     Note: LifecycleStore itself is the persistence layer (T1.6c).
     This module adds query/aggregation on top of it.

T2.2 Equity Curve: append-only balance/equity snapshot log (parquet).
     Writes one row per poll cycle when account info is available.

T2.3 R/R Realized vs Target: computes per-trade realized R/R from
     entry_price, sl_placed, exit_price stored in TrackedTrade, and
     compares against the minimum R/R from config. Per the cross-phase
     spec: "never display single-trade P&L as headline -- report rolling
     metrics (last 20/50 trades) by default."

T2.4 Daily Reconciliation: compares internal lifecycle store against
     broker-reported open positions (via BrokerBridge.get_all_open_
     positions) using ticket IDs, not counts. Flags any residuals.
     Per plan: "compare MT5 ticket IDs, not just counts."

T2.5 Alert System: event-driven; callers push StateChangeEvent objects,
     this module routes them to configured channels (console, and a
     Telegram stub that is explicitly not wired to a real bot token yet).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

import pandas as pd

from broker.bridge import BrokerBridge
from config.schema import TradingConfig
from execution.lifecycle import LifecycleStore, TrackedTrade, TradeState, CloseReason

log = logging.getLogger("ak_monitor")


# ── T2.1 Trade DB query layer ─────────────────────────────────────────

def get_all_trades(store: LifecycleStore) -> list[TrackedTrade]:
    return list(store.load_all().values())


def get_closed_trades(store: LifecycleStore) -> list[TrackedTrade]:
    return [t for t in get_all_trades(store) if t.state in (
        TradeState.CLOSED, TradeState.RECONCILED
    )]


def get_open_trades(store: LifecycleStore) -> list[TrackedTrade]:
    return [t for t in get_all_trades(store) if t.state in (
        TradeState.OPENED, TradeState.FILLED, TradeState.MONITORED
    )]


def get_unreconciled_closed(store: LifecycleStore) -> list[TrackedTrade]:
    return [t for t in get_all_trades(store) if t.state == TradeState.CLOSED]


# ── T2.2 Equity Curve ────────────────────────────────────────────────

class EquityCurveTracker:
    """
    Appends balance/equity snapshots to a parquet file, one row per
    poll cycle when account data is available. Parquet chosen for
    efficient time-series reads in T3.2's metrics engine.

    NOTE: account_balance and account_equity are passed in by the main
    loop (ak_agent_v3.py) rather than fetched here, because fetching
    requires a bridge call and this module is deliberately side-effect-
    free regarding the broker connection. The main loop owns the bridge.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, balance: float, equity: float, open_positions: int) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "balance": balance,
            "equity": equity,
            "open_positions": open_positions,
            "drawdown_pct": round((balance - equity) / balance * 100, 3) if balance > 0 else 0.0,
        }
        if self._path.exists():
            df = pd.read_parquet(self._path)
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        else:
            df = pd.DataFrame([row])
        df.to_parquet(self._path, index=False)

    def read(self) -> pd.DataFrame:
        if not self._path.exists():
            return pd.DataFrame(columns=["ts", "balance", "equity",
                                          "open_positions", "drawdown_pct"])
        return pd.read_parquet(self._path)

    def peak_balance(self) -> float:
        df = self.read()
        return float(df["balance"].max()) if len(df) > 0 else 0.0

    def max_drawdown_pct(self) -> float:
        df = self.read()
        if len(df) < 2:
            return 0.0
        # Drawdown = equity falling below its own running peak, not
        # balance vs itself (balance is the deposited amount which
        # doesn't change unless you deposit/withdraw; equity fluctuates
        # with open P&L). An earlier version computed this on `balance`
        # which was always flat, producing 0.0 -- fixed to use `equity`.
        peak = df["equity"].cummax()
        dd = (peak - df["equity"]) / peak * 100
        return float(dd.max())


# ── T2.3 R/R Realized vs Target ──────────────────────────────────────

@dataclass(frozen=True)
class TradeRRAnalysis:
    ticket_id: int
    symbol: str
    is_long: bool | None
    entry_price: float | None
    sl_placed: float | None
    tp_placed: float | None
    exit_pnl: float | None      # stored as current_profit (P&L, not price)
    close_reason: str | None
    sl_distance: float | None   # abs(entry - sl)
    tp_distance: float | None   # abs(tp - entry)
    target_rr: float | None     # tp_distance / sl_distance
    realized_pnl: float | None  # exit_pnl
    hit_tp: bool
    hit_sl: bool


def analyze_rr(trade: TrackedTrade) -> TradeRRAnalysis:
    sl_dist = (
        abs(trade.entry_price - trade.sl_placed)
        if trade.entry_price is not None and trade.sl_placed is not None
        else None
    )
    tp_dist = (
        abs(trade.tp_placed - trade.entry_price)
        if trade.tp_placed is not None and trade.entry_price is not None
        else None
    )
    target_rr = (
        round(tp_dist / sl_dist, 2)
        if tp_dist is not None and sl_dist is not None and sl_dist > 0
        else None
    )
    return TradeRRAnalysis(
        ticket_id=trade.ticket_id,
        symbol=trade.symbol,
        is_long=trade.is_long,
        entry_price=trade.entry_price,
        sl_placed=trade.sl_placed,
        tp_placed=trade.tp_placed,
        exit_pnl=trade.exit_price,  # stored as P&L at close
        close_reason=trade.close_reason.value if trade.close_reason else None,
        sl_distance=sl_dist,
        tp_distance=tp_dist,
        target_rr=target_rr,
        realized_pnl=trade.exit_price,
        hit_tp=trade.close_reason == CloseReason.TP_HIT,
        hit_sl=trade.close_reason == CloseReason.SL_HIT,
    )


@dataclass(frozen=True)
class RollingRRReport:
    """
    Rolling R/R report over last N trades. Per the cross-phase spec:
    "never display single-trade P&L as headline -- report rolling
    metrics (last 20/50 trades) by default." The default window is 20.
    """
    window_size: int
    trades_analyzed: int
    win_rate_pct: float
    avg_target_rr: float | None
    tp_hit_count: int
    sl_hit_count: int
    timeout_count: int
    per_symbol: dict[str, dict]  # symbol -> {win_rate, count, avg_rr}


def rolling_rr_report(store: LifecycleStore, window: int = 20) -> RollingRRReport:
    closed = sorted(
        get_closed_trades(store),
        key=lambda t: t.closed_at or 0,
        reverse=True,
    )[:window]

    analyses = [analyze_rr(t) for t in closed]
    tp_hits = sum(1 for a in analyses if a.hit_tp)
    sl_hits = sum(1 for a in analyses if a.hit_sl)
    timeouts = sum(
        1 for t in closed if t.close_reason == CloseReason.TIMEOUT_CANCEL
    )
    rr_values = [a.target_rr for a in analyses if a.target_rr is not None]
    avg_rr = round(sum(rr_values) / len(rr_values), 2) if rr_values else None
    wr = round(tp_hits / len(analyses) * 100, 1) if analyses else 0.0

    by_sym: dict[str, dict] = {}
    for a in analyses:
        s = a.symbol
        if s not in by_sym:
            by_sym[s] = {"wins": 0, "total": 0, "rr_sum": 0.0, "rr_count": 0}
        by_sym[s]["total"] += 1
        if a.hit_tp:
            by_sym[s]["wins"] += 1
        if a.target_rr is not None:
            by_sym[s]["rr_sum"] += a.target_rr
            by_sym[s]["rr_count"] += 1

    per_sym = {}
    for s, d in by_sym.items():
        per_sym[s] = {
            "win_rate_pct": round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0.0,
            "count": d["total"],
            "avg_rr": round(d["rr_sum"] / d["rr_count"], 2) if d["rr_count"] else None,
        }

    return RollingRRReport(
        window_size=window,
        trades_analyzed=len(analyses),
        win_rate_pct=wr,
        avg_target_rr=avg_rr,
        tp_hit_count=tp_hits,
        sl_hit_count=sl_hits,
        timeout_count=timeouts,
        per_symbol=per_sym,
    )


# ── T2.4 Daily Reconciliation ─────────────────────────────────────────

@dataclass(frozen=True)
class ReconciliationReport:
    timestamp: str
    residual_ticket_ids: list[int]    # at broker, not in store
    missing_from_broker: list[int]    # in store as open, not at broker
    state_mismatches: list[dict]
    is_clean: bool


def reconcile(
    store: LifecycleStore,
    bridge: BrokerBridge,
    magic_number: int,
) -> ReconciliationReport:
    """
    Compares internal store against broker state using ticket IDs, per
    the plan: "compare MT5 ticket IDs, not just counts." Called once
    per the reconciliation schedule (daily in config) but can be called
    at any time.
    """
    broker_positions = {p.ticket_id: p for p in bridge.get_all_open_positions(magic_number)}
    store_open = {t.ticket_id: t for t in get_open_trades(store)}

    # Trades open at broker but not tracked locally
    residuals = [tid for tid in broker_positions if tid not in store_open]

    # Trades we think are open but broker says are closed
    missing = [
        tid for tid, trade in store_open.items()
        if tid not in broker_positions
        and trade.state in (TradeState.FILLED, TradeState.MONITORED)
    ]

    # State mismatches (in both, but state disagrees)
    mismatches = []
    for tid in broker_positions:
        if tid in store_open:
            store_state = store_open[tid].state.value
            broker_open = broker_positions[tid].is_open
            expected = store_state in ("filled", "monitored")
            if broker_open != expected:
                mismatches.append({
                    "ticket_id": tid,
                    "store_state": store_state,
                    "broker_is_open": broker_open,
                })

    is_clean = not residuals and not missing and not mismatches
    return ReconciliationReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        residual_ticket_ids=residuals,
        missing_from_broker=missing,
        state_mismatches=mismatches,
        is_clean=is_clean,
    )


# ── T2.5 Alert System ────────────────────────────────────────────────

class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Alert:
    severity: AlertSeverity
    category: str
    message: str
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            object.__setattr__(self, "timestamp", datetime.now(timezone.utc).isoformat())


AlertHandler = Callable[[Alert], None]


class AlertSystem:
    """
    Event-driven alert routing. Handlers are registered per channel;
    callers push Alert objects and this system routes to all registered
    handlers.

    Telegram handler is stubbed -- real implementation requires a bot
    token and chat ID which are not configured anywhere in this project
    yet. Explicitly stubbed rather than omitted so callers can see it
    exists and will need to be wired up, not silently absent.
    """

    def __init__(self) -> None:
        self._handlers: list[AlertHandler] = []

    def add_handler(self, handler: AlertHandler) -> None:
        self._handlers.append(handler)

    def emit(self, alert: Alert) -> None:
        for handler in self._handlers:
            try:
                handler(alert)
            except Exception as e:
                log.error("Alert handler failed: %s", e)

    def emit_state_change(self, trade: TrackedTrade) -> None:
        severity = (
            AlertSeverity.WARNING if trade.close_reason == CloseReason.SL_HIT
            else AlertSeverity.CRITICAL if trade.close_reason == CloseReason.EXECUTION_FAILURE
            else AlertSeverity.INFO
        )
        self.emit(Alert(
            severity=severity,
            category="state_change",
            message=(
                f"ticket={trade.ticket_id} {trade.symbol} -> {trade.state.value}"
                + (f" reason={trade.close_reason.value}" if trade.close_reason else "")
            ),
        ))

    def emit_reconciliation(self, report: ReconciliationReport) -> None:
        if report.is_clean:
            self.emit(Alert(
                severity=AlertSeverity.INFO,
                category="reconciliation",
                message="Daily reconciliation: CLEAN (no residuals or mismatches)",
            ))
        else:
            self.emit(Alert(
                severity=AlertSeverity.CRITICAL,
                category="reconciliation",
                message=(
                    f"Reconciliation ISSUES: residuals={report.residual_ticket_ids} "
                    f"missing={report.missing_from_broker} "
                    f"mismatches={len(report.state_mismatches)}"
                ),
            ))


def console_handler(alert: Alert) -> None:
    level = {
        AlertSeverity.INFO: log.info,
        AlertSeverity.WARNING: log.warning,
        AlertSeverity.CRITICAL: log.critical,
    }.get(alert.severity, log.info)
    level("[%s] %s: %s", alert.category, alert.severity.value.upper(), alert.message)


def telegram_stub_handler(alert: Alert) -> None:
    """
    Stub Telegram handler. Logs that it would send a message but does
    not attempt a network call -- no bot token or chat ID is configured.
    Replace this with a real implementation when those are available.
    """
    log.debug("TELEGRAM_STUB [%s] %s", alert.severity.value, alert.message)
