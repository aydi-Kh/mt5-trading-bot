# tests/test_sizer.py
"""
Tests risk/sizer.py. Priority order matches plan risk: the R/R hard gate
("If setup doesn't meet R/R minimum -> NO TRADE") and the OB-vs-ST trend
conflict gate are the two non-negotiable hard rejections — everything
else (SMT size scaling, exact volume math) is secondary.

NOTE on enum split: an earlier version of this test suite (and of
risk/sizer.py itself) used a single SMTAlignment enum with a
COUNTER_TREND member to represent "OB vs ST conflict". That was a real
naming/scope bug -- OB-vs-ST trend agreement and SMT correlation-pair
divergence are different checks per the plan (the former gates whether a
trade happens at all, the latter only scales size once a trade is
already permitted) -- fixed by splitting into TrendAgreement (AGREE/
CONFLICT) and SMTAlignment (ALIGNED/DIVERGENT) as two separate required
parameters. This file was updated accordingly, not left calling the old
single-enum signature.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.schema import TradingConfig
from risk.sizer import (
    compute_position_size,
    timeframe_to_profile,
    TrendAgreement,
    SMTAlignment,
    SizingRejectionReason,
    TradeProfile,
)

CFG = TradingConfig.from_yaml(str(Path(__file__).resolve().parent.parent / "config" / "trading_rules.yaml"))
EURUSD = next(i for i in CFG.instruments if i.symbol == "EURUSD")


def test_timeframe_to_profile_mapping():
    assert timeframe_to_profile("M15") == TradeProfile.SCALPING
    assert timeframe_to_profile("M5") == TradeProfile.SCALPING
    assert timeframe_to_profile("H4") == TradeProfile.SWING
    assert timeframe_to_profile("D1") == TradeProfile.SWING


def test_unknown_timeframe_raises():
    try:
        timeframe_to_profile("W1")
        assert False, "expected ValueError for unmapped timeframe"
    except ValueError as e:
        assert "W1" in str(e)


def test_insufficient_rr_is_rejected_not_just_flagged():
    """The literal plan requirement: R/R below minimum -> NO TRADE.
    Scalping min_rr=2.0. Entry 1.1000, SL 1.0990 (10 pip risk),
    TP 1.1015 (15 pip reward) = R/R 1.5, below the 2.0 minimum."""
    result = compute_position_size(
        account_equity=10_000,
        entry_price=1.1000,
        sl_price=1.0990,
        tp_price=1.1015,
        timeframe="M15",
        instrument=EURUSD,
        risk_config=CFG.risk_management,
        trend_agreement=TrendAgreement.AGREE,
        smt_alignment=SMTAlignment.ALIGNED,
        is_long=True,
    )
    assert result.approved is False, "expected rejection for R/R below minimum"
    assert result.volume is None, "rejected trade must not carry a computed volume"
    assert result.rejection_reason == SizingRejectionReason.INSUFFICIENT_RR
    assert abs(result.realized_rr - 1.5) < 1e-9, f"expected realized_rr=1.5, got {result.realized_rr}"


def test_exactly_at_minimum_rr_is_approved():
    """R/R exactly equal to min_rr (2.0) should pass — gate is '< min_rr'
    rejects, not '<= min_rr'. Entry 1.1000, SL 1.0990 (10 pip),
    TP 1.1020 (20 pip) = R/R exactly 2.0."""
    result = compute_position_size(
        account_equity=10_000,
        entry_price=1.1000,
        sl_price=1.0990,
        tp_price=1.1020,
        timeframe="M15",
        instrument=EURUSD,
        risk_config=CFG.risk_management,
        trend_agreement=TrendAgreement.AGREE,
        smt_alignment=SMTAlignment.ALIGNED,
        is_long=True,
    )
    assert result.approved is True, f"expected approval at exactly min_rr, got rejection: {result.rejection_reason}"
    assert abs(result.realized_rr - 2.0) < 1e-9


def test_above_minimum_rr_is_approved_with_volume():
    result = compute_position_size(
        account_equity=10_000,
        entry_price=1.1000,
        sl_price=1.0990,
        tp_price=1.1030,  # 30 pip reward / 10 pip risk = R/R 3.0
        timeframe="M15",
        instrument=EURUSD,
        risk_config=CFG.risk_management,
        trend_agreement=TrendAgreement.AGREE,
        smt_alignment=SMTAlignment.ALIGNED,
        is_long=True,
    )
    assert result.approved is True
    assert result.volume is not None and result.volume > 0
    assert result.size_pct_of_max == 100.0


def test_ob_st_trend_conflict_always_rejected_even_with_great_rr():
    """OB-vs-ST trend conflict must reject regardless of R/R quality --
    this is the plan's literal 'OB vs ST conflict -> NO TRADE' rule,
    independent of R/R math entirely. (This replaces the old, incorrectly
    SMT-attributed version of this test.)"""
    result = compute_position_size(
        account_equity=10_000,
        entry_price=1.1000,
        sl_price=1.0990,
        tp_price=1.1100,  # R/R = 10.0, excellent, must still be rejected
        timeframe="M15",
        instrument=EURUSD,
        risk_config=CFG.risk_management,
        trend_agreement=TrendAgreement.CONFLICT,
        smt_alignment=SMTAlignment.ALIGNED,  # irrelevant -- conflict gate fires first
        is_long=True,
    )
    assert result.approved is False
    assert result.rejection_reason == SizingRejectionReason.OB_ST_TREND_CONFLICT
    assert result.realized_rr is None, "trend-conflict rejection happens before R/R is even computed"


def test_trend_conflict_checked_independently_of_smt_value():
    """Confirms trend_agreement=CONFLICT rejects regardless of what
    smt_alignment says, in either direction -- the two parameters must
    not be coupled to each other."""
    for smt in (SMTAlignment.ALIGNED, SMTAlignment.DIVERGENT):
        result = compute_position_size(
            account_equity=10_000, entry_price=1.1000, sl_price=1.0990, tp_price=1.1100,
            timeframe="M15", instrument=EURUSD, risk_config=CFG.risk_management,
            trend_agreement=TrendAgreement.CONFLICT, smt_alignment=smt, is_long=True,
        )
        assert result.approved is False, f"expected rejection regardless of smt_alignment={smt}"
        assert result.rejection_reason == SizingRejectionReason.OB_ST_TREND_CONFLICT


def test_divergent_smt_halves_size_pct():
    result_aligned = compute_position_size(
        account_equity=10_000, entry_price=1.1000, sl_price=1.0990, tp_price=1.1030,
        timeframe="M15", instrument=EURUSD, risk_config=CFG.risk_management,
        trend_agreement=TrendAgreement.AGREE, smt_alignment=SMTAlignment.ALIGNED, is_long=True,
    )
    result_divergent = compute_position_size(
        account_equity=10_000, entry_price=1.1000, sl_price=1.0990, tp_price=1.1030,
        timeframe="M15", instrument=EURUSD, risk_config=CFG.risk_management,
        trend_agreement=TrendAgreement.AGREE, smt_alignment=SMTAlignment.DIVERGENT, is_long=True,
    )
    assert result_aligned.approved and result_divergent.approved
    assert result_divergent.size_pct_of_max == 50.0
    assert result_aligned.size_pct_of_max == 100.0
    # Same R/R, same SL distance -> divergent volume should be exactly half aligned volume
    assert abs(result_divergent.volume - result_aligned.volume / 2) < 1e-6, (
        f"expected divergent volume ({result_divergent.volume}) to be half of "
        f"aligned volume ({result_aligned.volume})"
    )


def test_zero_sl_distance_rejected():
    """Entry == SL (zero distance) must reject cleanly, not divide by zero."""
    result = compute_position_size(
        account_equity=10_000,
        entry_price=1.1000,
        sl_price=1.1000,
        tp_price=1.1030,
        timeframe="M15",
        instrument=EURUSD,
        risk_config=CFG.risk_management,
        trend_agreement=TrendAgreement.AGREE,
        smt_alignment=SMTAlignment.ALIGNED,
        is_long=True,
    )
    assert result.approved is False
    assert result.rejection_reason == SizingRejectionReason.ZERO_OR_NEGATIVE_SL_DISTANCE


def test_swing_profile_uses_swing_min_rr():
    """Same 1.5 R/R that fails scalping's 2.0 minimum should ALSO fail
    swing's stricter 4.0 minimum — confirms profile routing actually
    picks up the right config branch, not just falling through to scalp."""
    result = compute_position_size(
        account_equity=10_000,
        entry_price=1.1000,
        sl_price=1.0900,   # 100 pip risk
        tp_price=1.1300,   # 300 pip reward = R/R 3.0, fails swing's 4.0 min
        timeframe="H4",
        instrument=EURUSD,
        risk_config=CFG.risk_management,
        trend_agreement=TrendAgreement.AGREE,
        smt_alignment=SMTAlignment.ALIGNED,
        is_long=True,
    )
    assert result.approved is False
    assert result.profile == TradeProfile.SWING
    assert result.rejection_reason == SizingRejectionReason.INSUFFICIENT_RR
    assert abs(result.realized_rr - 3.0) < 1e-9


def run_all():
    tests = [
        test_timeframe_to_profile_mapping,
        test_unknown_timeframe_raises,
        test_insufficient_rr_is_rejected_not_just_flagged,
        test_exactly_at_minimum_rr_is_approved,
        test_above_minimum_rr_is_approved_with_volume,
        test_ob_st_trend_conflict_always_rejected_even_with_great_rr,
        test_trend_conflict_checked_independently_of_smt_value,
        test_divergent_smt_halves_size_pct,
        test_zero_sl_distance_rejected,
        test_swing_profile_uses_swing_min_rr,
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
