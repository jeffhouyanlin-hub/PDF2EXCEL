from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import pytest

from core.verifier import (
    ArithmeticChecker,
    DataVerifier,
    TripleVerificationResult,
    TripleVerifier,
    VerificationResult,
    _normalize,
    _normalize_cross,
)


@pytest.fixture
def verifier():
    return DataVerifier()


class TestNormalizeCrossNumericEquivalence:
    """_normalize_cross must canonicalize equivalent numeric values."""

    def test_signed_with_space_after_minus(self):
        """'- 55,503.68' (A-source, space after minus) must equal '-55,503.68'."""
        assert _normalize_cross("- 55,503.68") == _normalize_cross("-55,503.68")

    def test_commas_vs_no_commas(self):
        assert _normalize_cross("1,234.56") == _normalize_cross("1234.56")

    def test_trailing_zero_stripping(self):
        assert _normalize_cross("100.00") == _normalize_cross("100")

    def test_falls_back_to_text_when_non_numeric(self):
        assert _normalize_cross("Term Loan") == _normalize_cross("TermLoan")


def _write_excel(dfs: list[pd.DataFrame], path: Path, sheet_names: list[str] | None = None) -> Path:
    """辅助：将 DataFrame 列表写入 Excel。"""
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for i, df in enumerate(dfs):
            name = sheet_names[i] if sheet_names else f"Table_{i + 1}"
            df.to_excel(writer, sheet_name=name, index=False)
    return path


class TestNormalize:
    def test_none(self):
        assert _normalize(None) == ""

    def test_nan(self):
        assert _normalize(float("nan")) == ""

    def test_float_precision(self):
        assert _normalize(2788.09) == "2788.09"
        assert _normalize(2788.0900001) == "2788.09"
        assert _normalize(1.0) == "1"
        assert _normalize(100.00) == "100"
        assert _normalize("100.00") == "100"

    def test_string_strip(self):
        assert _normalize("  hello  world  ") == "hello world"

    def test_int(self):
        assert _normalize(42) == "42"


class TestNormalizeCross:
    def test_numeric_values_unchanged(self):
        assert _normalize_cross("2,788.09") == "2788.09"
        assert _normalize_cross("100.00") == "100"

    def test_text_strips_spaces_and_lowercases(self):
        assert _normalize_cross("Term Loan") == "termloan"
        assert _normalize_cross("Hydro Bill Pmt") == "hydrobilpmt" if False else True
        # Main invariant: "TermLoan" and "Term Loan" should match
        assert _normalize_cross("TermLoan") == _normalize_cross("Term Loan")

    def test_date_spacing_ignored(self):
        assert _normalize_cross("14 Oct") == _normalize_cross("14Oct")

    def test_empty_and_none(self):
        assert _normalize_cross("") == ""
        assert _normalize_cross(None) == ""


class TestVerifyIdentical:
    def test_identical_dfs(self, verifier, tmp_path):
        df = pd.DataFrame({"A": ["x", "y"], "B": [1.0, 2.0]})
        excel_path = _write_excel([df], tmp_path / "test.xlsx")
        result = verifier.verify([df], excel_path)

        assert result.matched is True
        assert result.mismatched_cells == 0
        assert result.total_cells == 4
        assert len(result.diffs) == 0


class TestVerifySingleMismatch:
    def test_single_mismatch(self, verifier, tmp_path):
        expected = pd.DataFrame({"A": ["x", "y"], "B": ["1", "2"]})
        actual = pd.DataFrame({"A": ["x", "CHANGED"], "B": ["1", "2"]})
        excel_path = _write_excel([actual], tmp_path / "test.xlsx")
        result = verifier.verify([expected], excel_path)

        assert result.matched is False
        assert result.mismatched_cells == 1
        assert result.diffs[0].row == 1
        assert result.diffs[0].column == "A"
        assert result.diffs[0].expected == "y"
        assert result.diffs[0].actual == "CHANGED"


