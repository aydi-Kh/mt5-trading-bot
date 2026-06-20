# tests/test_order_block_status.py
"""
Tests signals/order_block_status.py. Priority: the wick-vs-close
distinction between FULLY_MITIGATED and BROKEN, since that's the part of
the T1.2b spec most likely to be implemented wrong (easy to accidentally
use high/low instead of close, or vice versa, for the BROKEN check).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.bridge import CandleData
from signals.order_block import OrderBlock, OBType
from signals.order_block_status import assess_ob_status, assess_ob_statuses, OBStatus


def _c(o, h, l, c, t=0.0) -> CandleData:
    return CandleData(symbol="EURUSD", timeframe="H1", open=o, high=h, low=l, close=c, volume=100.0, timestamp=t)


def _bull_ob(ob_low=1.08, ob_high=1.10, ob_idx=2) -> OrderBlock:
    """A bullish OB with zone [1.08, 1.10], formed at index ob_idx."""
    return OrderBlock(
        ob_high=ob_high, ob_low=ob_low, ob_type=OBType.BULLISH,
        bos_candle_index=ob_idx + 1, ob_candle_index=ob_idx,
        bos_timestamp=0.0, ob_candle_timestamp=0.0,
    )


def _bear_ob(ob_low=1.10, ob_high=1.12, ob_idx=2) -> OrderBlock:
    return OrderBlock(
        ob_high=ob_high, ob_low=ob_low, ob_type=OBType.BEARISH,
        bos_candle_index=ob_idx + 1, ob_candle_index=ob_idx,
        bos_timestamp=0.0, ob_candle_timestamp=0.0,
    )


def test_fresh_when_price_never_returns_to_zone():
    ob = _bull_ob()
    # All candles after formation stay well above the zone [1.08, 1.10]
    candles = [_c(0,0,0,0), _c(0,0,0,0), _c(1.08,1.10,1.08,1.09),  # index 2: the OB candle itself
               _c(1.15, 1.16, 1.14, 1.155), _c(1.155, 1.17, 1.15, 1.165)]
    result = assess_ob_status(ob, candles)
    assert result.status == OBStatus.FRESH
    assert result.max_retrace_pct == 0.0


def test_fresh_when_retrace_at_exactly_threshold():
    """Retrace exactly AT the freshness_threshold (20%) should be FRESH,
    not PARTIAL_MITIGATED -- gate is '> threshold', not '>= threshold'."""
    ob = _bull_ob(ob_low=1.08, ob_high=1.10)  # zone height = 0.02
    # 20% retrace means low reaches ob_high - 0.20*0.02 = 1.10 - 0.004 = 1.096
    candles = [_c(0,0,0,0), _c(0,0,0,0), _c(1.08,1.10,1.08,1.09),
               _c(1.15, 1.16, 1.096, 1.10)]  # low=1.096 exactly
    result = assess_ob_status(ob, candles, freshness_threshold=0.20)
    assert abs(result.max_retrace_pct - 0.20) < 1e-9, f"expected retrace exactly 0.20, got {result.max_retrace_pct}"
    assert result.status == OBStatus.FRESH, f"expected FRESH at exactly threshold, got {result.status}"


def test_partial_mitigated_above_threshold():
    ob = _bull_ob(ob_low=1.08, ob_high=1.10)
    # 50% retrace: low reaches 1.10 - 0.5*0.02 = 1.09
    candles = [_c(0,0,0,0), _c(0,0,0,0), _c(1.08,1.10,1.08,1.09),
               _c(1.10, 1.10, 1.09, 1.095)]
    result = assess_ob_status(ob, candles, freshness_threshold=0.20)
    assert result.status == OBStatus.PARTIAL_MITIGATED
    assert abs(result.max_retrace_pct - 0.5) < 1e-9


def test_fully_mitigated_wick_only_no_close_beyond():
    """Wick pierces fully through the zone (retrace >= 100%) but the
    candle's CLOSE stays within or above the zone -- must be
    FULLY_MITIGATED, not BROKEN. This is the core wick-vs-close distinction."""
    ob = _bull_ob(ob_low=1.08, ob_high=1.10)
    # Wick low = 1.07 (below ob_low, 100%+ retrace), but close = 1.085 (still inside zone, not beyond ob_low)
    candles = [_c(0,0,0,0), _c(0,0,0,0), _c(1.08,1.10,1.08,1.09),
               _c(1.10, 1.10, 1.07, 1.085)]
    result = assess_ob_status(ob, candles)
    assert result.max_retrace_pct >= 1.0, f"expected retrace >= 1.0 (wick fully through), got {result.max_retrace_pct}"
    assert result.status == OBStatus.FULLY_MITIGATED, (
        f"expected FULLY_MITIGATED for wick-through-but-close-inside, got {result.status}"
    )


def test_broken_when_close_goes_beyond_zone():
    """Same deep wick penetration as above, but this time the candle's
    CLOSE also goes beyond ob_low -- must be BROKEN."""
    ob = _bull_ob(ob_low=1.08, ob_high=1.10)
    candles = [_c(0,0,0,0), _c(0,0,0,0), _c(1.08,1.10,1.08,1.09),
               _c(1.10, 1.10, 1.07, 1.075)]  # close=1.075, below ob_low=1.08
    result = assess_ob_status(ob, candles)
    assert result.status == OBStatus.BROKEN, f"expected BROKEN when close < ob_low, got {result.status}"


def test_broken_persists_even_if_later_candle_retraces_less():
    """Once BROKEN (a close went beyond), a LATER candle with a smaller
    wick must not 'un-break' the assessment -- BROKEN is sticky once
    triggered within the scan window."""
    ob = _bull_ob(ob_low=1.08, ob_high=1.10)
    candles = [
        _c(0,0,0,0), _c(0,0,0,0), _c(1.08,1.10,1.08,1.09),
        _c(1.10, 1.10, 1.07, 1.075),  # close beyond -> BROKEN triggers here
        _c(1.075, 1.12, 1.075, 1.11),  # later candle moves back up, doesn't touch zone at all
    ]
    result = assess_ob_status(ob, candles)
    assert result.status == OBStatus.BROKEN, (
        f"expected BROKEN to persist despite a later non-touching candle, got {result.status}"
    )


def test_bearish_ob_mirror_logic():
    """Confirm the bearish OB path (zone above price, retrace = price
    moving UP into the zone, BROKEN = close ABOVE ob_high) mirrors the
    bullish logic correctly, not just copy-paste with a sign error."""
    ob = _bear_ob(ob_low=1.10, ob_high=1.12)  # zone height 0.02
    # Wick high = 1.13 (beyond ob_high, full retrace), close = 1.125 (beyond ob_high too) -> BROKEN
    candles = [_c(0,0,0,0), _c(0,0,0,0), _c(1.10,1.12,1.10,1.11),
               _c(1.10, 1.13, 1.10, 1.125)]
    result = assess_ob_status(ob, candles)
    assert result.status == OBStatus.BROKEN, f"expected BROKEN for bearish OB with close above ob_high, got {result.status}"


def test_bearish_ob_fully_mitigated_wick_only():
    ob = _bear_ob(ob_low=1.10, ob_high=1.12)
    # Wick high = 1.13 (beyond), close = 1.115 (still inside zone, not beyond ob_high)
    candles = [_c(0,0,0,0), _c(0,0,0,0), _c(1.10,1.12,1.10,1.11),
               _c(1.10, 1.13, 1.10, 1.115)]
    result = assess_ob_status(ob, candles)
    assert result.status == OBStatus.FULLY_MITIGATED, (
        f"expected FULLY_MITIGATED for bearish OB wick-through-close-inside, got {result.status}"
    )


def test_max_retrace_tracked_even_when_already_broken():
    """If a candle breaks the zone with a modest retrace (e.g. exactly
    100%) but a LATER candle pierces much deeper (e.g. 150%) without
    itself closing beyond, max_retrace_pct should reflect the deeper
    value -- confirms the loop doesn't stop tracking max once broken=True."""
    ob = _bull_ob(ob_low=1.08, ob_high=1.10)  # height 0.02
    candles = [
        _c(0,0,0,0), _c(0,0,0,0), _c(1.08,1.10,1.08,1.09),
        _c(1.10, 1.10, 1.08, 1.075),   # low=1.08 (exactly 100% retrace), close=1.075 -> BROKEN
        _c(1.075, 1.08, 1.07, 1.078),  # low=1.07 -> retrace = (1.10-1.07)/0.02 = 1.5 (150%)
    ]
    result = assess_ob_status(ob, candles)
    assert result.status == OBStatus.BROKEN
    assert result.max_retrace_pct >= 1.5 - 1e-9, (
        f"expected max_retrace_pct to reflect the deeper later penetration (1.5), got {result.max_retrace_pct}"
    )


