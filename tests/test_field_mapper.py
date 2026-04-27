"""Unit tests for core.fee_sort.field_mapper."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from core.fee_sort.field_mapper import FieldMapper, StandardRow, _to_float


class TestToFloat:
    def test_plain_number(self):
        assert _to_float("123.45") == 123.45

    def test_commas(self):
        assert _to_float("1,234.56") == 1234.56

    def test_empty(self):
        assert _to_float("") == 0.0

    def test_none_like(self):
        assert _to_float("nan") == 0.0

    def test_currency_symbol(self):
        assert _to_float("£1,000.00") == 1000.0


class TestFieldMapper:
    @staticmethod
    def _make_excel_bytes(rows: list[dict], columns: list[str] | None = None) -> bytes:
        cols = columns or ["Statement", "Date", "Description", "Withdrawals", "Deposits", "Balance"]
        df = pd.DataFrame(rows, columns=cols)
        buf = io.BytesIO()
        df.to_excel(buf, index=False)
        return buf.getvalue()

    def test_basic_mapping(self):
        excel = self._make_excel_bytes([
            {"Statement": "pdf1", "Date": "01/01/2023", "Description": "Opening Balance",
             "Withdrawals": "", "Deposits": "", "Balance": "1000"},
            {"Statement": "pdf1", "Date": "02/01/2023", "Description": "Payment",
             "Withdrawals": "50", "Deposits": "", "Balance": "950"},
        ])
        mapping = [("pdf1", -1), ("pdf1", 0)]
        rows = FieldMapper.map(excel, mapping)

        assert len(rows) == 2
        assert rows[0].row_type == "opening_balance"
        assert rows[1].row_type == "transaction"
        assert rows[1].withdrawals_float == 50.0

    def test_separator_rows(self):
        excel = self._make_excel_bytes([
            {"Statement": "", "Date": "", "Description": "", "Withdrawals": "", "Deposits": "", "Balance": ""},
        ])
        mapping = [("__separator__", -1)]
        rows = FieldMapper.map(excel, mapping)

        assert len(rows) == 1
        assert rows[0].row_type == "separator"

    def test_mapping_shorter_than_df(self):
        """If row_mapping is shorter than DataFrame, extra rows are ignored."""
        excel = self._make_excel_bytes([
            {"Statement": "a", "Date": "", "Description": "X", "Withdrawals": "", "Deposits": "", "Balance": ""},
            {"Statement": "b", "Date": "", "Description": "Y", "Withdrawals": "", "Deposits": "", "Balance": ""},
        ])
        mapping = [("a", 0)]
        rows = FieldMapper.map(excel, mapping)
        assert len(rows) == 1