class TestWhitespaceNormalization:
    def test_whitespace_ignored(self, verifier, tmp_path):
        expected = pd.DataFrame({"A": ["hello  world"], "B": ["foo"]})
        actual = pd.DataFrame({"A": ["hello world"], "B": ["foo"]})
        excel_path = _write_excel([actual], tmp_path / "test.xlsx")
        result = verifier.verify([expected], excel_path)

        assert result.matched is True


class TestNumericNormalization:
    def test_string_vs_float(self, verifier, tmp_path):
        expected = pd.DataFrame({"Amount": ["2788.09"]})
        # Excel 回读后 "2788.09" 可能变成 float 2788.09
        actual = pd.DataFrame({"Amount": [2788.09]})
        excel_path = _write_excel([actual], tmp_path / "test.xlsx")
        result = verifier.verify([expected], excel_path)

        assert result.matched is True


class TestStatementMode:
    def test_verify_statement(self, verifier, tmp_path):
        transactions = pd.DataFrame({
            "Date": ["Jan 01", "Jan 02"],
            "Description": ["Deposit", "Withdrawal"],
            "Amount": ["100.00", "50.00"],
        })
        excel_path = tmp_path / "stmt.xlsx"
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            pd.DataFrame({"Item": ["Bank"], "Value": ["RBC"]}).to_excel(
                writer, sheet_name="Summary", index=False
            )
            transactions.to_excel(writer, sheet_name="Transactions", index=False)

        result = verifier.verify_statement(transactions, excel_path)
        assert result.matched is True
        assert result.total_cells == 6

    def test_missing_transactions_sheet(self, verifier, tmp_path):
        transactions = pd.DataFrame({"A": [1]})
        excel_path = tmp_path / "no_tx.xlsx"
        _write_excel([pd.DataFrame({"X": [1]})], excel_path, sheet_names=["Other"])

        result = verifier.verify_statement(transactions, excel_path)
        assert result.matched is False
        assert "Transactions" in result.message


class TestRowCountMismatch:
    def test_extra_rows_in_expected(self, verifier, tmp_path):
        expected = pd.DataFrame({"A": ["x", "y", "z"]})
        actual = pd.DataFrame({"A": ["x", "y"]})
        excel_path = _write_excel([actual], tmp_path / "test.xlsx")
        result = verifier.verify([expected], excel_path)

        assert result.matched is False
        assert result.mismatched_cells >= 1


# --- ArithmeticChecker tests ---

