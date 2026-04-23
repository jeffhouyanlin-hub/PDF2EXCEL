"""Unit tests for core.fee_sort.output_builder."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from core.fee_sort.field_mapper import StandardRow
from core.fee_sort.output_builder import OUTPUT_COLUMNS, OutputBuilder
from core.fee_sort.rule_engine import ClassificationResult


def _sr(
    idx: int = 0,
    row_type: str = "transaction",
    description: str = "TEST",
    withdrawals: str = "100.00",
    deposits: str = "",
    **kw,
) -> StandardRow:
    from core.fee_sort.field_mapper import _to_float
    return StandardRow(
        idx=idx,
        row_type=row_type,
        statement=kw.get("statement", "pdf1"),
        date=kw.get("date", "01/01/2023"),
        description=description,
        withdrawals=withdrawals,
        deposits=deposits,
        balance=kw.get("balance", "900"),
        withdrawals_float=_to_float(withdrawals),
        deposits_float=_to_float(deposits),
    )


class TestOutputBuilder:
    def test_basic_build(self):
        rows = [_sr(description="SALARY", withdrawals="3000")]
        cls = [ClassificationResult(category="Director Salary", detail="salary", rule_hit="P1-salary")]
        df = OutputBuilder.build(rows, cls, "Barclays_1234", "2023")

        assert list(df.columns) == OUTPUT_COLUMNS
        assert len(df) == 1
        assert df.iloc[0]["Bank_Acc"] == "Barclays_1234"
        assert df.iloc[0]["Category"] == "Director Salary"

    def test_separator_skipped(self):
        rows = [
            _sr(row_type="separator"),
            _sr(description="PAYMENT", withdrawals="50"),
        ]
        cls = [
            ClassificationResult(exclude=True, rule_hit="P0-separator"),
            ClassificationResult(category="Other Expense", rule_hit="P7-fallback", need_review=True),
        ]
        df = OutputBuilder.build(rows, cls, "HSBC_5678", "2023")
        assert len(df) == 1  # separator excluded
        assert df.iloc[0]["Description"] == "PAYMENT"

    def test_opening_balance_skipped(self):
        rows = [
            _sr(description="Opening Balance", row_type="opening_balance", withdrawals=""),
            _sr(description="PAYMENT", withdrawals="50"),
        ]
        cls = [
            ClassificationResult(exclude=True, detail="Opening Balance", rule_hit="P0-opening_balance"),
            ClassificationResult(category="Office", detail="rent", rule_hit="P4-office"),
        ]
        df = OutputBuilder.build(rows, cls, "X_0000", "2023")
        assert len(df) == 1
        assert df.iloc[0]["Description"] == "PAYMENT"

    def test_closing_balance_skipped(self):
        rows = [
            _sr(description="Closing Balance", withdrawals=""),
            _sr(description="RENT PAYMENT", withdrawals="500"),
        ]
        cls = [
            ClassificationResult(category="Other Expense", rule_hit="P7-fallback", need_review=True),
            ClassificationResult(category="Office", detail="rent", rule_hit="P4-office"),
        ]
        df = OutputBuilder.build(rows, cls, "X_0000", "2023")
        assert len(df) == 1
        assert df.iloc[0]["Description"] == "RENT PAYMENT"

    def test_need_review_flag(self):
        rows = [_sr(description="RANDOM", withdrawals="100")]
        cls = [ClassificationResult(category="Other Expense", rule_hit="P7-fallback", need_review=True)]
        df = OutputBuilder.build(rows, cls, "X_0000", "2023")
        assert df.iloc[0]["Need_Review"] == "Y"

    def test_to_excel_bytes(self):
        rows = [_sr(description="TEST", withdrawals="100")]
        cls = [ClassificationResult(category="Office", detail="rent", rule_hit="P4-office")]
        df = OutputBuilder.build(rows, cls, "Test_0000", "2023")
        xlsx_bytes = OutputBuilder.to_excel_bytes(df)

        # Read back and verify
        result = pd.read_excel(io.BytesIO(xlsx_bytes), sheet_name="FeeSort")
        assert len(result) == 1
        assert result.iloc[0]["Category"] == "Office"

    def test_deposit_rows_skipped(self):
        """Deposit-only rows should be completely excluded from output."""
        rows = [
            _sr(description="DEPOSIT", withdrawals="", deposits="500.00"),
            _sr(description="PAYMENT", withdrawals="100.00", deposits=""),
        ]
        cls = [
            ClassificationResult(category="Other Expense", rule_hit="P7-fallback", need_review=True),
            ClassificationResult(category="Office", detail="rent", rule_hit="P4-office"),
        ]
        df = OutputBuilder.build(rows, cls, "X_0000", "2023")
        assert len(df) == 1  # deposit row skipped
        assert df.iloc[0]["Description"] == "PAYMENT"
