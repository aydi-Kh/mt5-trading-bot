# data/historical_connector.py
"""
T3.1 — MT5 Historical Connector (4h estimate)

Three-tier fallback chain, in order:
  1. MetaTrader5.copy_rates_range() -- primary, most accurate to broker
  2. Broker REST API               -- fallback if MT5 unavailable/offline
  3. Dukascopy HTTP               -- last resort, free but quality differs
                                     from broker tick data

After any successful fetch, data is saved to parquet locally so the next
run can serve from disk without hitting the network again. Staleness
threshold: if local parquet exists and its newest candle is less than
`stale_after_seconds` old (default: 2 * timeframe_seconds), return from
cache rather than fetching again.

Data Availability Risk (from the plan): some brokers cap MT5 history at
~10,000 bars (~1.1 year for H1). The connector reports how many bars it
fetched and whether it meets the `min_required_bars_h1` floor from config.
It does NOT silently substitute a shorter history -- it raises a distinct
HistoricalDataAvailabilityWarning so the caller (T3.4 walk-forward) can
decide whether to reduce the window or abort.

Dukascopy: uses a minimal OHLCV HTTP endpoint that returns JSON. This is
a best-effort tier -- the connector retries once on network error, then
falls through to HistoricalDataUnavailable. Data quality may differ from
broker tick data (per plan note). Callers using dukascopy-sourced data
should record the source in their logs for later comparison.
"""
from __future__ import annotations

import logging
import struct
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger("ak_historical")

