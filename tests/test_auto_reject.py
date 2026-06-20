# tests/test_auto_reject.py
"""
Tests signals/auto_reject.py. Priority: confirm delegation to
OrderBlockAssessment.status and SuperTrendResult.is_flip_within actually
works against REAL objects produced by those modules (not hand-built
mocks that might not match the real shape), the conservative None-news
handling, and that multiple simultaneous triggers are all reported.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.bridge import CandleData
from signals.order_block import OrderBlock, OBType, detect_order_blocks
from signals.order_block_status import assess_ob_status, OBStatus
from signals.supertrend import compute_supertrend
from signals.auto_reject import check_auto_reject, RejectCondition


def _c(o, h, l, c, t=0.0) -> CandleData:
    return CandleData(symbol="EURUSD", timeframe="H1", open=o, high=h, low=l, close=c, volume=100.0, timestamp=t)


def _fresh_ob_assessment():
    """Builds a real, FRESH OrderBlockAssessment using actual T1.2a/b
    functions, not a hand-constructed mock -- confirms auto_reject.py's
    delegation works against the real output shape."""
    candles = [
        _c(1.10, 1.10, 1.09, 1.095, t=0),
        _c(1.095, 1.11, 1.09, 1.10, t=1),
        _c(1.10, 1.15, 1.10, 1.12, t=2),
        _c(1.12, 1.12, 1.08, 1.09, t=3),
        _c(1.09, 1.10, 1.08, 1.085, t=4),  # OB candle
        _c(1.085, 1.20, 1.08, 1.18, t=5),  # BOS candle
        _c(1.18, 1.19, 1.17, 1.175, t=6),  # price stays well clear, never retraces
    ]
    obs = detect_order_blocks(candles, fractal_window=2)
    bullish = [ob for ob in obs if ob.ob_type == OBType.BULLISH][0]
    assessment = assess_ob_status(bullish, candles)
    return assessment, bullish


def _stable_supertrend():
    """Real SuperTrendResult from a clean, unbroken uptrend -- no flip
    within the last several candles."""
    candles = [_c(1.10 + i * 0.0050, 1.10 + i * 0.0050 + 0.0005, 1.10 + i * 0.0050 - 0.0005, 1.10 + i * 0.0050, t=i) for i in range(40)]
    return compute_supertrend(candles, atr_period=10, multiplier=3.0)


def _flipped_supertrend():
    """Real SuperTrendResult from a series that flips direction on the
    very last two candles -- confirmed is_flip_within(2) == True via
    direct probing before adding this fixture. The original fixture
    reused the up/down series from test_supertrend.py, but that series
    ends with a long stable bearish trend (the flip was many candles back)
    so is_flip_within(2) was False at the series end -- wrong fixture."""
    # Long stable uptrend, then a sharp reversal on the FINAL two candles.
    # Use a large enough reversal that SuperTrend's ATR-based band can't
    # absorb it and must flip direction.
    closes = [1.1000 + i * 0.0030 for i in range(30)]  # stable up
    closes.append(closes[-1] - 0.15)  # massive drop, forces flip
    closes.append(closes[-1] - 0.05)  # continues down, second candle
    candles = [_c(c, c + 0.001, c - 0.001, c, t=i) for i, c in enumerate(closes)]
    st = compute_supertrend(candles, atr_period=10, multiplier=3.0)
    return st


def test_passes_with_all_conditions_favorable():
    assessment, ob = _fresh_ob_assessment()
    st = _stable_supertrend()
    ob_size = ob.ob_high - ob.ob_low
    result = check_auto_reject(
        ob_assessment=assessment,
        supertrend_result=st,
        spread=0.0001,
        ob_size=ob_size,
        minutes_to_next_major_news=120.0,
    )
    assert result.passed is True, f"expected pass, got triggered={result.triggered_conditions}"
    assert result.triggered_conditions == []


def test_mitigated_ob_rejects_using_real_t1_2b_output():
    """Build a real PARTIAL_MITIGATED assessment (not a mock) and confirm
    the gate correctly delegates to it."""
    candles = [
        _c(1.10, 1.10, 1.09, 1.095, t=0),
        _c(1.095, 1.11, 1.09, 1.10, t=1),
        _c(1.10, 1.15, 1.10, 1.12, t=2),
        _c(1.12, 1.12, 1.08, 1.09, t=3),
        _c(1.09, 1.10, 1.08, 1.085, t=4),  # OB candle, zone [1.08, 1.10]
        # BOS candle: low must stay well ABOVE ob_low (1.08) so the BOS
        # candle itself doesn't accidentally register as a 100% retrace
        # into the zone -- the original fixture had low=1.08 here which
        # caused FULLY_MITIGATED before the intended partial-retrace
        # candle was even reached. Fixed: low=1.085 (safely above ob_low).
        _c(1.085, 1.20, 1.085, 1.18, t=5),  # BOS, low=1.085 (above ob_low=1.08)
        _c(1.18, 1.18, 1.092, 1.095, t=6),  # retraces 40% into zone -> PARTIAL_MITIGATED
    ]
    obs = detect_order_blocks(candles, fractal_window=2)
    bullish = [ob for ob in obs if ob.ob_type == OBType.BULLISH][0]
    assessment = assess_ob_status(bullish, candles)
    assert assessment.status == OBStatus.PARTIAL_MITIGATED, (
        f"test setup invalid -- expected PARTIAL_MITIGATED, got {assessment.status} "
        f"(max_retrace={assessment.max_retrace_pct})"
    )

    st = _stable_supertrend()
    result = check_auto_reject(
        ob_assessment=assessment, supertrend_result=st, spread=0.0001,
        ob_size=bullish.ob_high - bullish.ob_low, minutes_to_next_major_news=120.0,
    )
    assert result.passed is False
    assert RejectCondition.MITIGATED_OB in result.triggered_conditions


def test_unstable_supertrend_rejects_using_real_t1_3_output():
    assessment, ob = _fresh_ob_assessment()
    st = _flipped_supertrend()
    assert st.is_flip_within(2) is True, "test setup invalid -- expected a recent flip"

    result = check_auto_reject(
        ob_assessment=assessment, supertrend_result=st, spread=0.0001,
        ob_size=ob.ob_high - ob.ob_low, minutes_to_next_major_news=120.0,
    )
    assert result.passed is False
    assert RejectCondition.UNSTABLE_SUPERTREND in result.triggered_conditions


def test_spread_prohibitive_when_spread_exceeds_threshold():
    assessment, ob = _fresh_ob_assessment()
    st = _stable_supertrend()
    ob_size = ob.ob_high - ob.ob_low  # 0.02 in this fixture
    result = check_auto_reject(
        ob_assessment=assessment, supertrend_result=st,
        spread=ob_size * 0.50,  # 50% of OB size, exceeds the 30% default threshold
        ob_size=ob_size, minutes_to_next_major_news=120.0,
    )
    assert result.passed is False
    assert RejectCondition.SPREAD_PROHIBITIVE in result.triggered_conditions


def test_spread_exactly_at_threshold_passes():
    """Boundary: spread exactly equal to 30% of ob_size should PASS, gate
    is strictly '>', not '>='."""
    assessment, ob = _fresh_ob_assessment()
    st = _stable_supertrend()
    ob_size = ob.ob_high - ob.ob_low
    result = check_auto_reject(
        ob_assessment=assessment, supertrend_result=st,
        spread=ob_size * 0.30,  # exactly at threshold
        ob_size=ob_size, minutes_to_next_major_news=120.0,
    )
    assert RejectCondition.SPREAD_PROHIBITIVE not in result.triggered_conditions, (
        f"expected spread exactly at 30% threshold to pass, got triggered={result.triggered_conditions}"
    )


def test_zero_ob_size_rejects_conservatively():
    assessment, ob = _fresh_ob_assessment()
    st = _stable_supertrend()
    result = check_auto_reject(
        ob_assessment=assessment, supertrend_result=st,
        spread=0.0001, ob_size=0.0, minutes_to_next_major_news=120.0,
    )
    assert result.passed is False
    assert RejectCondition.SPREAD_PROHIBITIVE in result.triggered_conditions


def test_news_blackout_when_within_buffer():
    assessment, ob = _fresh_ob_assessment()
    st = _stable_supertrend()
    result = check_auto_reject(
        ob_assessment=assessment, supertrend_result=st, spread=0.0001,
        ob_size=ob.ob_high - ob.ob_low, minutes_to_next_major_news=15.0,  # within 30 min buffer
    )
    assert result.passed is False
    assert RejectCondition.NEWS_BLACKOUT in result.triggered_conditions


def test_news_blackout_when_data_unavailable_none():
    """Missing news data (None) must FAIL conservatively, not silently
    pass -- this is the explicit 'fail loud on missing data' design
    decision documented in the source."""
    assessment, ob = _fresh_ob_assessment()
    st = _stable_supertrend()
    result = check_auto_reject(
        ob_assessment=assessment, supertrend_result=st, spread=0.0001,
        ob_size=ob.ob_high - ob.ob_low, minutes_to_next_major_news=None,
    )
    assert result.passed is False, "expected None news data to fail conservatively, not pass silently"
    assert RejectCondition.NEWS_BLACKOUT in result.triggered_conditions


def test_news_exactly_at_buffer_boundary_passes():
    """Exactly 30 minutes to news should PASS, gate is strictly '<', not '<='."""
    assessment, ob = _fresh_ob_assessment()
    st = _stable_supertrend()
    result = check_auto_reject(
        ob_assessment=assessment, supertrend_result=st, spread=0.0001,
        ob_size=ob.ob_high - ob.ob_low, minutes_to_next_major_news=30.0,
    )
    assert RejectCondition.NEWS_BLACKOUT not in result.triggered_conditions, (
        f"expected exactly 30min to pass (buffer is exclusive), got triggered={result.triggered_conditions}"
    )


def test_multiple_simultaneous_triggers_all_reported():
    """When several conditions fail at once, ALL must appear in
    triggered_conditions, not just the first one encountered."""
    candles = [
        _c(1.10, 1.10, 1.09, 1.095, t=0),
        _c(1.095, 1.11, 1.09, 1.10, t=1),
        _c(1.10, 1.15, 1.10, 1.12, t=2),
        _c(1.12, 1.12, 1.08, 1.09, t=3),
        _c(1.09, 1.10, 1.08, 1.085, t=4),
        _c(1.085, 1.20, 1.085, 1.18, t=5),  # BOS, low fixed above ob_low
        _c(1.18, 1.18, 1.092, 1.095, t=6),  # PARTIAL_MITIGATED
    ]
    obs = detect_order_blocks(candles, fractal_window=2)
    bullish = [ob for ob in obs if ob.ob_type == OBType.BULLISH][0]
    assessment = assess_ob_status(bullish, candles)
    assert assessment.status == OBStatus.PARTIAL_MITIGATED, (
        f"setup invalid: expected PARTIAL_MITIGATED, got {assessment.status}"
    )
    st = _flipped_supertrend()
    assert st.is_flip_within(2) is True, (
        f"setup invalid: expected recent ST flip, got directions={[p.direction for p in st.points[-3:]]}"
    )

    result = check_auto_reject(
        ob_assessment=assessment, supertrend_result=st,
        spread=10.0, ob_size=0.02,  # spread-prohibitive
        minutes_to_next_major_news=5.0,  # news blackout
    )
    assert result.passed is False
    assert set(result.triggered_conditions) == {
        RejectCondition.MITIGATED_OB,
        RejectCondition.UNSTABLE_SUPERTREND,
        RejectCondition.SPREAD_PROHIBITIVE,
        RejectCondition.NEWS_BLACKOUT,
    }, f"expected all 4 conditions triggered, got {result.triggered_conditions}"


def run_all():
    tests = [
        test_passes_with_all_conditions_favorable,
        test_mitigated_ob_rejects_using_real_t1_2b_output,
        test_unstable_supertrend_rejects_using_real_t1_3_output,
        test_spread_prohibitive_when_spread_exceeds_threshold,
        test_spread_exactly_at_threshold_passes,
        test_zero_ob_size_rejects_conservatively,
        test_news_blackout_when_within_buffer,
        test_news_blackout_when_data_unavailable_none,
        test_news_exactly_at_buffer_boundary_passes,
        test_multiple_simultaneous_triggers_all_reported,
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
