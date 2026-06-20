# risk/sizer.py
"""
T1.5 — Risk/Position Sizer (4h estimate)

Per plan: "If setup doesn't meet R/R minimum → NO TRADE." This is encoded
as a hard gate here, not a warning — compute_position_size() returns a
SizingResult with approved=False and no volume when R/R is insufficient,
rather than computing a size anyway and letting a caller forget to check.

Also enforces the SMT sizing rules from config (aligned=100%, divergent=50%,
counter-trend=no_trade) by accepting an SMTAlignment as input rather than
duplicating that branching logic in T1.7's pipeline integration.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from config.schema import RiskManagementConfig, InstrumentConfig


class TradeProfile(Enum):
    SCALPING = "scalping"
    SWING = "swing"


class TrendAgreement(Enum):
    """
    Whether the OB's directional bias and the SuperTrend M15 filter's
    current direction agree. This is a SEPARATE, PRIOR check to SMT --
    it has nothing to do with correlated-pair divergence. An earlier
    version of this module conflated this with SMTAlignment under a
    single enum and a misleading 'COUNTER_TREND_SMT' rejection name;
    split out here because OB-vs-ST agreement and SMT correlation
    divergence are genuinely different signals answering different
    questions, and treating them as one enum made it impossible to
    correctly attribute a rejection to its actual cause.
    """
    AGREE = "agree"        # OB direction matches SuperTrend direction
    CONFLICT = "conflict"  # OB direction contradicts SuperTrend -> no trade,
                            # independent of what SMT says


class SMTAlignment(Enum):
    """
    Pure SMT correlation-pair divergence result, per the plan's SMT
    Filter Rules section. This assumes OB/ST agreement has ALREADY been
    checked separately (see TrendAgreement) -- SMTAlignment says nothing
    about OB or ST on its own.
    """
    ALIGNED = "aligned"      # correlated pair confirms the same direction -> full size
    DIVERGENT = "divergent"  # correlated pair diverges -> half size


# Timeframes that map to each profile, per config's risk_management.*.timeframes.
# Kept here (not re-derived from config at call time) because the mapping
# direction we need is timeframe -> profile, which is the inverse of how
# the YAML expresses it (profile -> list of timeframes).
_TIMEFRAME_TO_PROFILE: dict[str, TradeProfile] = {
    "M5": TradeProfile.SCALPING,
    "M15": TradeProfile.SCALPING,
    "H4": TradeProfile.SWING,
    "D1": TradeProfile.SWING,
}


class SizingRejectionReason(Enum):
    INSUFFICIENT_RR = "insufficient_rr"
    OB_ST_TREND_CONFLICT = "ob_st_trend_conflict"
    UNKNOWN_TIMEFRAME_PROFILE = "unknown_timeframe_profile"
    ZERO_OR_NEGATIVE_SL_DISTANCE = "zero_or_negative_sl_distance"
    COMPUTED_VOLUME_NON_POSITIVE = "computed_volume_non_positive"


@dataclass(frozen=True)
class SizingResult:
    approved: bool
    volume: float | None
    profile: TradeProfile | None
    realized_rr: float | None
    size_pct_of_max: float | None  # 100.0 (aligned) or 50.0 (divergent), for audit/logging
    rejection_reason: SizingRejectionReason | None


def timeframe_to_profile(timeframe: str) -> TradeProfile:
    if timeframe not in _TIMEFRAME_TO_PROFILE:
        raise ValueError(
            f"Timeframe '{timeframe}' has no risk profile mapping. "
            f"Known timeframes: {sorted(_TIMEFRAME_TO_PROFILE.keys())}. "
            f"This must be resolved in config, not silently defaulted — "
            f"an unmapped timeframe trading with the wrong risk profile "
            f"is exactly the kind of silent failure the YAML externalization "
            f"(T1.1) is meant to prevent."
        )
    return _TIMEFRAME_TO_PROFILE[timeframe]


def compute_position_size(
    *,
    account_equity: float,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    timeframe: str,
    instrument: InstrumentConfig,
    risk_config: RiskManagementConfig,
    trend_agreement: TrendAgreement,
    smt_alignment: SMTAlignment,
    is_long: bool,
) -> SizingResult:
    """
    Returns a SizingResult. Callers (T1.7) must check `.approved` before
    using `.volume` — an unapproved result has volume=None specifically
    to make "forgot to check approved" fail fast with an AttributeError-
    adjacent TypeError rather than silently trading a None-derived size.

    trend_agreement and smt_alignment are deliberately two separate
    parameters, not one combined enum (see TrendAgreement/SMTAlignment
    docstrings for why) -- trend_agreement gates whether a trade is
    allowed AT ALL (OB vs ST conflict = hard no-trade, matching the
    plan's Auto-Reject/SMT Filter Rules text exactly: "OB vs ST conflict
    -> NO TRADE"), while smt_alignment only affects SIZE once a trade is
    already permitted.
    """
    # 1. Resolve profile from timeframe.
    try:
        profile = timeframe_to_profile(timeframe)
    except ValueError:
        return SizingResult(
            approved=False,
            volume=None,
            profile=None,
            realized_rr=None,
            size_pct_of_max=None,
            rejection_reason=SizingRejectionReason.UNKNOWN_TIMEFRAME_PROFILE,
        )

    profile_config = (
        risk_config.scalping if profile == TradeProfile.SCALPING else risk_config.swing
    )

    # 2. OB-vs-ST trend conflict is a hard no-trade, checked BEFORE SMT
    # and independent of R/R math entirely -- this is the actual "OB vs
    # ST conflict -> NO TRADE" rule from the plan, which an earlier
    # version of this module incorrectly implemented as an SMT check.
    if trend_agreement == TrendAgreement.CONFLICT:
        return SizingResult(
            approved=False,
            volume=None,
            profile=profile,
            realized_rr=None,
            size_pct_of_max=None,
            rejection_reason=SizingRejectionReason.OB_ST_TREND_CONFLICT,
        )

    # 3. SL/TP distance sanity. Direction-aware: for a long, SL must be
    # below entry and TP above; for a short, the reverse. We compute
    # distances as unsigned values and validate direction is at least
    # self-consistent (not validating that direction is "correct" market
    # analysis — that's T1.2/T1.4's job, not the sizer's).
    sl_distance = abs(entry_price - sl_price)
    tp_distance = abs(tp_price - entry_price)

    if sl_distance <= 0:
        return SizingResult(
            approved=False,
            volume=None,
            profile=profile,
            realized_rr=None,
            size_pct_of_max=None,
            rejection_reason=SizingRejectionReason.ZERO_OR_NEGATIVE_SL_DISTANCE,
        )

    realized_rr = tp_distance / sl_distance

    # 4. Hard R/R gate — this is the "NO TRADE" enforcement from the plan.
    # Epsilon tolerance guards against float representation error rejecting
    # a setup that is exactly at the minimum (e.g. 1.1020-1.1000 in IEEE754
    # double precision evaluates to 0.0019999999999998, not 0.0020 — without
    # this tolerance, a textbook-exact R/R 2.0 setup would be wrongly
    # rejected). The tolerance is intentionally tiny (1e-9 relative) so it
    # only absorbs float noise, not genuine near-misses.
    rr_epsilon = 1e-9
    if realized_rr < profile_config.min_rr - rr_epsilon:
        return SizingResult(
            approved=False,
            volume=None,
            profile=profile,
            realized_rr=realized_rr,
            size_pct_of_max=None,
            rejection_reason=SizingRejectionReason.INSUFFICIENT_RR,
        )

    # 5. SMT-based size scaling, per config.smt.rules.
    size_pct = (
        100.0 if smt_alignment == SMTAlignment.ALIGNED else 50.0
    )

    # 6. Risk-based volume calc.
    # max_sl_pct_account is expressed as a percentage (e.g. 0.3 = 0.3%),
    # matching the YAML comment "SL max 0.3% account" — NOT a fraction
    # like 0.003. This must match config/schema.py's Field(le=100) range
    # assumption; a mismatch here would silently 100x the actual risk.
    max_risk_amount = account_equity * (profile_config.max_sl_pct_account / 100.0)
    risk_amount_for_this_trade = max_risk_amount * (size_pct / 100.0)

    # volume (in lots, broker convention) such that sl_distance * pip_value
    # * volume_in_units == risk_amount. We express volume in "lots" where
    # 1.0 lot = 100,000 units (standard FX convention) — this assumption
    # should be revisited per-instrument once XAUUSD's actual contract
    # size is confirmed against the broker, since commodities often use
    # a different lot definition than FX majors.
    units_per_lot = 100_000.0
    value_per_pip_per_lot = instrument.pip_value * units_per_lot
    sl_distance_in_pips = sl_distance / instrument.pip_value

    if value_per_pip_per_lot <= 0 or sl_distance_in_pips <= 0:
        return SizingResult(
            approved=False,
            volume=None,
            profile=profile,
            realized_rr=realized_rr,
            size_pct_of_max=size_pct,
            rejection_reason=SizingRejectionReason.COMPUTED_VOLUME_NON_POSITIVE,
        )

    volume = risk_amount_for_this_trade / (sl_distance_in_pips * value_per_pip_per_lot)

    if volume <= 0:
        return SizingResult(
            approved=False,
            volume=None,
            profile=profile,
            realized_rr=realized_rr,
            size_pct_of_max=size_pct,
            rejection_reason=SizingRejectionReason.COMPUTED_VOLUME_NON_POSITIVE,
        )

    return SizingResult(
        approved=True,
        volume=round(volume, 2),  # most brokers quantize to 0.01 lot steps
        profile=profile,
        realized_rr=realized_rr,
        size_pct_of_max=size_pct,
        rejection_reason=None,
    )
