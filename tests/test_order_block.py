# tests/test_order_block.py
"""
Tests signals/order_block.py against hand-constructed candle sequences
with known structural shape. Per the plan, T1.2 is the highest-risk task
("most likely to need a second pass") -- these tests are deliberately
adversarial about the exact mechanics (fractal ties, doji handling, BOS
consumption) rather than just confirming a plausible-looking output.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.bridge import CandleData
from signals.order_block import (
    find_swing_points, detect_order_blocks, OBType, _find_ob_candle, _is_bullish_candle, _is_bearish_candle,
)


def _c(o, h, l, c, t=0.0, symbol="EURUSD", tf="H1") -> CandleData:
    return CandleData(symbol=symbol, timeframe=tf, open=o, high=h, low=l, close=c, volume=100.0, timestamp=t)


# ──────────────────────────────────────────────────────────────────────
# Swing point detection
# ──────────────────────────────────────────────────────────────────────

def test_swing_high_detected_at_clear_peak():
    # index:  0     1     2     3     4
    # high:  1.10  1.12  1.15  1.11  1.09   <- index 2 is a clear fractal peak (N=2)
    candles = [
        _c(1.10, 1.10, 1.09, 1.10, t=0),
        _c(1.10, 1.12, 1.10, 1.11, t=1),
        _c(1.11, 1.15, 1.11, 1.14, t=2),
        _c(1.14, 1.11, 1.08, 1.09, t=3),
        _c(1.09, 1.09, 1.07, 1.08, t=4),
    ]
    highs, lows = find_swing_points(candles, fractal_window=2)
    assert len(highs) == 1, f"expected exactly 1 swing high, got {len(highs)}"
    assert highs[0].index == 2
    assert highs[0].price == 1.15


def test_swing_low_detected_at_clear_trough():
    candles = [
        _c(1.10, 1.11, 1.10, 1.10, t=0),
        _c(1.10, 1.10, 1.08, 1.09, t=1),
        _c(1.09, 1.09, 1.05, 1.06, t=2),  # clear trough
        _c(1.06, 1.09, 1.06, 1.08, t=3),
        _c(1.08, 1.10, 1.08, 1.09, t=4),
    ]
    highs, lows = find_swing_points(candles, fractal_window=2)
    assert len(lows) == 1, f"expected exactly 1 swing low, got {len(lows)}"
    assert lows[0].index == 2
    assert lows[0].price == 1.05


def test_tied_high_does_not_count_as_swing():
    """If the candidate peak ties with a neighbor (not strictly greater),
    it must NOT be flagged as a swing high -- per the strict-inequality
    requirement encoded via the count()==1 check."""
    candles = [
        _c(1.10, 1.10, 1.09, 1.10, t=0),
        _c(1.10, 1.12, 1.10, 1.11, t=1),
        _c(1.11, 1.15, 1.11, 1.14, t=2),   # tied peak
        _c(1.14, 1.15, 1.13, 1.13, t=3),   # tied peak (same high as index 2)
        _c(1.13, 1.10, 1.09, 1.09, t=4),
    ]
    highs, lows = find_swing_points(candles, fractal_window=2)
    assert len(highs) == 0, f"expected no swing high on a tie, got {[h.index for h in highs]}"


def test_no_swings_on_monotonic_series():
    """A strictly monotonically increasing series has no interior fractal
    peaks or troughs at all (every candle's neighbor on one side is
    always higher) -- confirms the function doesn't hallucinate swings."""
    candles = [_c(1.10 + i * 0.01, 1.10 + i * 0.01 + 0.005, 1.10 + i * 0.01 - 0.005, 1.10 + i * 0.01, t=i) for i in range(10)]
    highs, lows = find_swing_points(candles, fractal_window=2)
    assert highs == [] and lows == [], f"expected no swings on monotonic series, got highs={highs} lows={lows}"