def test_no_candles_after_formation_is_fresh():
    """OB formed on the very last candle in the series -- nothing to
    scan yet, must be FRESH with 0 candles_since_formation, not error."""
    ob = _bull_ob(ob_idx=2)
    candles = [_c(0,0,0,0), _c(0,0,0,0), _c(1.08,1.10,1.08,1.09)]  # OB candle is the last one
    result = assess_ob_status(ob, candles)
    assert result.status == OBStatus.FRESH
    assert result.candles_since_formation == 0


def test_batch_excludes_obs_older_than_max_age():
    """assess_ob_statuses must silently exclude OBs whose ob_candle_index
    falls before the max_age_candles cutoff from the end of the series."""
    old_ob = _bull_ob(ob_idx=0)    # very old
    recent_ob = _bull_ob(ob_idx=8)  # near the end
    candles = [_c(1.08+i*0.001, 1.10+i*0.001, 1.08+i*0.001, 1.09+i*0.001, t=i) for i in range(10)]

    results = assess_ob_statuses([old_ob, recent_ob], candles, max_age_candles=3)
    result_obs = [r.order_block for r in results]
    assert old_ob not in result_obs, "expected old OB (index 0) to be excluded with max_age_candles=3 on a 10-candle series"
    assert recent_ob in result_obs, "expected recent OB (index 8) to be included"


