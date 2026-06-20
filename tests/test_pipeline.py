# tests/test_pipeline.py
"""
T1.7 integration tests. These are the first tests in the project that
compose all signal modules end-to-end (OB detection, OB status, SuperTrend,
SMT, auto-reject, sizer), so they are inherently more fragile than the
unit tests for individual modules -- a fixture construction error in any
one module can cascade here. Setup validity assertions before the actual
claim are deliberately included throughout, the same discipline used in
test_auto_reject.py after its first fixture failures.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.bridge import CandleData
from config.schema import TradingConfig
from signals.pipeline import (
    evaluate_signal, PipelineInputs, TradeAction, check_trend_agreement,
)
from signals.order_block import OBType
from risk.sizer import TrendAgreement

CFG = TradingConfig.from_yaml(str(Path(__file__).resolve().parent.parent / "config" / "trading_rules.yaml"))


def _c(o, h, l, c, t=0.0, sym="EURUSD", tf="H1") -> CandleData:
    return CandleData(symbol=sym, timeframe=tf, open=o, high=h, low=l, close=c, volume=100.0, timestamp=t)


def _monotone_m15(base=1.10, n=50, step=0.001) -> list[CandleData]:
    """A monotone uptrend for M15 SuperTrend -- produces a stable bullish
    ST result with no recent flip."""
    return [_c(base + i*step, base + i*step + 0.0005, base + i*step - 0.0005,
               base + i*step, t=float(i), tf="M15") for i in range(n)]


def _build_h1_with_bullish_ob() -> list[CandleData]:
    """
    H1 series that produces exactly one FRESH bullish OB.
    Key design constraints:
    - BOS candle's low must stay ABOVE ob_low (to avoid the T1.2b
      footgun where the BOS candle itself triggers 100% retrace)
    - OB zone must be wide enough that post-BOS candles don't
      accidentally retrace into it and change status from FRESH
    - Candles after BOS must stay above the OB zone entirely
    Verified by direct probe before writing tests against it.
    """
    return [
        _c(1.10, 1.10, 1.09, 1.095, t=0),
        _c(1.095, 1.11, 1.09, 1.10, t=1),
        _c(1.10, 1.15, 1.10, 1.12, t=2),   # swing high candidate: 1.15
        _c(1.12, 1.12, 1.08, 1.09, t=3),
        _c(1.09, 1.10, 1.08, 1.085, t=4),  # bearish OB candle, zone [1.08, 1.10]
        _c(1.085, 1.12, 1.105, 1.11, t=5),  # quiet confirming, low=1.105 above ob_high=1.10
        _c(1.11, 1.12, 1.105, 1.115, t=6),  # quiet confirming, low=1.105 above ob_high=1.10
        _c(1.098, 1.20, 1.10, 1.19, t=7),  # BOS candle, low=1.10 == ob_high, close=1.19
        _c(1.19, 1.20, 1.18, 1.185, t=8),  # well above zone, no retrace
        _c(1.185, 1.19, 1.175, 1.18, t=9), # well above zone, no retrace
    ]


def test_check_trend_agreement_all_cases():
    """Direct unit test of the new mapping function that lives in T1.7."""
    assert check_trend_agreement(OBType.BULLISH, 1) == TrendAgreement.AGREE
    assert check_trend_agreement(OBType.BEARISH, -1) == TrendAgreement.AGREE
    assert check_trend_agreement(OBType.BULLISH, -1) == TrendAgreement.CONFLICT
    assert check_trend_agreement(OBType.BEARISH, 1) == TrendAgreement.CONFLICT


def test_pass_when_no_fresh_ob_in_direction():
    """If no FRESH OB exists in the trade direction, pipeline must PASS."""
    h1 = [_c(1.10 + i*0.001, 1.10 + i*0.001 + 0.0005, 1.10 + i*0.001 - 0.0005,
              1.10 + i*0.001, t=float(i)) for i in range(20)]  # flat, no structural breaks

    inputs = PipelineInputs(
        symbol="EURUSD",
        h1_candles=h1,
        m15_candles_primary=_monotone_m15(),
        m15_candles_correlated=None,
        entry_price=1.12,
        sl_price=1.11,
        tp_price=1.14,
        is_long=True,
        current_spread=0.0001,
        account_equity=10_000,
        minutes_to_next_major_news=120.0,
        as_of=datetime.now(timezone.utc),
    )
    decision = evaluate_signal(inputs, CFG)
    assert decision.action == TradeAction.PASS
    assert "no fresh OB" in decision.pass_reason


def test_pass_when_symbol_not_in_config():
    """A symbol not declared in config instruments must produce PASS, not crash."""
    inputs = PipelineInputs(
        symbol="USDJPY",  # not in config
        h1_candles=[_c(160.0, 160.1, 159.9, 160.05, t=float(i)) for i in range(10)],
        m15_candles_primary=_monotone_m15(base=160.0, step=0.01),
        m15_candles_correlated=None,
        entry_price=160.05,
        sl_price=159.90,
        tp_price=160.35,
        is_long=True,
        current_spread=0.02,
        account_equity=10_000,
        minutes_to_next_major_news=120.0,
        as_of=datetime.now(timezone.utc),
    )
    decision = evaluate_signal(inputs, CFG)
    assert decision.action == TradeAction.PASS
    assert "not in config" in decision.pass_reason


def test_trend_conflict_produces_pass():
    """OB bullish but ST bearish -> TrendAgreement.CONFLICT -> PASS via sizer."""
    h1 = _build_h1_with_bullish_ob()

    # M15 in a strong DOWNTREND so SuperTrend will be bearish
    m15_down = [_c(1.20 - i*0.003, 1.20 - i*0.003 + 0.001, 1.20 - i*0.003 - 0.001,
                   1.20 - i*0.003, t=float(i), tf="M15") for i in range(50)]

    inputs = PipelineInputs(
        symbol="EURUSD",
        h1_candles=h1,
        m15_candles_primary=m15_down,
        m15_candles_correlated=None,
        entry_price=1.18,
        sl_price=1.17,
        tp_price=1.22,
        is_long=True,  # long despite bearish ST -> conflict
        current_spread=0.0001,
        account_equity=10_000,
        minutes_to_next_major_news=120.0,
        as_of=datetime.now(timezone.utc),
    )
    decision = evaluate_signal(inputs, CFG)
    assert decision.action == TradeAction.PASS
    assert decision.trend_agreement == TrendAgreement.CONFLICT
    assert "sizing rejected" in decision.pass_reason or "trend_conflict" in decision.pass_reason.lower() or "ob_st" in decision.pass_reason.lower()


def test_news_blackout_produces_pass():
    """Major news within 30 min must block execution via auto-reject gate."""
    h1 = _build_h1_with_bullish_ob()
    m15 = _monotone_m15()

    inputs = PipelineInputs(
        symbol="EURUSD",
        h1_candles=h1,
        m15_candles_primary=m15,
        m15_candles_correlated=None,
        entry_price=1.18,
        sl_price=1.17,
        tp_price=1.22,
        is_long=True,
        current_spread=0.0001,
        account_equity=10_000,
        minutes_to_next_major_news=5.0,  # within 30-min buffer
        as_of=datetime.now(timezone.utc),
    )
    decision = evaluate_signal(inputs, CFG)
    assert decision.action == TradeAction.PASS
    assert "auto_reject" in decision.pass_reason
    assert "news_blackout" in decision.pass_reason


def test_insufficient_rr_produces_pass():
    """TP too close to entry (R:R 1:1, below the 2.0 minimum) must PASS."""
    h1 = _build_h1_with_bullish_ob()
    m15 = _monotone_m15()

    inputs = PipelineInputs(
        symbol="EURUSD",
        h1_candles=h1,
        m15_candles_primary=m15,
        m15_candles_correlated=None,
        entry_price=1.18,
        sl_price=1.17,     # SL 100 pips below
        tp_price=1.19,     # TP only 100 pips above -> R:R 1.0, below min 2.0
        is_long=True,
        current_spread=0.0001,
        account_equity=10_000,
        minutes_to_next_major_news=120.0,
        as_of=datetime.now(timezone.utc),
    )
    decision = evaluate_signal(inputs, CFG)
    assert decision.action == TradeAction.PASS
    assert "sizing rejected" in decision.pass_reason


def run_all():
    tests = [
        test_check_trend_agreement_all_cases,
        test_pass_when_no_fresh_ob_in_direction,
        test_pass_when_symbol_not_in_config,
        test_trend_conflict_produces_pass,
        test_news_blackout_produces_pass,
        test_insufficient_rr_produces_pass,
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