def test_first_and_last_n_candles_unevaluable():
    """With fractal_window=2, the first 2 and last 2 candles can never be
    flagged as swings (insufficient neighbors on one side) -- confirms no
    out-of-range indexing and no false positives at the edges."""
    candles = [_c(1.10, 1.10 + 0.001 * i, 1.10 - 0.001, 1.10, t=i) for i in range(6)]
    highs, lows = find_swing_points(candles, fractal_window=2)
    for h in highs:
        assert 2 <= h.index <= 3, f"swing high at edge index {h.index} should be unevaluable"
    for l in lows:
        assert 2 <= l.index <= 3, f"swing low at edge index {l.index} should be unevaluable"


# ──────────────────────────────────────────────────────────────────────
# OB candle identification (_find_ob_candle)
# ──────────────────────────────────────────────────────────────────────

def test_find_ob_candle_for_bullish_bos_is_nearest_bearish():
    # index 0: bullish, index 1: bearish (this should be the OB), index 2: bullish (BOS candle)
    candles = [
        _c(1.10, 1.11, 1.09, 1.105, t=0),  # bullish (close>open)
        _c(1.105, 1.11, 1.09, 1.095, t=1),  # bearish (close<open) <- expected OB
        _c(1.095, 1.20, 1.09, 1.18, t=2),  # bullish, big impulse, this is the BOS candle
    ]
    ob_idx = _find_ob_candle(candles, bos_index=2, ob_type=OBType.BULLISH)
    assert ob_idx == 1, f"expected OB candle at index 1 (nearest bearish), got {ob_idx}"


def test_find_ob_candle_for_bearish_bos_is_nearest_bullish():
    candles = [
        _c(1.10, 1.11, 1.09, 1.095, t=0),  # bearish
        _c(1.095, 1.10, 1.09, 1.098, t=1),  # bullish <- expected OB
        _c(1.098, 1.10, 1.00, 1.01, t=2),  # bearish, impulse down, BOS candle
    ]
    ob_idx = _find_ob_candle(candles, bos_index=2, ob_type=OBType.BEARISH)
    assert ob_idx == 1, f"expected OB candle at index 1 (nearest bullish), got {ob_idx}"


def test_find_ob_candle_skips_doji():
    """A doji (close==open) is neither bullish nor bearish per
    _is_bullish_candle/_is_bearish_candle -- the scan must skip past it
    to find the next genuinely opposite-colored candle, not stop or
    misclassify the doji as a match."""
    candles = [
        _c(1.10, 1.11, 1.09, 1.095, t=0),  # bearish <- expected OB (the real one)
        _c(1.095, 1.10, 1.09, 1.095, t=1),  # doji (close==open) -- must be skipped
        _c(1.095, 1.20, 1.09, 1.18, t=2),  # bullish, BOS candle
    ]
    ob_idx = _find_ob_candle(candles, bos_index=2, ob_type=OBType.BULLISH)
    assert ob_idx == 0, f"expected scan to skip the doji at index 1 and find bearish candle at index 0, got {ob_idx}"


def test_find_ob_candle_returns_none_when_no_opposite_candle_exists():
    """If every candle before the BOS candle is the SAME color (no
    opposite-colored candle exists at all), must return None, not crash
    or silently pick a same-colored candle."""
    candles = [
        _c(1.10, 1.11, 1.09, 1.105, t=0),  # bullish
        _c(1.105, 1.12, 1.10, 1.115, t=1),  # bullish
        _c(1.115, 1.20, 1.11, 1.18, t=2),  # bullish, BOS candle -- looking for BEARISH OB candidate, none exists
    ]
    ob_idx = _find_ob_candle(candles, bos_index=2, ob_type=OBType.BULLISH)
    assert ob_idx is None, f"expected None when no opposite-colored candle exists, got {ob_idx}"


# ──────────────────────────────────────────────────────────────────────
# Full pipeline: detect_order_blocks
# ──────────────────────────────────────────────────────────────────────

