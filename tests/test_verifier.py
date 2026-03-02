from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import pytest

from core.verifier import DataVerifier, VerificationResult, _normalize


@pytest.fixture
def verifier():
    return DataVerifier()


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