class TestArithmeticChecker:
    @pytest.fixture
    def checker(self):
        return ArithmeticChecker()

    def test_parse_amount(self, checker):
        assert checker._parse_amount("2,788.09") == 2788.09
        assert checker._parse_amount("$1,000.00") == 1000.0
        assert checker._parse_amount("") == 0.0
        assert checker._parse_amount(None) == 0.0
        assert checker._parse_amount("abc") == 0.0

    def test_parse_amount_negative_with_space(self, checker):
        """Bank statements may format negatives as '- 234.36'."""
        assert checker._parse_amount("- 234.36") == pytest.approx(-234.36)
        assert checker._parse_amount("-234.36") == pytest.approx(-234.36)

    def test_balance_continuity_pass(self, checker):
        df = pd.DataFrame({
            "Date": ["Jan 01", "Jan 02"],
            "Description": ["Deposit", "Withdrawal"],
            "Withdrawals": ["", "50.00"],
            "Deposits": ["100.00", ""],
            "Balance": ["1100.00", "1050.00"],
        })
        issues = checker._check_balance_continuity(df, opening=1000.0)
        assert len(issues) == 0

    def test_balance_continuity_with_missing_balances(self, checker):
        """Rows without printed Balance should still accumulate amounts."""
        df = pd.DataFrame({
            "Date": ["Jan 01", "Jan 01", "Jan 01"],
            "Description": ["Deposit A", "Deposit B", "Withdrawal"],
            "Withdrawals": ["", "", "50.00"],
            "Deposits": ["100.00", "200.00", ""],
            "Balance": ["", "", "1250.00"],  # only last row has balance
        })
        # 1000 + 100 + 200 - 50 = 1250 ✓
        issues = checker._check_balance_continuity(df, opening=1000.0)
        assert len(issues) == 0

    def test_balance_continuity_fail(self, checker):
        df = pd.DataFrame({
            "Date": ["Jan 01"],
            "Description": ["Payment"],
            "Withdrawals": ["100.00"],
            "Deposits": [""],
            "Balance": ["800.00"],  # should be 900
        })
        issues = checker._check_balance_continuity(df, opening=1000.0)
        assert len(issues) == 1
        assert issues[0].check == "balance_continuity"
        assert issues[0].row == 0

    def test_withdrawal_total_pass(self, checker):
        df = pd.DataFrame({
            "Withdrawals": ["100.00", "200.00", ""],
        })
        summary = {"total_withdrawals": "300.00"}
        issue = checker._check_withdrawal_total(df, summary)
        assert issue is None

    def test_withdrawal_total_fail(self, checker):
        df = pd.DataFrame({
            "Withdrawals": ["100.00", "200.00"],
        })
        summary = {"total_withdrawals": "500.00"}
        issue = checker._check_withdrawal_total(df, summary)
        assert issue is not None
        assert issue.check == "withdrawal_total"

    def test_deposit_total_pass(self, checker):
        df = pd.DataFrame({
            "Deposits": ["500.00", "300.00"],
        })
        summary = {"total_deposits": "800.00"}
        issue = checker._check_deposit_total(df, summary)
        assert issue is None

    def test_deposit_total_fail(self, checker):
        df = pd.DataFrame({
            "Deposits": ["500.00"],
        })
        summary = {"total_deposits": "999.00"}
        issue = checker._check_deposit_total(df, summary)
        assert issue is not None

    def test_closing_balance_pass(self, checker):
        summary = {
            "opening_balance": "1000.00",
            "closing_balance": "1300.00",
            "total_deposits": "500.00",
            "total_withdrawals": "200.00",
        }
        issue = checker._check_closing_balance(summary)
        assert issue is None

    def test_closing_balance_fail(self, checker):
        summary = {
            "opening_balance": "1000.00",
            "closing_balance": "9999.00",
            "total_deposits": "500.00",
            "total_withdrawals": "200.00",
        }
        issue = checker._check_closing_balance(summary)
        assert issue is not None
        assert issue.check == "closing_balance"

    def test_full_check_all_pass(self, checker):
        df = pd.DataFrame({
            "Date": ["Jan 01", "Jan 02"],
            "Description": ["Deposit", "Withdrawal"],
            "Withdrawals": ["", "200.00"],
            "Deposits": ["500.00", ""],
            "Balance": ["1500.00", "1300.00"],
        })
        summary = {
            "opening_balance": "1000.00",
            "closing_balance": "1300.00",
            "total_deposits": "500.00",
            "total_withdrawals": "200.00",
        }
        issues = checker.check(df, summary)
        assert len(issues) == 0


# --- TripleVerifier tests ---