_TF_SECONDS = {
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


class HistoricalDataUnavailable(RuntimeError):
    """Raised when all three tiers failed to produce any data."""


class HistoricalDataAvailabilityWarning(UserWarning):
    """Raised when data was fetched but is shorter than min_required_bars."""


def _parquet_path(storage_dir: str, symbol: str, timeframe: str) -> Path:
    return Path(storage_dir) / f"{symbol}_{timeframe}.parquet"


def _to_candle_df(rows: list[dict]) -> pd.DataFrame:
    """Normalise any tier's row format to a consistent DataFrame schema."""
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "time": "timestamp", "open": "open", "high": "high",
        "low": "low", "close": "close", "tick_volume": "volume",
        "real_volume": "volume",  # dukascopy uses "volume" directly
    })
    for col in ["timestamp", "open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            df[col] = 0.0
    return df[["timestamp", "open", "high", "low", "close", "volume"]].copy()


# ── Tier 1: MetaTrader5 ───────────────────────────────────────────────

def _fetch_mt5(
    symbol: str,
    timeframe: str,
    count: int,
) -> pd.DataFrame | None:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        log.debug("MetaTrader5 not installed — skipping tier 1")
        return None

    tf_map = {
        "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }
    if timeframe not in tf_map:
        return None

    if not mt5.initialize():
        log.warning("MT5 initialize() failed — skipping tier 1")
        return None

    rates = mt5.copy_rates_from_pos(symbol, tf_map[timeframe], 0, count)
    mt5.shutdown()

    if rates is None or len(rates) == 0:
        return None

    rows = [
        {"timestamp": float(r["time"]), "open": float(r["open"]),
         "high": float(r["high"]), "low": float(r["low"]),
         "close": float(r["close"]), "volume": float(r["tick_volume"])}
        for r in rates
    ]
    df = pd.DataFrame(rows)
    log.info("Tier 1 (MT5): %d bars fetched for %s %s", len(df), symbol, timeframe)
    return df


# ── Tier 2: Broker REST API ───────────────────────────────────────────

def _fetch_broker_rest(
    symbol: str,
    timeframe: str,
    count: int,
    rest_base_url: str | None,
) -> pd.DataFrame | None:
    """
    Generic broker REST fallback. Requires `rest_base_url` to be
    configured by the caller (e.g. "https://api.yourbroker.com/ohlcv").
    If not configured, this tier is skipped transparently.

    Expected response format: JSON array of objects with keys:
    timestamp (unix epoch int), open, high, low, close, volume.
    """
    if not rest_base_url:
        log.debug("Broker REST URL not configured — skipping tier 2")
        return None

    tf_seconds = _TF_SECONDS.get(timeframe, 3600)
    end_ts = int(time.time())
    start_ts = end_ts - count * tf_seconds

    url = (
        f"{rest_base_url.rstrip('/')}?"
        f"symbol={symbol}&timeframe={timeframe}"
        f"&from={start_ts}&to={end_ts}&count={count}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            import json
            data = json.loads(resp.read())
        df = _to_candle_df(data)
        log.info("Tier 2 (broker REST): %d bars for %s %s", len(df), symbol, timeframe)
        return df
    except Exception as e:
        log.warning("Tier 2 (broker REST) failed: %s", e)
        return None


# ── Tier 3: Dukascopy ────────────────────────────────────────────────

_DUKASCOPY_TF_MAP = {
    "M5": "m5", "M15": "m15", "H1": "h1", "H4": "h4", "D1": "d1",
}

_DUKASCOPY_INSTRUMENTS = {
    "EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "XAUUSD": "XAU/USD",
}


def _fetch_dukascopy(
    symbol: str,
    timeframe: str,
    count: int,
) -> pd.DataFrame | None:
    """
    Dukascopy free data tier via their public HTTP endpoint.
    Returns None (not raises) on any network/parse failure.
    Note from plan: "data quality may differ from broker tick data."
    """
    dc_symbol = _DUKASCOPY_INSTRUMENTS.get(symbol)
    dc_tf = _DUKASCOPY_TF_MAP.get(timeframe)
    if not dc_symbol or not dc_tf:
        log.debug("No Dukascopy mapping for %s %s — skipping tier 3", symbol, timeframe)
        return None

    tf_seconds = _TF_SECONDS.get(timeframe, 3600)
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(seconds=count * tf_seconds)

    # Dukascopy free endpoint: returns CSV-like JSON, structure varies.
    # Using the tick data endpoint that supports aggregation.
    url = (
        f"https://freeserv.dukascopy.com/2.0/?"
        f"path=chart/json&instrument={dc_symbol.replace('/', '%2F')}"
        f"&offer_side=B&interval={dc_tf.upper()}"
        f"&splits=true&stocks=false"
        f"&from={int(start_dt.timestamp()) * 1000}"
        f"&to={int(end_dt.timestamp()) * 1000}"
    )

    for attempt in range(2):  # one retry per plan spec
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AK-Agent/3"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                import json
                data = json.loads(resp.read())
            if not data:
                return None
            rows = [
                {
                    "timestamp": float(row[0]) / 1000.0,  # ms -> seconds
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]) if len(row) > 5 else 0.0,
                }
                for row in data
            ]
            df = pd.DataFrame(rows)
            log.info("Tier 3 (Dukascopy): %d bars for %s %s", len(df), symbol, timeframe)
            return df
        except Exception as e:
            log.warning("Tier 3 (Dukascopy) attempt %d failed: %s", attempt + 1, e)
            if attempt == 0:
                time.sleep(2)

    return None


# ── Cache layer ───────────────────────────────────────────────────────

def _load_cache(path: Path, stale_after_seconds: float) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
        if len(df) == 0:
            return None
        newest_ts = float(df["timestamp"].max())
        age = time.time() - newest_ts
        if age > stale_after_seconds:
            log.debug("Cache stale (%.0fs old, threshold %.0fs) — will refresh",
                      age, stale_after_seconds)
            return None
        log.info("Serving %d bars from cache: %s", len(df), path.name)
        return df
    except Exception as e:
        log.warning("Cache read failed (%s) — will fetch fresh", e)
        return None


def _save_cache(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    log.debug("Saved %d bars to cache: %s", len(df), path.name)


# ── Public entry point ────────────────────────────────────────────────

def fetch_historical(
    symbol: str,
    timeframe: str,
    count: int,
    storage_dir: str = "./data/historical/",
    min_required_bars: int | None = None,
    rest_base_url: str | None = None,
    stale_after_candles: int = 2,
) -> pd.DataFrame:
    """
    Fetches `count` bars of OHLCV data for symbol/timeframe, using the
    3-tier fallback chain. Returns a DataFrame with columns:
    [timestamp, open, high, low, close, volume], oldest first.

    Raises:
      HistoricalDataUnavailable  -- all tiers failed
      HistoricalDataAvailabilityWarning -- data fetched but < min_required_bars
        (this is a UserWarning, not an exception; callers can catch it if
         they want to handle the short-history case differently from no data)
    """
    tf_seconds = _TF_SECONDS.get(timeframe, 3600)
    stale_threshold = stale_after_candles * tf_seconds
    cache_path = _parquet_path(storage_dir, symbol, timeframe)

    # Try cache first
    cached = _load_cache(cache_path, stale_threshold)
    if cached is not None:
        _maybe_warn_short(cached, min_required_bars, symbol, timeframe)
        return cached.sort_values("timestamp").reset_index(drop=True)

    # Tier 1: MT5
    df = _fetch_mt5(symbol, timeframe, count)

    # Tier 2: Broker REST
    if df is None or len(df) == 0:
        df = _fetch_broker_rest(symbol, timeframe, count, rest_base_url)

    # Tier 3: Dukascopy
    if df is None or len(df) == 0:
        df = _fetch_dukascopy(symbol, timeframe, count)

    if df is None or len(df) == 0:
        raise HistoricalDataUnavailable(
            f"All 3 tiers failed to fetch {symbol} {timeframe} data. "
            f"Checked: MT5 (terminal running?), broker REST (URL configured?), "
            f"Dukascopy (network available? symbol {symbol} in mapping?)."
        )

    df = df.sort_values("timestamp").reset_index(drop=True)
    _save_cache(df, cache_path)
    _maybe_warn_short(df, min_required_bars, symbol, timeframe)
    return df


def _maybe_warn_short(
    df: pd.DataFrame,
    min_required_bars: int | None,
    symbol: str,
    timeframe: str,
) -> None:
    if min_required_bars is not None and len(df) < min_required_bars:
        import warnings
        warnings.warn(
            f"{symbol} {timeframe}: fetched {len(df)} bars, "
            f"minimum required is {min_required_bars}. "
            f"Walk-forward window (T3.4) may need to be reduced. "
            f"Per plan: verify len(copy_rates_from_pos('{symbol}', "
            f"TIMEFRAME_{timeframe}, 0, 99999)) on your broker.",
            HistoricalDataAvailabilityWarning,
            stacklevel=3,
        )