def test_batch_empty_candles_returns_empty():
    ob = _bull_ob()
    results = assess_ob_statuses([ob], [], max_age_candles=200)
    assert results == [], "expected empty result for empty candle series, not an error"


def test_misaligned_candles_raises_instead_of_silently_wrong():
    """
    Directly tests the index-alignment footgun documented in the module
    docstring: confirmed via separate manual probing that passing a
    re-sliced candles list silently produces a wrong-but-plausible-looking
    result. This test confirms the defensive bounds check added afterward
    converts the most detectable case (index now fully out of range) into
    a loud ValueError instead.
    """
    ob = _bull_ob(ob_idx=8)  # OB formed late in a 10-candle series
    short_series = [_c(1.08, 1.10, 1.08, 1.09, t=i) for i in range(5)]  # too short, index 8 doesn't exist
    try:
        assess_ob_status(ob, short_series)
        assert False, "expected ValueError for ob_candle_index out of range, got a silent result instead"
    except ValueError as e:
        assert "out of range" in str(e)


def run_all():
    tests = [
        test_fresh_when_price_never_returns_to_zone,
        test_fresh_when_retrace_at_exactly_threshold,
        test_partial_mitigated_above_threshold,
        test_fully_mitigated_wick_only_no_close_beyond,
        test_broken_when_close_goes_beyond_zone,
        test_broken_persists_even_if_later_candle_retraces_less,
        test_bearish_ob_mirror_logic,
        test_bearish_ob_fully_mitigated_wick_only,
        test_max_retrace_tracked_even_when_already_broken,
        test_no_candles_after_formation_is_fresh,
        test_batch_excludes_obs_older_than_max_age,
        test_batch_empty_candles_returns_empty,
        test_misaligned_candles_raises_instead_of_silently_wrong,
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