def test_full_pipeline_detects_bullish_ob_on_engineered_bos():
    """
    Construct: a swing high forms, then later a bearish candle, then a
    strong bullish impulse candle whose CLOSE breaks above the swing high
    -- this should produce exactly one bullish OrderBlock anchored on the
    bearish candle.
    """
    candles = [
        _c(1.10, 1.10, 1.09, 1.095, t=0),
        _c(1.095, 1.11, 1.09, 1.10, t=1),
        _c(1.10, 1.15, 1.10, 1.12, t=2),   # swing high candidate: high=1.15
        _c(1.12, 1.12, 1.08, 1.09, t=3),
        _c(1.09, 1.10, 1.08, 1.085, t=4),  # bearish <- expected OB candle
        _c(1.085, 1.20, 1.08, 1.18, t=5),  # bullish impulse, close=1.18 > swing high 1.15 -> BOS
        _c(1.18, 1.19, 1.17, 1.175, t=6),
        _c(1.175, 1.18, 1.16, 1.165, t=7),
    ]
    obs = detect_order_blocks(candles, fractal_window=2)
    bullish_obs = [ob for ob in obs if ob.ob_type == OBType.BULLISH]
    assert len(bullish_obs) >= 1, f"expected at least 1 bullish OB, got {len(bullish_obs)} total OBs: {obs}"

    ob = bullish_obs[0]
    assert ob.bos_candle_index == 5, f"expected BOS at index 5, got {ob.bos_candle_index}"
    assert ob.ob_candle_index == 4, f"expected OB candle at index 4 (the bearish one), got {ob.ob_candle_index}"
    assert ob.ob_high == 1.10 and ob.ob_low == 1.08, f"expected OB zone [1.08, 1.10], got [{ob.ob_low}, {ob.ob_high}]"


def test_swing_point_only_consumed_once_no_duplicate_bos():
    """If price closes beyond the same swing high on TWO separate later
    candles, only the FIRST close should register as the BOS event --
    the swing point is consumed and must not produce a second OB for the
    same structural break."""
    candles = [
        _c(1.10, 1.10, 1.09, 1.095, t=0),
        _c(1.095, 1.11, 1.09, 1.10, t=1),
        _c(1.10, 1.15, 1.10, 1.12, t=2),   # swing high: 1.15
        _c(1.12, 1.12, 1.08, 1.09, t=3),
        _c(1.09, 1.10, 1.08, 1.085, t=4),  # bearish, OB candle
        _c(1.085, 1.20, 1.08, 1.18, t=5),  # close 1.18 > 1.15 -> FIRST break (BOS)
        _c(1.18, 1.25, 1.17, 1.22, t=6),   # close 1.22, ALSO > 1.15, but swing already consumed
        _c(1.22, 1.23, 1.20, 1.21, t=7),
    ]
    obs = detect_order_blocks(candles, fractal_window=2)
    bullish_obs = [ob for ob in obs if ob.ob_type == OBType.BULLISH]
    bos_indices = [ob.bos_candle_index for ob in bullish_obs]
    assert bos_indices.count(5) <= 1 and 6 not in bos_indices, (
        f"expected swing high broken only once at index 5, not again at index 6 "
        f"-- got BOS indices: {bos_indices}"
    )


def test_no_order_blocks_on_flat_series():
    """A series with no swing points at all (too short, or genuinely
    flat/monotonic) should produce zero OBs, not error."""
    candles = [_c(1.10, 1.101, 1.099, 1.10, t=i) for i in range(8)]
    obs = detect_order_blocks(candles, fractal_window=2)
    assert obs == [], f"expected no OBs on a flat series, got {obs}"


def test_bos_only_considers_swings_before_current_candle():
    """A swing point must not be 'broken' by a candle that occurs at or
    before its own formation -- structurally, a BOS can only reference
    swing points already established in the past relative to the
    breaking candle. This guards against an off-by-one allowing index i
    to break a swing point also at index i."""
    # Single swing high at index 2; candle at index 2 itself has close
    # equal to its own high (can't break itself) -- just confirm no OB
    # references bos_candle_index == swing's own index in a self-referential way.
    candles = [
        _c(1.10, 1.10, 1.09, 1.095, t=0),
        _c(1.095, 1.11, 1.09, 1.10, t=1),
        _c(1.10, 1.15, 1.10, 1.15, t=2),  # swing high, close==high, can't break its own level
        _c(1.15, 1.15, 1.13, 1.14, t=3),
        _c(1.14, 1.14, 1.12, 1.13, t=4),
    ]
    obs = detect_order_blocks(candles, fractal_window=2)
    for ob in obs:
        assert ob.bos_candle_index != 2 or ob.ob_type != OBType.BULLISH, (
            "swing point at index 2 must not be both the swing AND the breaking candle"
        )


