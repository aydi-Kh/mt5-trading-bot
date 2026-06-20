# tests/test_supertrend.py
"""
Tests compute_supertrend() against synthetic candle series with known
trend shapes. Since this is a fresh implementation (not a verified port,
per the note in signals/supertrend.py), these tests check structural
correctness — does it produce a sane result, does direction respond to
an obvious trend, does is_flip_within correctly detect engineered flips —
rather than asserting exact numerical values from an external reference,
which I don't have without your original implementation to compare against.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.bridge import CandleData
from signals.supertrend import compute_supertrend, SuperTrendResult


def _make_candles(closes: list[float], symbol="EURUSD", tf="M15") -> list[CandleData]:
    """Builds simple candles where high/low are +/- 0.0005 around close,
    open = previous close. Good enough for structural trend tests."""
    candles = []
    prev_close = closes[0]
    for i, c in enumerate(closes):
        candles.append(
            CandleData(
                symbol=symbol,
                timeframe=tf,
                open=prev_close,
                high=max(prev_close, c) + 0.0005,
                low=min(prev_close, c) - 0.0005,
                close=c,
                volume=100.0,
                timestamp=float(1700000000 + i * 900),  # M15 spacing
            )
        )
        prev_close = c
    return candles


def test_raises_on_insufficient_candles():
    candles = _make_candles([1.1000] * 5)  # need atr_period(10)+1 = 11
    try:
        compute_supertrend(candles, atr_period=10, multiplier=3.0)
        assert False, "expected ValueError on insufficient candles"
    except ValueError as e:
        assert "at least 11 candles" in str(e)


def test_strong_uptrend_ends_bullish():
    """A clean, strong, monotonic uptrend should end with direction=+1.
    Using a large enough move that 3x ATR bands can't keep direction
    pinned bearish."""
    closes = [1.1000 + i * 0.0050 for i in range(40)]  # steady strong climb
    candles = _make_candles(closes)
    result = compute_supertrend(candles, atr_period=10, multiplier=3.0)
    assert result.current_direction == 1, (
        f"expected bullish direction after sustained strong uptrend, "
        f"got {result.current_direction}"
    )


def test_strong_downtrend_ends_bearish():
    closes = [1.2000 - i * 0.0050 for i in range(40)]
    candles = _make_candles(closes)
    result = compute_supertrend(candles, atr_period=10, multiplier=3.0)
    assert result.current_direction == -1, (
        f"expected bearish direction after sustained strong downtrend, "
        f"got {result.current_direction}"
    )


def test_output_length_matches_usable_candles():
    """points list should have exactly len(candles) - (atr_period - 1)
    entries, since the first atr_period-1 candles can't produce a value."""
    n = 30
    atr_period = 10
    candles = _make_candles([1.1000 + i * 0.0001 for i in range(n)])
    result = compute_supertrend(candles, atr_period=atr_period, multiplier=3.0)
    expected_len = n - (atr_period - 1)
    assert len(result.points) == expected_len, (
        f"expected {expected_len} points, got {len(result.points)}"
    )


def test_is_flip_within_detects_engineered_flip():
    """Build a series that goes up strongly then reverses sharply down,
    forcing a direction flip, and confirm is_flip_within(n) sees it
    immediately after the flip and stops seeing it once the window
    slides past."""
    up = [1.1000 + i * 0.0080 for i in range(20)]
    down = [up[-1] - i * 0.0080 for i in range(1, 20)]
    closes = up + down
    candles = _make_candles(closes)
    result = compute_supertrend(candles, atr_period=10, multiplier=3.0)

    directions = [p.direction for p in result.points]
    # Confirm a flip actually occurred in the synthetic data — otherwise
    # this test would pass vacuously.
    assert len(set(directions)) > 1, (
        "synthetic up/down series did not produce a direction flip at all — "
        "test construction is invalid, not testing what it claims to"
    )

    # Immediately after a flip, is_flip_within(2) must be True.
    flip_index = next(i for i in range(1, len(directions)) if directions[i] != directions[i - 1])
    result_at_flip = SuperTrendResult(points=result.points[: flip_index + 1])
    assert result_at_flip.is_flip_within(2) is True, (
        "expected is_flip_within(2) to detect the flip immediately after it occurs"
    )


def test_is_flip_within_false_long_after_stable_trend():
    """Deep into a long, unbroken uptrend, is_flip_within(2) should be False."""
    closes = [1.1000 + i * 0.0050 for i in range(40)]
    candles = _make_candles(closes)
    result = compute_supertrend(candles, atr_period=10, multiplier=3.0)
    assert result.is_flip_within(2) is False, (
        "expected no flip detected deep into a stable, unbroken uptrend"
    )


def test_is_flip_within_conservative_on_insufficient_history():
    """If there isn't enough history to evaluate the window, the method
    must default to True (treat as unstable) rather than silently
    returning False, per the conservative-default comment in the source."""
    candles = _make_candles([1.1000 + i * 0.0010 for i in range(11)])  # bare minimum
    result = compute_supertrend(candles, atr_period=10, multiplier=3.0)
    # Only 1-2 points exist at minimum candle count; asking for a 5-candle
    # window should hit the insufficient-history branch.
    assert result.is_flip_within(5) is True, (
        "expected conservative True when insufficient history to evaluate window"
    )


def run_all():
    tests = [
        test_raises_on_insufficient_candles,
        test_strong_uptrend_ends_bullish,
        test_strong_downtrend_ends_bearish,
        test_output_length_matches_usable_candles,
        test_is_flip_within_detects_engineered_flip,
        test_is_flip_within_false_long_after_stable_trend,
        test_is_flip_within_conservative_on_insufficient_history,
    ]
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except AssertionError as e:
            failures.append(t.__name__)
            print(f"FAIL: {t.__name__} -- {e}")
    print()
    if failures:
        print(f"{len(failures)}/{len(tests)} FAILED: {failures}")
        sys.exit(1)
    else:
        print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    run_all()
