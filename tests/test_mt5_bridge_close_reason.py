# tests/test_mt5_bridge_close_reason.py
"""
Tests _infer_close_reason() in isolation using a fake `mt5` module object
and synthetic deal records — this is the only part of MetaTrader5Bridge
testable without a live, logged-in MT5 terminal on Windows.

This matters specifically because Phase 2's reconciliation risk note says:
"reconciliation can't distinguish trade closed by SL vs manually vs
connection drop" — this test is the concrete check that the mapping in
T1.6a actually produces the distinct CloseReason values T2.4 depends on.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.bridge import CloseReason
from broker.mt5_bridge import MetaTrader5Bridge


class FakeMT5:
    """Minimal stand-in for the constants MetaTrader5Bridge reads off
    the mt5 module when classifying a deal's close reason."""
    DEAL_REASON_SL = 0
    DEAL_REASON_TP = 1
    DEAL_REASON_CLIENT = 2
    DEAL_REASON_MOBILE = 3
    DEAL_REASON_WEB = 4
    DEAL_REASON_EXPERT = 5


def _deal(reason):
    return SimpleNamespace(reason=reason, profit=0.0, symbol="EURUSD")


def test_sl_hit():
    fake = FakeMT5()
    deals = [_deal(fake.DEAL_REASON_SL)]
    result = MetaTrader5Bridge._infer_close_reason(deals, fake)
    assert result == CloseReason.SL_HIT, f"expected SL_HIT, got {result}"


def test_tp_hit():
    fake = FakeMT5()
    deals = [_deal(fake.DEAL_REASON_TP)]
    result = MetaTrader5Bridge._infer_close_reason(deals, fake)
    assert result == CloseReason.TP_HIT, f"expected TP_HIT, got {result}"


def test_manual_close_client():
    fake = FakeMT5()
    deals = [_deal(fake.DEAL_REASON_CLIENT)]
    result = MetaTrader5Bridge._infer_close_reason(deals, fake)
    assert result == CloseReason.MANUAL_CLOSE, f"expected MANUAL_CLOSE, got {result}"


def test_manual_close_mobile():
    fake = FakeMT5()
    deals = [_deal(fake.DEAL_REASON_MOBILE)]
    result = MetaTrader5Bridge._infer_close_reason(deals, fake)
    assert result == CloseReason.MANUAL_CLOSE, f"expected MANUAL_CLOSE, got {result}"


def test_manual_close_web():
    fake = FakeMT5()
    deals = [_deal(fake.DEAL_REASON_WEB)]
    result = MetaTrader5Bridge._infer_close_reason(deals, fake)
    assert result == CloseReason.MANUAL_CLOSE, f"expected MANUAL_CLOSE, got {result}"


def test_expert_close_is_timeout_not_manual():
    """This is the case that matters most: a trade closed by our own
    T1.8 timeout-cancel logic must NOT be miscategorized as MANUAL_CLOSE,
    or T2.4's daily reconciliation would wrongly attribute system behavior
    to human intervention."""
    fake = FakeMT5()
    deals = [_deal(fake.DEAL_REASON_EXPERT)]
    result = MetaTrader5Bridge._infer_close_reason(deals, fake)
    assert result == CloseReason.TIMEOUT_CANCEL, f"expected TIMEOUT_CANCEL, got {result}"


def test_empty_deals_means_connection_lost():
    """No deal history at all for a ticket we were tracking is the
    signature of a connection gap, not a normal close — must not be
    silently treated as e.g. MANUAL_CLOSE."""
    fake = FakeMT5()
    result = MetaTrader5Bridge._infer_close_reason([], fake)
    assert result == CloseReason.CONNECTION_LOST, f"expected CONNECTION_LOST, got {result}"


def test_unrecognized_reason_code_does_not_guess():
    """An unmapped reason code (e.g. a future MT5 API addition) must fall
    back to CONNECTION_LOST (flagged for manual review) rather than being
    silently bucketed into an existing category — this is the 'fail loud,
    not silent' principle applied to reconciliation, not just config."""
    fake = FakeMT5()
    deals = [_deal(reason=999)]
    result = MetaTrader5Bridge._infer_close_reason(deals, fake)
    assert result == CloseReason.CONNECTION_LOST, f"expected CONNECTION_LOST fallback, got {result}"


def run_all():
    tests = [
        test_sl_hit,
        test_tp_hit,
        test_manual_close_client,
        test_manual_close_mobile,
        test_manual_close_web,
        test_expert_close_is_timeout_not_manual,
        test_empty_deals_means_connection_lost,
        test_unrecognized_reason_code_does_not_guess,
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
