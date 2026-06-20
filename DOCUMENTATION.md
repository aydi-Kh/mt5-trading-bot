# AK Agent V3 — Documentation

> Written after behavior is final (T4.3 per plan). If code and docs disagree, code is correct.

---

## Architecture

**Timeframe hierarchy** (non-negotiable):

| Timeframe | Role | Module |
|-----------|------|--------|
| H1 | Structure: OB detection, BOS/CHoCH | `signals/order_block.py`, `signals/order_block_status.py` |
| M15 | Confirmation: SuperTrend filter + SMT | `signals/supertrend.py`, `signals/smt.py` |
| M5 | Execution only: limit order, 3-candle timeout | `execution/engine.py` |

**Critical path through the code per cycle:**

```
MT5 get_candles (H1, M15 ×2)
  → evaluate_signal()          # signals/pipeline.py
      → detect_order_blocks()  # H1 only
      → assess_ob_statuses()   # same H1 candle list (index-alignment)
      → compute_supertrend()   # M15 only
      → check_trend_agreement()# OB type vs ST direction
      → assess_smt()           # M15 primary vs M15 correlated
      → check_auto_reject()    # 5 conditions
      → compute_position_size()# R/R gate + SMT size scaling
  → submit_trade()             # execution/engine.py
      → _ob_based_sl()         # SL below OB low (long) / above OB high (short)
      → lifecycle.open_trade() # execution/lifecycle.py
  → lifecycle.poll_cycle()     # state machine: OPENED→FILLED→MONITORED→CLOSED
  → check_fill_timeouts()      # cancel if cycles_in_opened >= 3
  → lifecycle.find_residual_trades() # zero-residual guarantee
```

---

## Running on your machine

**Prerequisites:** Windows, MetaTrader5 terminal open and logged into XM Demo #1301528711.

```powershell
cd "C:\Users\X1 CARBONE\Desktop\AK"

# Install dependencies
pip install pydantic pyarrow pandas pyyaml

# Start the agent
python ak_agent_v3.py

# Paper trading mode (no real orders)
# Change MetaTrader5Bridge to PaperBrokerBridge in ak_agent_v3.py line ~50
```

**Config:** `config/trading_rules.yaml` — all trading rules live here.
Changing parameters requires no code edits. Schema is validated on startup.

---

## Module contracts

### `signals/pipeline.py` — `evaluate_signal(inputs, cfg) -> TradeDecision`

The central function. Pure: no I/O, no side effects, deterministic.
Backtest uses the exact same function (`research/backtest.py` imports it directly).

**Index alignment requirement:** `h1_candles` passed to `evaluate_signal`
must be the same list used internally for both `detect_order_blocks()` and
`assess_ob_statuses()`. Passing a resliced/differently-windowed list produces
silently wrong OB status assessments. This is the single most dangerous footgun
in the codebase. See `signals/order_block_status.py` docstring and the bounds
check at `assess_ob_status()` line ~95.

### `execution/lifecycle.py` — `TradeLifecycleManager`

**Never trust only local state.** `poll_cycle()` queries the broker directly every
call. Do not infer state from what you believe should have happened.