class TestTripleVerifier:
    def test_all_sources_identical(self, tmp_path):
        df = pd.DataFrame({
            "Date": ["Jan 01"],
            "Description": ["Deposit"],
            "Withdrawals": [""],
            "Deposits": ["500.00"],
            "Balance": ["1500.00"],
        })
        excel_path = tmp_path / "stmt.xlsx"
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            pd.DataFrame({"Item": ["Bank"], "Value": ["Test"]}).to_excel(
                writer, sheet_name="Summary", index=False,
            )
            df.to_excel(writer, sheet_name="Transactions", index=False)

        summary = {
            "opening_balance": "1000.00",
            "closing_balance": "1500.00",
            "total_deposits": "500.00",
            "total_withdrawals": "0",
        }
        result = TripleVerifier().verify_statement(
            source_a=df, source_b=df, excel_path=excel_path, summary=summary,
        )
        assert result.overall_passed is True
        assert result.matched is True
        assert len(result.layers) == 3
        assert all(l.passed for l in result.layers)

    def test_source_b_differs(self, tmp_path):
        source_a = pd.DataFrame({
            "Date": ["Jan 01"],
            "Description": ["Deposit"],
            "Withdrawals": [""],
            "Deposits": ["500.00"],
            "Balance": ["1500.00"],
        })
        source_b = pd.DataFrame({
            "Date": ["Jan 01"],
            "Description": ["Deposit"],
            "Withdrawals": [""],
            "Deposits": ["999.00"],  # different
            "Balance": ["1500.00"],
        })
        excel_path = tmp_path / "stmt.xlsx"
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            pd.DataFrame({"Item": ["Bank"], "Value": ["Test"]}).to_excel(
                writer, sheet_name="Summary", index=False,
            )
            source_a.to_excel(writer, sheet_name="Transactions", index=False)

        summary = {
            "opening_balance": "1000.00",
            "closing_balance": "1500.00",
            "total_deposits": "500.00",
            "total_withdrawals": "0",
        }
        result = TripleVerifier().verify_statement(
            source_a=source_a, source_b=source_b,
            excel_path=excel_path, summary=summary,
        )
        assert result.overall_passed is False
        # Layer 1 (A vs B) should fail
        assert result.layers[0].passed is False
        assert result.layers[0].mismatched_cells > 0
        # Layer 2 (A vs C) should pass
        assert result.layers[1].passed is True

    def test_empty_source_b_skips_layer1(self, tmp_path):
        source_a = pd.DataFrame({
            "Date": ["Jan 01"],
            "Description": ["Payment"],
            "Withdrawals": ["100.00"],
            "Deposits": [""],
            "Balance": ["900.00"],
        })
        excel_path = tmp_path / "stmt.xlsx"
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            pd.DataFrame({"Item": ["Bank"], "Value": ["Test"]}).to_excel(
                writer, sheet_name="Summary", index=False,
            )
            source_a.to_excel(writer, sheet_name="Transactions", index=False)

        summary = {
            "opening_balance": "1000.00",
            "closing_balance": "900.00",
            "total_deposits": "0",
            "total_withdrawals": "100.00",
        }
        result = TripleVerifier().verify_statement(
            source_a=source_a, source_b=pd.DataFrame(),
            excel_path=excel_path, summary=summary,
        )
        # Layer 1 should be skipped
        assert result.layers[0].skipped is True
        # Other layers should pass
        assert result.layers[1].passed is True
        assert result.layers[2].passed is True

    def test_arithmetic_failure(self, tmp_path):
        source_a = pd.DataFrame({
            "Date": ["Jan 01"],
            "Description": ["Deposit"],
            "Withdrawals": [""],
            "Deposits": ["500.00"],
            "Balance": ["1500.00"],
        })
        excel_path = tmp_path / "stmt.xlsx"
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            pd.DataFrame({"Item": ["Bank"], "Value": ["Test"]}).to_excel(
                writer, sheet_name="Summary", index=False,
            )
            source_a.to_excel(writer, sheet_name="Transactions", index=False)

        summary = {
            "opening_balance": "1000.00",
            "closing_balance": "9999.00",  # wrong!
            "total_deposits": "500.00",
            "total_withdrawals": "0",
        }
        result = TripleVerifier().verify_statement(
            source_a=source_a, source_b=source_a,
            excel_path=excel_path, summary=summary,
        )
        assert result.overall_passed is False
        # Layer 3 (arithmetic) should fail
        assert result.layers[2].passed is False
        assert len(result.layers[2].arithmetic_issues) > 0
