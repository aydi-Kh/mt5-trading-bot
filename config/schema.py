# config/schema.py
"""
T1.1 — Pydantic schema for trading_rules.yaml

Design intent (from project spec): all OB/SMT/risk/reject rules live in YAML,
not hardcoded in ak_agent_v3.py. This module is the validation gate — if the
YAML is malformed or out of range, the system must crash at startup with a
clear error, never fall back to a silent default that could trade with wrong
risk parameters.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ──────────────────────────────────────────────────────────────────────────
# Instruments
# ──────────────────────────────────────────────────────────────────────────

class InstrumentRole(str, Enum):
    PRIMARY = "primary"
    SMT_PAIR = "smt_pair"
    COMMODITY_TEST = "commodity_test"


class InstrumentConfig(BaseModel):
    symbol: str
    role: InstrumentRole
    pip_value: float = Field(gt=0)
    max_spread_pips: float = Field(gt=0)
    smt_correlation_with: str | None = None

    @model_validator(mode="after")
    def smt_pair_must_declare_correlation(self) -> "InstrumentConfig":
        if self.role == InstrumentRole.SMT_PAIR and not self.smt_correlation_with:
            raise ValueError(
                f"Instrument '{self.symbol}' has role=smt_pair but no "
                f"smt_correlation_with target — SMT analyzer (T1.4) cannot "
                f"function without a correlation pair."
            )
        return self


# ──────────────────────────────────────────────────────────────────────────
# Timeframes
# ──────────────────────────────────────────────────────────────────────────

class TimeframeConfig(BaseModel):
    structure: Literal["H1"] = "H1"
    confirmation: Literal["M15"] = "M15"
    execution: Literal["M5"] = "M5"


# ──────────────────────────────────────────────────────────────────────────
# SuperTrend
# ──────────────────────────────────────────────────────────────────────────

class SuperTrendConfig(BaseModel):
    timeframe: Literal["M15"] = "M15"
    atr_period: int = Field(default=10, gt=0)
    multiplier: float = Field(default=3.0, gt=0)
    source: Literal["close", "open", "high", "low"] = "close"


# ──────────────────────────────────────────────────────────────────────────
# Order Block
# ──────────────────────────────────────────────────────────────────────────

class OBFreshnessConfig(BaseModel):
    max_age_candles_h1: int = Field(gt=0)
    mitigation_threshold_pct: float = Field(gt=0, le=1.0)


class OBVolumeConfig(BaseModel):
    min_relative_volume: float = Field(gt=0)


class SessionTimes(BaseModel):
    london: tuple[str, str]
    newyork: tuple[str, str]

    @model_validator(mode="after")
    def validate_hhmm_format(self) -> "SessionTimes":
        import re
        pattern = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
        for name, (start, end) in (("london", self.london), ("newyork", self.newyork)):
            if not pattern.match(start) or not pattern.match(end):
                raise ValueError(
                    f"session_times_utc.{name} must be HH:MM 24h format, got {start!r}/{end!r}"
                )
        return self


class OBSessionFilterConfig(BaseModel):
    valid_sessions: list[Literal["london", "newyork"]]
    session_times_utc: SessionTimes

    @model_validator(mode="after")
    def at_least_one_session(self) -> "OBSessionFilterConfig":
        if not self.valid_sessions:
            raise ValueError(
                "order_block.session_filter.valid_sessions is empty — "
                "every OB would be tagged session_invalid and excluded "
                "from T1.7, making the signal pipeline produce zero signals."
            )
        return self


class OrderBlockConfig(BaseModel):
    freshness: OBFreshnessConfig
    volume_confirmation: OBVolumeConfig
    session_filter: OBSessionFilterConfig


# ──────────────────────────────────────────────────────────────────────────
# SMT
# ──────────────────────────────────────────────────────────────────────────

class SMTSizingRules(BaseModel):
    aligned_size_pct: float = Field(gt=0, le=100)
    divergent_size_pct: float = Field(gt=0, le=100)
    counter_trend_action: Literal["no_trade"] = "no_trade"

    @model_validator(mode="after")
    def divergent_must_not_exceed_aligned(self) -> "SMTSizingRules":
        if self.divergent_size_pct > self.aligned_size_pct:
            raise ValueError(
                "smt.rules.divergent_size_pct cannot exceed aligned_size_pct — "
                "divergent SMT is a lower-confidence signal per spec and must "
                "size at or below the fully-aligned case."
            )
        return self


class SMTConfig(BaseModel):
    lookback_candles_m15: int = Field(gt=0)
    divergence_threshold_pct: float = Field(gt=0, le=1.0)
    rules: SMTSizingRules


# ──────────────────────────────────────────────────────────────────────────
# Risk management
# ──────────────────────────────────────────────────────────────────────────

class RiskProfile(BaseModel):
    timeframes: list[str]
    min_rr: float = Field(gt=0)
    max_sl_pct_account: float = Field(gt=0, le=100)


class RiskManagementConfig(BaseModel):
    scalping: RiskProfile
    swing: RiskProfile

    @model_validator(mode="after")
    def swing_rr_must_exceed_scalp_rr(self) -> "RiskManagementConfig":
        # Spec: scalp min R/R 1:2, swing min R/R 1:4 — swing should never be
        # allowed to have a *looser* R/R requirement than scalping, that
        # would invert the intended risk posture.
        if self.swing.min_rr < self.scalping.min_rr:
            raise ValueError(
                f"risk_management.swing.min_rr ({self.swing.min_rr}) is lower "
                f"than scalping.min_rr ({self.scalping.min_rr}) — swing trades "
                f"are meant to require a stricter R/R, not a looser one."
            )
        return self


# ──────────────────────────────────────────────────────────────────────────
# Auto-reject rules
# ──────────────────────────────────────────────────────────────────────────

# These five IDs are referenced by name in T1.9 (Auto-Reject Gate) and in
# T1.2b (mitigation check explicitly reuses condition "mitigated_ob" rather
# than duplicating the calculation — see plan §8 point 2). Keeping them as
# a closed Literal set prevents a YAML typo from silently creating a rule
# that T1.9 never checks.
AutoRejectId = Literal[
    "mitigated_ob",
    "unstable_supertrend",
    "insufficient_rr",
    "spread_prohibitive",
    "news_blackout",
]

_REQUIRED_REJECT_IDS: set[str] = {
    "mitigated_ob",
    "unstable_supertrend",
    "insufficient_rr",
    "spread_prohibitive",
    "news_blackout",
}


class AutoRejectRule(BaseModel):
    id: AutoRejectId
    condition: str = Field(min_length=1)


def _validate_auto_reject_rules(rules: list[AutoRejectRule]) -> list[AutoRejectRule]:
    """
    Shared validator applied to the root-level `auto_reject: list[...]` field.
    Kept as a free function (rather than a wrapper model) because the YAML
    shape is a plain list — wrapping it in a model with an aliased field
    was fighting pydantic's validation order instead of matching the data.
    """
    present = {r.id for r in rules}
    missing = _REQUIRED_REJECT_IDS - present
    if missing:
        raise ValueError(
            f"auto_reject is missing required rule id(s): {sorted(missing)} — "
            f"the spec defines exactly 5 non-negotiable reject conditions, "
            f"all must be present in config even if a developer wants to "
            f"loosen the *condition* expression later."
        )
    return rules


# ──────────────────────────────────────────────────────────────────────────
# Execution
# ──────────────────────────────────────────────────────────────────────────

class ExecutionConfig(BaseModel):
    m5_fill_timeout_candles: int = Field(gt=0)
    order_type: Literal["limit"] = "limit"


# ──────────────────────────────────────────────────────────────────────────
# Data / historical fetch
# ──────────────────────────────────────────────────────────────────────────

class HistoricalFetchConfig(BaseModel):
    primary_source: Literal["mt5_copy_rates"] = "mt5_copy_rates"
    fallback_chain: list[Literal["broker_rest", "dukascopy"]]
    min_required_bars_h1: int = Field(gt=0)
    storage_format: Literal["parquet"] = "parquet"
    storage_path: str


class DataConfig(BaseModel):
    historical_fetch: HistoricalFetchConfig


# ──────────────────────────────────────────────────────────────────────────
# Root config
# ──────────────────────────────────────────────────────────────────────────

class TradingConfig(BaseModel):
    """
    Root schema. Load via TradingConfig.from_yaml(path) — never construct
    a TradingConfig from partial/default data in application code. If the
    YAML is wrong, this must raise, not degrade.
    """
    instruments: list[InstrumentConfig] = Field(min_length=1, max_length=3)
    timeframes: TimeframeConfig
    supertrend: SuperTrendConfig
    order_block: OrderBlockConfig
    smt: SMTConfig
    risk_management: RiskManagementConfig
    auto_reject: list[AutoRejectRule] = Field(min_length=5)
    execution: ExecutionConfig
    data: DataConfig

    @field_validator("auto_reject")
    @classmethod
    def _check_auto_reject_completeness(cls, rules: list[AutoRejectRule]) -> list[AutoRejectRule]:
        return _validate_auto_reject_rules(rules)

    @model_validator(mode="after")
    def max_three_instruments_per_spec(self) -> "TradingConfig":
        # Redundant with Field(max_length=3) but spec explicitly calls out
        # "MAX 3 instruments for V3" as a hard constraint — fail with a
        # message that references the spec, not a generic pydantic error.
        if len(self.instruments) > 3:
            raise ValueError(
                f"Spec hard constraint: MAX 3 instruments for V3, got "
                f"{len(self.instruments)}."
            )
        return self

    @model_validator(mode="after")
    def smt_pairs_must_resolve_to_declared_instrument(self) -> "TradingConfig":
        symbols = {i.symbol for i in self.instruments}
        for instr in self.instruments:
            if instr.smt_correlation_with and instr.smt_correlation_with not in symbols:
                raise ValueError(
                    f"Instrument '{instr.symbol}' declares "
                    f"smt_correlation_with='{instr.smt_correlation_with}', but "
                    f"that symbol is not present in the instruments list."
                )
        return self

    @classmethod
    def from_yaml(cls, path: str) -> "TradingConfig":
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if raw is None:
            raise ValueError(f"Config file '{path}' is empty or not valid YAML.")
        # pydantic v2: raises ValidationError on bad data — let it propagate.
        return cls.model_validate(raw)
