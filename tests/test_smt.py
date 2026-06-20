# tests/test_smt.py
"""
Tests signals/smt.py. Priority: confirmed-vs-divergent classification
correctness, the "freshness" requirement (only the current/last candle's
extreme counts, not a stale one from earlier in the window), and the
divergence magnitude calculation since it's the most arithmetically
involved part of this module.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.bridge import CandleData
from signals.smt import assess_smt, SMTResult, PriceDirection


def _c(close, t=0.0) -> CandleData:
    # Only close matters for this module's logic; other OHLC fields are
    # filled with the same value since they're unused by assess_smt.
    return CandleData(symbol="X", timeframe="M15", open=close, high=close, low=close, close=close, volume=100.0, timestamp=t)


def _series(closes: list[float]) -> list[CandleData]:
    return [_c(v, t=i) for i, v in enumerate(closes)]


def test_returns_none_when_too_short():
    primary = _series([1.10])
    correlated = _series([1.30])
    assert assess_smt(primary, correlated) is None


def test_returns_none_when_primary_not_making_fresh_extreme():
    """Primary's last close is NOT the max or min of the window -- price
    pulled back after an earlier extreme -- nothing to assess right now."""
    primary = _series([1.10, 1.15, 1.20, 1.12])  # high was 1.20 at index 2, pulled back since
    correlated = _series([1.30, 1.32, 1.35, 1.31])
    result = assess_smt(primary, correlated, lookback_candles=4)
    assert result is None, "expected None when primary's last close isn't a fresh extreme"


def test_confirmed_when_both_make_new_high_together():
    primary = _series([1.10, 1.12, 1.14, 1.16])      # new high on last candle
    correlated = _series([1.30, 1.32, 1.34, 1.36])   # also new high on last candle
    result = assess_smt(primary, correlated, lookback_candles=4)
    assert result is not None
    assert result.result == SMTResult.CONFIRMED
    assert result.direction_checked == PriceDirection.UP
    assert result.divergence_magnitude_pct == 0.0


def test_confirmed_when_both_make_new_low_together():
    primary = _series([1.16, 1.14, 1.12, 1.10])
    correlated = _series([1.36, 1.34, 1.32, 1.30])
    result = assess_smt(primary, correlated, lookback_candles=4)
    assert result is not None
    assert result.result == SMTResult.CONFIRMED
    assert result.direction_checked == PriceDirection.DOWN


def test_divergent_when_primary_makes_new_high_correlated_does_not():
    """Classic SMT divergence: primary pushes to a fresh high, correlated
    pair's last close is NOT its window high (it topped out earlier and
    is now lower) -- strong divergence signal."""
    primary = _series([1.10, 1.12, 1.14, 1.20])       # fresh, strong new high
    correlated = _series([1.30, 1.36, 1.34, 1.32])    # correlated topped at index 1, now lower
    result = assess_smt(primary, correlated, lookback_candles=4, divergence_threshold_pct=0.15)
    assert result is not None
    assert result.result == SMTResult.DIVERGENT, (
        f"expected DIVERGENT, got {result.result} (magnitude={result.divergence_magnitude_pct})"
    )


def test_weak_divergence_below_threshold_classified_as_confirmed():
    """If the correlated pair almost confirms (small shortfall, below
    divergence_threshold_pct), the result should be CONFIRMED, not
    DIVERGENT -- the threshold exists specifically to filter out noise-
    level discrepancies from genuine divergence."""
    primary = _series([1.1000, 1.1010, 1.1020, 1.1030])      # 30 pip move
    correlated = _series([1.3000, 1.3010, 1.3019, 1.3029])   # 29 pip move, last close (1.3029) IS its window max
    # correlated's last close 1.3029 is still its own window max, so this
    # is actually CONFIRMED outright (correlated_made_extreme=True) --
    # use a case where correlated's max occurred earlier instead, by a
    # tiny margin, to genuinely test the threshold boundary.
    correlated2 = _series([1.3000, 1.3029, 1.3025, 1.3028])  # max=1.3029 at index1, last=1.3028 (barely short)
    result = assess_smt(primary, correlated2, lookback_candles=4, divergence_threshold_pct=0.50)
    assert result is not None
    assert result.result == SMTResult.CONFIRMED, (
        f"expected CONFIRMED for a shortfall well below the 0.50 threshold, "
        f"got {result.result} (magnitude={result.divergence_magnitude_pct})"
    )


def test_divergence_magnitude_is_zero_for_confirmed():
    primary = _series([1.10, 1.12, 1.14, 1.16])
    correlated = _series([1.30, 1.32, 1.34, 1.36])
    result = assess_smt(primary, correlated, lookback_candles=4)
    assert result.divergence_magnitude_pct == 0.0


def test_lookback_window_restricts_to_recent_candles_only():
    """A divergence that existed further back than lookback_candles must
    not affect the current assessment -- only the most recent N candles
    should be considered."""
    # Long history with an old divergence pattern, but the last 4 candles
    # (lookback=4) show clean confirmation.
    primary = _series([1.50, 1.10, 1.12, 1.14, 1.16])     # old high of 1.50 far in the past
    correlated = _series([1.30, 1.30, 1.32, 1.34, 1.36])  # confirms within the recent window
    result = assess_smt(primary, correlated, lookback_candles=4)
    assert result is not None
    assert result.result == SMTResult.CONFIRMED, (
        "expected the stale old extreme (outside the lookback window) to be ignored"
    )


def test_correlated_pair_making_its_own_fresh_extreme_independently_is_confirmed():
    """If the correlated pair's last close IS its own window extreme in
    the same direction the primary moved, that's confirmation even if
    the exact magnitudes differ a lot."""
    primary = _series([1.10, 1.11, 1.12, 1.13])
    correlated = _series([1.30, 1.50, 1.70, 1.90])  # much bigger move, but still confirms (own last=own max)
    result = assess_smt(primary, correlated, lookback_candles=4)
    assert result.result == SMTResult.CONFIRMED


def test_classic_divergence_correlated_makes_opposite_extreme():
    """The textbook SMT divergence case: primary makes a fresh HIGH while
    the correlated pair simultaneously makes its OWN fresh LOW (not just
    'fails to confirm', but actively diverges in the opposite direction).
    Must be classified DIVERGENT with maximal magnitude, not partially
    or ambiguously handled."""
    primary = _series([1.10, 1.12, 1.14, 1.20])     # fresh high
    correlated = _series([1.36, 1.34, 1.32, 1.20])  # fresh LOW on same last candle
    result = assess_smt(primary, correlated, lookback_candles=4, divergence_threshold_pct=0.15)
    assert result is not None
    assert result.result == SMTResult.DIVERGENT
    assert result.direction_checked == PriceDirection.UP
    assert result.divergence_magnitude_pct >= 0.99, (
        f"expected near-maximal divergence magnitude for an outright "
        f"opposite-direction move, got {result.divergence_magnitude_pct}"
    )


def run_all():
    tests = [
        test_returns_none_when_too_short,
        test_returns_none_when_primary_not_making_fresh_extreme,
        test_confirmed_when_both_make_new_high_together,
        test_confirmed_when_both_make_new_low_together,
        test_divergent_when_primary_makes_new_high_correlated_does_not,
        test_weak_divergence_below_threshold_classified_as_confirmed,
        test_divergence_magnitude_is_zero_for_confirmed,
        test_lookback_window_restricts_to_recent_candles_only,
        test_correlated_pair_making_its_own_fresh_extreme_independently_is_confirmed,
        test_classic_divergence_correlated_makes_opposite_extreme,
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
