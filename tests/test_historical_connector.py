# tests/test_historical_connector.py
"""
Tests data/historical_connector.py. Strategy: test cache layer and
fallback logic without live network or MT5. The three actual fetch
functions (_fetch_mt5, _fetch_broker_rest, _fetch_dukascopy) are
replaced with synthetic stubs via monkeypatching in individual tests.
This is not a full integration test -- end-to-end correctness with a
real broker requires running on the actual machine with MT5 open.
"""
import sys
import tempfile
import time
import warnings
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.historical_connector as hc
from data.historical_connector import (
    HistoricalDataUnavailable,
    HistoricalDataAvailabilityWarning,
    fetch_historical,
    _load_cache,
    _save_cache,
    _parquet_path,
)


def _synthetic_df(n=100) -> pd.DataFrame:
    now = time.time()
    return pd.DataFrame({
        "timestamp": [float(now - (n - i) * 3600) for i in range(n)],
        "open": [1.10 + i * 0.0001 for i in range(n)],
        "high": [1.10 + i * 0.0001 + 0.0005 for i in range(n)],
        "low":  [1.10 + i * 0.0001 - 0.0005 for i in range(n)],
        "close":[1.10 + i * 0.0001 for i in range(n)],
        "volume": [1000.0] * n,
    })


def test_cache_miss_when_no_file_exists():
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "EURUSD_H1.parquet"
    result = _load_cache(path, stale_after_seconds=3600)
    assert result is None, "expected None for non-existent cache file"


def test_cache_hit_when_fresh():
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "EURUSD_H1.parquet"
    df = _synthetic_df(50)
    _save_cache(df, path)
    # Cache is fresh (last candle timestamp is now-ish)
    result = _load_cache(path, stale_after_seconds=7200)
    assert result is not None
    assert len(result) == 50


def test_cache_miss_when_stale():
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "EURUSD_H1.parquet"
    df = _synthetic_df(50)
    # Make all timestamps very old (8 hours ago)
    df["timestamp"] = df["timestamp"] - 8 * 3600
    _save_cache(df, path)
    result = _load_cache(path, stale_after_seconds=3600)
    assert result is None, "expected None for stale cache (8h old > 1h threshold)"


def test_serves_from_cache_without_calling_mt5():
    tmp = tempfile.mkdtemp()
    df = _synthetic_df(200)
    path = _parquet_path(tmp, "EURUSD", "H1")
    _save_cache(df, path)

    call_log = []
    def fake_mt5(*args): call_log.append("mt5"); return None

    with patch.object(hc, "_fetch_mt5", fake_mt5):
        result = fetch_historical("EURUSD", "H1", 200, storage_dir=tmp,
                                   stale_after_candles=2)
    assert len(result) == 200
    assert "mt5" not in call_log, "MT5 must not be called when fresh cache exists"


def test_fallback_to_tier2_when_mt5_returns_none():
    tmp = tempfile.mkdtemp()
    df = _synthetic_df(100)

    def fake_mt5(*args): return None
    def fake_rest(*args): return df

    with patch.object(hc, "_fetch_mt5", fake_mt5), \
         patch.object(hc, "_fetch_broker_rest", fake_rest), \
         patch.object(hc, "_fetch_dukascopy", lambda *a: None):
        result = fetch_historical("EURUSD", "H1", 100, storage_dir=tmp)
    assert len(result) == 100


def test_fallback_to_tier3_when_tier1_and_tier2_fail():
    tmp = tempfile.mkdtemp()
    df = _synthetic_df(80)

    with patch.object(hc, "_fetch_mt5", lambda *a: None), \
         patch.object(hc, "_fetch_broker_rest", lambda *a: None), \
         patch.object(hc, "_fetch_dukascopy", lambda *a: df):
        result = fetch_historical("EURUSD", "H1", 80, storage_dir=tmp)
    assert len(result) == 80


def test_raises_unavailable_when_all_tiers_fail():
    tmp = tempfile.mkdtemp()
    with patch.object(hc, "_fetch_mt5", lambda *a: None), \
         patch.object(hc, "_fetch_broker_rest", lambda *a: None), \
         patch.object(hc, "_fetch_dukascopy", lambda *a: None):
        try:
            fetch_historical("EURUSD", "H1", 100, storage_dir=tmp)
            assert False, "expected HistoricalDataUnavailable"
        except HistoricalDataUnavailable as e:
            assert "All 3 tiers failed" in str(e)


def test_saves_to_parquet_after_successful_fetch():
    tmp = tempfile.mkdtemp()
    df = _synthetic_df(150)

    with patch.object(hc, "_fetch_mt5", lambda *a: df), \
         patch.object(hc, "_fetch_broker_rest", lambda *a: None), \
         patch.object(hc, "_fetch_dukascopy", lambda *a: None):
        fetch_historical("EURUSD", "H1", 150, storage_dir=tmp)

    path = _parquet_path(tmp, "EURUSD", "H1")
    assert path.exists(), "expected parquet file to be written after successful fetch"
    saved = pd.read_parquet(path)
    assert len(saved) == 150


def test_warns_when_fewer_bars_than_minimum():
    tmp = tempfile.mkdtemp()
    df = _synthetic_df(500)  # short of the 17520 minimum

    with patch.object(hc, "_fetch_mt5", lambda *a: df), \
         patch.object(hc, "_fetch_broker_rest", lambda *a: None), \
         patch.object(hc, "_fetch_dukascopy", lambda *a: None), \
         warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = fetch_historical("EURUSD", "H1", 500, storage_dir=tmp,
                                   min_required_bars=17520)
    shortage_warnings = [w for w in caught if issubclass(w.category, HistoricalDataAvailabilityWarning)]
    assert len(shortage_warnings) == 1, (
        f"expected exactly 1 HistoricalDataAvailabilityWarning, got {len(shortage_warnings)}"
    )
    assert "500 bars" in str(shortage_warnings[0].message)
    assert "17520" in str(shortage_warnings[0].message)
    # Data is still returned despite the warning -- it's a UserWarning, not an error
    assert len(result) == 500


def test_returns_oldest_first():
    """Output must be sorted oldest-first regardless of which tier produced it,
    since OB detection and SuperTrend both assume oldest-first candle order."""
    tmp = tempfile.mkdtemp()
    df = _synthetic_df(50)
    # Shuffle to simulate a tier returning out of order
    df = df.sample(frac=1).reset_index(drop=True)

    with patch.object(hc, "_fetch_mt5", lambda *a: df), \
         patch.object(hc, "_fetch_broker_rest", lambda *a: None), \
         patch.object(hc, "_fetch_dukascopy", lambda *a: None):
        result = fetch_historical("EURUSD", "H1", 50, storage_dir=tmp)

    timestamps = list(result["timestamp"])
    assert timestamps == sorted(timestamps), "expected output sorted oldest-first"


def run_all():
    tests = [
        test_cache_miss_when_no_file_exists,
        test_cache_hit_when_fresh,
        test_cache_miss_when_stale,
        test_serves_from_cache_without_calling_mt5,
        test_fallback_to_tier2_when_mt5_returns_none,
        test_fallback_to_tier3_when_tier1_and_tier2_fail,
        test_raises_unavailable_when_all_tiers_fail,
        test_saves_to_parquet_after_successful_fetch,
        test_warns_when_fewer_bars_than_minimum,
        test_returns_oldest_first,
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