**OPENED→CLOSED ambiguity is intentional.** A trade in OPENED state whose broker
reports `is_open=False` is NOT automatically CLOSED — it may be a pending limit
not yet filled. Only `cancel_pending_trade()` (called by T1.8's timeout logic)
transitions a never-filled OPENED trade to CLOSED. This prevents fabricating a
`close_reason` for a trade that was never a live position.

**State machine:**
```
OPENED ──fill confirmed──→ FILLED ──one more poll──→ MONITORED ──SL/TP/manual──→ CLOSED ──T2.4──→ RECONCILED
  │
  └──cancel_pending_trade()──→ CLOSED (TIMEOUT_CANCEL)
```

### `broker/paper_bridge.py` — `PaperBrokerBridge`

Drop-in replacement for `MetaTrader5Bridge`. Delegates `get_candles()`/`get_tick()`
to a real data source; simulates all order execution in memory.

**Fill conditions:**
- `LIMIT_BUY` fills when `ask <= order.price` (we buy at the ask)
- `LIMIT_SELL` fills when `bid >= order.price` (we sell at the bid)

Getting these backwards would silently fill at wrong prices. They are tested in
`tests/test_paper_bridge.py::test_long_fills_when_ask_reaches_limit_price`.

### `risk/sizer.py` — `compute_position_size()`

Two separate gate parameters — **not one**:
- `trend_agreement: TrendAgreement` — OB direction vs SuperTrend direction.
  `CONFLICT` → hard no-trade, checked before R/R, before SMT.
- `smt_alignment: SMTAlignment` — correlation pair divergence only.
  `DIVERGENT` → 50% size, not a rejection.

These were mistakenly conflated into one `SMTAlignment` enum in an early version
(see commit `16ad5bd`). The split is deliberate and must not be reverted.

---

## Known gaps (explicit, not hidden)

| Gap | Location | Note |
|-----|----------|------|
| No news calendar | `signals/auto_reject.py` condition 5 | Condition always passes (no data source). Wire a real API here. |
| Account equity hardcoded | `ak_agent_v3.py` line ~175 | `get_account_info()` not yet in `BrokerBridge` ABC. Add it. |
| TP from ATR×3.75 placeholder | `ak_agent_v3.py` `_evaluate_symbol()` | PDH/PWL liquidity levels not fetched. T3.x scope. |
| T1.2c backtest validation | Not yet built | Validates OB detection on 2 years real data. Gated on T3.3. |
| MT5Bridge untested live | All bridge tests use FakeBridge | Requires Windows + terminal. Cannot verify in CI. |
| Dukascopy tier | `data/historical_connector.py` | Endpoint format may change. Verify before relying on it. |

---

## Bugs found and fixed during development

These are real bugs caught by tests in this project, documented here so they're
findable if similar symptoms appear in the future:

1. **`cycles_in_opened` stuck at 0** (T1.6c): post-transition state check — the counter
   incremented based on `trade.state` AFTER the transition, which could never equal
   `OPENED` once the trade had just filled. Fixed: check `prior_state` (pre-transition).

2. **R/R float boundary rejection** (T1.5): `1.1020 - 1.1000` in IEEE754 double
   evaluates to `0.0019999...`, not `0.0020`. A textbook 2.0 R/R setup was being
   rejected. Fixed: epsilon tolerance `1e-9` on the `< min_rr` comparison.

3. **SMTAlignment conflated two checks** (T1.4 pre-work): `COUNTER_TREND` on
   `SMTAlignment` claimed to represent OB-vs-ST conflict, but SMT is only the
   correlation pair divergence check. Fixed: split into `TrendAgreement` (AGREE/CONFLICT)
   and `SMTAlignment` (ALIGNED/DIVERGENT) as separate required parameters.

4. **OB status index alignment** (T1.2b): passing a resliced candle list to
   `assess_ob_status()` silently produced wrong results (candle indices from
   `detect_order_blocks()` pointed into a different position in the resliced list).
   Fixed: added out-of-range bounds check; full fix is architectural (pass the same list).

5. **auto_reject.py pre-existing with 3 failing tests**: found a file I had no record
   of writing. Evaluated on merit, fixed 3 fixture construction errors (same BOS-touching-
   ob-low pattern seen across T1.2b and T1.7 tests). Kept the legitimate design decisions.

6. **LONG limit fill direction** (T3.3 stress tests): "never fills" for a LONG limit
   means `entry_price` must be BELOW market (price hasn't dropped that far), not above it
   (`low <= entry_price` is always true when entry is above market). Same logic applies
   in the live engine and paper bridge.

---

## Test suite

```powershell
# Run all tests
for f in tests/test_*.py: python "$f"

# Run specific module
python tests/test_lifecycle.py

# Run integration tests only
python tests/test_integration.py

# Run stress tests
python tests/test_stress.py
```

**Total: 184 unit + integration + stress tests across 17 modules.**

---

## Phase 4 gaps remaining (T1.2c and final walk-forward)

- **T1.2c** (OB backtest validation): validate `detect_order_blocks()` on 2 years
  of real H1 data from your broker. Run `data/historical_connector.py::fetch_historical`
  then `research/backtest.py::run_backtest` with a long `h1_df`. Check the OB count
  and freshness distribution against your manual chart review.

- **Walk-forward final run**: once `T1.2c` confirms OB detection is clean on real data,
  run `research/walk_forward.py::run_walk_forward` with `h1_df` of 2+ years. The holdout
  window (last 3 months) is evaluated exactly once, after all parameter decisions are final.
  Do not peek at holdout results before fixing parameters.
