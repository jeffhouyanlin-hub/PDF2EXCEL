"""Unit tests for core.fee_sort.rule_engine."""

from __future__ import annotations

import pytest

from core.fee_sort.field_mapper import StandardRow
from core.fee_sort.rule_engine import Category, ClassificationResult, RuleEngine


def _make_row(
    description: str = "",
    withdrawals: str = "",
    deposits: str = "",
    row_type: str = "transaction",
    **kwargs,
) -> StandardRow:
    """Helper to build a StandardRow with sensible defaults."""
    from core.fee_sort.field_mapper import _to_float

    return StandardRow(
        idx=kwargs.get("idx", 0),
        row_type=row_type,
        statement=kwargs.get("statement", "test_pdf"),
        date=kwargs.get("date", "01/01/2023"),
        description=description,
        withdrawals=withdrawals,
        deposits=deposits,
        balance=kwargs.get("balance", "1000.00"),
        withdrawals_float=_to_float(withdrawals),
        deposits_float=_to_float(deposits),
    )


# =========================================================================
# P0 — Exclude
# =========================================================================

class TestP0Exclude:
    def test_separator_row(self):
        row = _make_row(row_type="separator")
        result = RuleEngine().classify(row)
        assert result.exclude is True
        assert result.rule_hit == "P0-separator"

    def test_opening_balance(self):
        row = _make_row(description="Opening Balance", row_type="opening_balance")
        result = RuleEngine().classify(row)
        assert result.exclude is True
        assert "opening_balance" in result.rule_hit

    def test_deposit_not_excluded_by_rule(self):
        """Deposits are no longer excluded by P0; they are filtered in OutputBuilder."""
        row = _make_row(deposits="500.00")
        result = RuleEngine().classify(row)
        assert result.exclude is False  # P0-deposit removed

    def test_own_account_transfer_keyword(self):
        row = _make_row(description="TRANSFER TO SELF", withdrawals="100.00")
        result = RuleEngine().classify(row)
        assert result.exclude is True
        assert "own_transfer" in result.rule_hit

    def test_own_account_transfer_by_number(self):
        engine = RuleEngine(account_numbers=["12345678"])
        row = _make_row(description="TFR TO 12345678", withdrawals="200.00")
        result = engine.classify(row)
        assert result.exclude is True
        assert "own_transfer" in result.rule_hit

    def test_withdrawal_not_excluded(self):
        """A normal withdrawal with no matching pattern should NOT be excluded."""
        row = _make_row(description="RANDOM PAYMENT", withdrawals="50.00")
        result = RuleEngine().classify(row)
        assert result.exclude is False


# =========================================================================
# P1 — Director Salary
# =========================================================================

class TestP1Salary:
    @pytest.mark.parametrize("desc", ["SALARY PAYMENT", "Monthly Payroll", "Director Pay", "wages"])
    def test_salary_patterns(self, desc):
        row = _make_row(description=desc, withdrawals="3000.00")
        result = RuleEngine().classify(row)
        assert result.category == Category.DIRECTOR_SALARY.value
        assert result.rule_hit == "P1-salary"


# =========================================================================
# P2 — HMRC
# =========================================================================

class TestP2HMRC:
    @pytest.mark.parametrize("desc", [
        "HMRC VAT PAYMENT",
        "PAYE SETTLEMENT",
        "CORPORATION TAX",
        "NATIONAL INSURANCE CONTRIB",
    ])
    def test_hmrc_patterns(self, desc):
        row = _make_row(description=desc, withdrawals="1500.00")
        result = RuleEngine().classify(row)
        assert result.category == Category.HMRC.value
        assert result.rule_hit == "P2-HMRC"


# =========================================================================
# P3 — Bank Charge
# =========================================================================

class TestP3BankCharge:
    @pytest.mark.parametrize("desc", [
        "BANK FEE",
        "SERVICE CHARGE",
        "INTEREST CHARGE",
        "Monthly Fee",
        "BANK CHARGE",
    ])
    def test_bank_charge_patterns(self, desc):
        row = _make_row(description=desc, withdrawals="15.00")
        result = RuleEngine().classify(row)
        assert result.category == Category.BANK_CHARGE.value
        assert result.rule_hit == "P3-bank_charge"


# =========================================================================
# P4 — Office
# =========================================================================

class TestP4Office:
    @pytest.mark.parametrize("desc", [
        "OFFICE RENT PAYMENT",
        "ELECTRICITY BILL",
        "WATER RATES",
        "OFFICE SUPPLIES",
        "TELEPHONE BILL",
        "BROADBAND DIRECT DEBIT",
        "UTILITY PAYMENT",
    ])
    def test_office_patterns(self, desc):
        row = _make_row(description=desc, withdrawals="200.00")
        result = RuleEngine().classify(row)
        assert result.category == Category.OFFICE.value
        assert result.rule_hit == "P4-office"


# =========================================================================
# P5 — Insurance
# =========================================================================

class TestP5Insurance:
    def test_insurance_basic(self):
        row = _make_row(description="INSURANCE PREMIUM DD", withdrawals="100.00")
        result = RuleEngine().classify(row)
        assert result.category == Category.INSURANCE.value
        assert result.need_review is False

    def test_aviva_below_threshold(self):
        row = _make_row(description="AVIVA INSURANCE", withdrawals="400.00")
        result = RuleEngine(aviva_threshold=500.0).classify(row)
        assert result.category == Category.INSURANCE.value
        assert result.need_review is False

    def test_aviva_above_threshold(self):
        row = _make_row(description="AVIVA INSURANCE", withdrawals="600.00")
        result = RuleEngine(aviva_threshold=500.0).classify(row)
        assert result.category == Category.INSURANCE.value
        assert result.need_review is True


# =========================================================================
# P6 — Professional Fee
# =========================================================================

class TestP6Professional:
    @pytest.mark.parametrize("desc", [
        "ACCOUNTANT FEES",
        "SOLICITOR PAYMENT",
        "LEGAL FEE",
        "ANNUAL AUDIT",
        "CONSULTING FEE ACME LTD",
    ])
    def test_professional_patterns(self, desc):
        row = _make_row(description=desc, withdrawals="500.00")
        result = RuleEngine().classify(row)
        assert result.category == Category.PROFESSIONAL_FEE.value
        assert result.rule_hit == "P6-professional"


# =========================================================================
# P7 — Fallback
# =========================================================================

class TestP7Fallback:
    def test_unmatched_goes_to_other(self):
        row = _make_row(description="RANDOM VENDOR PAYMENT XYZ", withdrawals="99.99")
        result = RuleEngine().classify(row)
        assert result.category == Category.OTHER_EXPENSE.value
        assert result.need_review is True
        assert result.rule_hit == "P7-fallback"


# =========================================================================
# Priority ordering
# =========================================================================

class TestPriorityOrder:
    def test_deposit_with_salary_classified_as_salary(self):
        """Deposit-only rows are no longer P0-excluded; salary pattern wins."""
        row = _make_row(description="SALARY REFUND", deposits="500.00")
        result = RuleEngine().classify(row)
        assert result.category == Category.DIRECTOR_SALARY.value

    def test_salary_beats_hmrc(self):
        """P1 salary should win over P2 HMRC if both match."""
        row = _make_row(description="SALARY HMRC", withdrawals="100.00")
        result = RuleEngine().classify(row)
        assert result.rule_hit == "P1-salary"