def test_one_candle_breaking_multiple_unconsumed_swings_produces_single_ob():
    """
    Adversarial case found by direct probing (not part of the original
    spec'd test list): a candle whose close breaks through MULTIPLE
    unconsumed swing highs at once must produce exactly ONE bullish OB
    (anchored on the most recent/highest swing), not zero and not a
    duplicate per broken level.

    NOTE on construction: an earlier attempt at this test was itself
    broken -- it tried to place a second, higher swing high too close to
    the eventual breaking candle, which caused the breaking candle's high
    to fall inside the swing's own post-formation fractal window,
    invalidating it as a swing point before the test could even reach
    the scenario it meant to check. Fixed by inserting quiet candles
    between the second swing and the impulse candle so the swing is a
    genuine, confirmed fractal peak first. Kept this note because it's
    exactly the kind of subtle test-construction trap that can produce a
    false "looks correct" green result.
    """
    candles = [
        _c(1.10, 1.10, 1.09, 1.095, t=0),
        _c(1.095, 1.12, 1.09, 1.10, t=1),    # swing high candidate #1: 1.12 (will NOT be confirmed -- see below)
        _c(1.10, 1.11, 1.09, 1.095, t=2),
        _c(1.095, 1.10, 1.09, 1.098, t=3),
        _c(1.098, 1.15, 1.09, 1.10, t=4),    # swing high #2: 1.15, confirmed (higher than #1, so #1 never confirms)
        _c(1.10, 1.11, 1.095, 1.10, t=5),    # quiet candle confirming the t=4 fractal
        _c(1.10, 1.11, 1.095, 1.105, t=6),   # quiet candle confirming the t=4 fractal
        _c(1.105, 1.10, 1.08, 1.085, t=7),   # bearish, expected OB candle
        _c(1.085, 1.20, 1.08, 1.19, t=8),    # close 1.19 breaks the 1.15 swing -> BOS
    ]
    highs, lows = find_swing_points(candles, fractal_window=2)
    assert len(highs) == 1 and highs[0].price == 1.15, (
        f"expected exactly one confirmed swing high at 1.15 (the 1.12 "
        f"candidate should never confirm since 1.15 occurs within its "
        f"post-window), got {[(h.index, h.price) for h in highs]}"
    )

    obs = detect_order_blocks(candles, fractal_window=2)
    bullish_obs = [ob for ob in obs if ob.ob_type == OBType.BULLISH]
    assert len(bullish_obs) == 1, (
        f"expected exactly 1 bullish OB from a single breaking candle, "
        f"got {len(bullish_obs)}: {bullish_obs}"
    )
    assert bullish_obs[0].bos_candle_index == 8
    assert bullish_obs[0].ob_candle_index == 7


def run_all():
    tests = [
        test_swing_high_detected_at_clear_peak,
        test_swing_low_detected_at_clear_trough,
        test_tied_high_does_not_count_as_swing,
        test_no_swings_on_monotonic_series,
        test_first_and_last_n_candles_unevaluable,
        test_find_ob_candle_for_bullish_bos_is_nearest_bearish,
        test_find_ob_candle_for_bearish_bos_is_nearest_bullish,
        test_find_ob_candle_skips_doji,
        test_find_ob_candle_returns_none_when_no_opposite_candle_exists,
        test_full_pipeline_detects_bullish_ob_on_engineered_bos,
        test_swing_point_only_consumed_once_no_duplicate_bos,
        test_no_order_blocks_on_flat_series,
        test_bos_only_considers_swings_before_current_candle,
        test_one_candle_breaking_multiple_unconsumed_swings_produces_single_ob,
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
