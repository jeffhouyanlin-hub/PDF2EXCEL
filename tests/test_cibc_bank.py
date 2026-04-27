"""CIBC bank parser smoke tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.parser_registry import PARSERS, auto_detect, get_parser
from core.parsers.cibc_bank import CIBCBankParser

CIBC_DIR = Path("/Users/vox/Desktop/CIBC 2025/Cheque 0885")
SAMPLE_CIBC = CIBC_DIR / "onlineStatement_2025-01-31.pdf"


@pytest.mark.skipif(not SAMPLE_CIBC.exists(), reason="Sample PDF not available")
def test_cibc_parser_basic_shape() -> None:
    info = CIBCBankParser().parse(SAMPLE_CIBC)
    assert info.bank_name == "CIBC"
    assert info.account_number == "73-40885"
    assert info.period_from.startswith("Jan")
    assert info.period_to.endswith("2025")
    assert info.opening_balance == "5,323.55"
    assert info.closing_balance == "3,937.17"
    assert len(info.transactions) == 5
    assert list(info.transactions.columns) == [
        "Date", "Description", "Withdrawals", "Deposits", "Balance",
    ]


@pytest.mark.skipif(not SAMPLE_CIBC.exists(), reason="Sample PDF not available")
def test_cibc_same_day_multi_transactions() -> None:
    """Feb 28 statement has Feb 18 with TWO PREAUTH DEBIT transactions."""
    feb = CIBC_DIR / "onlineStatement_2025-02-28.pdf"
    if not feb.exists():
        pytest.skip("Sample PDF not available")
    info = CIBCBankParser().parse(feb)
    # Both Feb 18 debits should appear as separate rows (not merged)
    feb18_rows = info.transactions[info.transactions["Date"] == "Feb 18"]
    assert len(feb18_rows) == 2


def test_registry_contains_expected_parsers() -> None:
    keys = {p.key for p in PARSERS}
    assert {"rbc_bank", "rbc_cc", "cibc_bank"}.issubset(keys)


def test_registry_lookup() -> None:
    assert get_parser("cibc_bank") is not None
    assert get_parser("nonexistent") is None


@pytest.mark.skipif(not SAMPLE_CIBC.exists(), reason="Sample PDF not available")
def test_auto_detect_cibc() -> None:
    entry = auto_detect(SAMPLE_CIBC)
    assert entry is not None
    assert entry.key == "cibc_bank"


@pytest.mark.skipif(
    not Path("/Users/vox/Desktop/RBC 2025/Mastercard 6031/MasterCard Statement-6031 2025-01-06.pdf").exists(),
    reason="Sample PDF not available",
)
def test_auto_detect_rbc_cc() -> None:
    entry = auto_detect("/Users/vox/Desktop/RBC 2025/Mastercard 6031/MasterCard Statement-6031 2025-01-06.pdf")
    assert entry is not None
    assert entry.key == "rbc_cc"
