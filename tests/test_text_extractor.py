"""Tests for core.text_extractor (Source B)."""
from __future__ import annotations

import pytest

from core.text_extractor import TextBasedExtractor, TextExtractionResult


@pytest.fixture
def extractor():
    return TextBasedExtractor()


class TestFindHeader:
    def test_finds_standard_header(self, extractor):
        lines = [
            "Some summary line",
            "Another line",
            "    Date       Description                    Withdrawals($)   Deposits($)    Balance($)",
            "    Jan 01     Opening Balance                                                 1,000.00",
        ]
        idx, bounds = extractor._find_header(lines)
        assert idx == 2
        assert "Date" in bounds
        assert "Withdrawals" in bounds
        assert "Deposits" in bounds
        assert "Balance" in bounds

    def test_no_header_returns_negative(self, extractor):
        lines = ["This is random text", "Nothing useful here"]
        idx, bounds = extractor._find_header(lines)
        assert idx == -1
        assert bounds == {}

    def test_header_without_description(self, extractor):
        """Description is optional, only 4 required columns."""
        lines = [
            "    Date       Withdrawals   Deposits    Balance",
        ]
        idx, bounds = extractor._find_header(lines)
        assert idx == 0
        assert "Date" in bounds
        assert "Description" not in bounds  # not required

    def test_column_bounds_ordering(self, extractor):
        lines = [
            "  Date    Description       Withdrawals   Deposits   Balance",
        ]
        idx, bounds = extractor._find_header(lines)
        # Each column's start should be < next column's start
        starts = [bounds[k][0] for k in sorted(bounds, key=lambda k: bounds[k][0])]
        assert starts == sorted(starts)


class TestSliceLine:
    def test_basic_slicing(self, extractor):
        bounds = {
            "Date": (0, 10),
            "Description": (10, 40),
            "Withdrawals": (40, 55),
            "Deposits": (55, 70),
            "Balance": (70, 120),
        }
        line = "Jan 01    Opening Balance                                     1,000.00"
        result = extractor._slice_line(line, bounds)
        assert result["Date"] == "Jan 01"
        assert "Opening Balance" in result["Description"]

    def test_short_line(self, extractor):
        bounds = {"Date": (0, 10), "Balance": (70, 120)}
        line = "Jan 01"
        result = extractor._slice_line(line, bounds)
        assert result["Date"] == "Jan 01"
        assert result["Balance"] == ""


class TestSliceLineOverflow:
    """Test that text overflow from Description into numeric columns is fixed."""

    def test_text_overflow_into_withdrawals(self, extractor):
        """E.g. 'ZR 41.59' in Withdrawals → Description gets 'ZR', Withdrawals gets '41.59'."""
        bounds = {
            "Date": (0, 10),
            "Description": (10, 40),
            "Withdrawals": (40, 55),
            "Deposits": (55, 70),
            "Balance": (70, 120),
        }
        line = "Jan 01    Some Description                ZR 41.59"
        result = extractor._slice_line(line, bounds)
        assert result["Withdrawals"] == "41.59"
        assert "ZR" in result["Description"]

    def test_numeric_code_overflow(self, extractor):
        """E.g. '742 300.00' in Withdrawals → Description gets '742', Withdrawals gets '300.00'."""
        bounds = {
            "Date": (0, 10),
            "Description": (10, 40),
            "Withdrawals": (40, 55),
            "Deposits": (55, 70),
            "Balance": (70, 120),
        }
        line = "Jan 01    Online Transfer Account        742 300.00"
        result = extractor._slice_line(line, bounds)
        assert result["Withdrawals"] == "300.00"
        assert "742" in result["Description"]

    def test_pure_text_overflow(self, extractor):
        """E.g. 'SINC' in Withdrawals (no amount) → moved to Description."""
        bounds = {
            "Date": (0, 10),
            "Description": (10, 40),
            "Withdrawals": (40, 55),
            "Deposits": (55, 70),
            "Balance": (70, 120),
        }
        line = "Jan 01    CAERUS ENTERPRI               SINC"
        result = extractor._slice_line(line, bounds)
        assert result["Withdrawals"] == ""
        assert "SINC" in result["Description"]

    def test_normal_amount_not_modified(self, extractor):
        """Normal numeric values should not be treated as overflow."""
        bounds = {
            "Date": (0, 10),
            "Description": (10, 40),
            "Withdrawals": (40, 55),
            "Deposits": (55, 70),
            "Balance": (70, 120),
        }
        line = "Jan 01    Payment                        100.00"
        result = extractor._slice_line(line, bounds)
        assert result["Withdrawals"] == "100.00"


class TestFindHeaderMidpointBoundary:
    """Test that Balance column boundary uses midpoint for right-aligned values."""

    def test_balance_boundary_uses_midpoint(self, extractor):
        lines = [
            "  Date   Description                    Withdrawals($) Deposits($)       Balance($)",
        ]
        idx, bounds = extractor._find_header(lines)
        # Balance should start before the "Balance" keyword position (73)
        # because of the midpoint adjustment between Deposits end and Balance start
        assert bounds["Balance"][0] < 73
        # Deposits should end at the same position where Balance starts
        assert bounds["Deposits"][1] == bounds["Balance"][0]


class TestBuildDataframe:
    def test_empty_rows(self, extractor):
        df = extractor._build_dataframe([])
        assert list(df.columns) == ["Date", "Description", "Withdrawals", "Deposits", "Balance"]
        assert len(df) == 0

    def test_noise_filtered(self, extractor):
        rows = [
            {"Date": "Jan 01", "Description": "Payment", "Withdrawals": "100.00",
             "Deposits": "", "Balance": "900.00"},
            {"Date": "", "Description": "3 of 5", "Withdrawals": "",
             "Deposits": "", "Balance": ""},  # noise
        ]
        df = extractor._build_dataframe(rows)
        assert len(df) == 1

    def test_closing_balance_truncation(self, extractor):
        rows = [
            {"Date": "Jan 01", "Description": "Payment", "Withdrawals": "100.00",
             "Deposits": "", "Balance": "900.00"},
            {"Date": "Jan 31", "Description": "Closing Balance", "Withdrawals": "",
             "Deposits": "", "Balance": "900.00"},
            {"Date": "Feb 01", "Description": "This should not appear", "Withdrawals": "",
             "Deposits": "", "Balance": "800.00"},
        ]
        df = extractor._build_dataframe(rows)
        assert len(df) == 2
        assert "Closing Balance" in df.iloc[-1]["Description"]

    def test_date_forward_fill(self, extractor):
        rows = [
            {"Date": "Jan 01", "Description": "First", "Withdrawals": "50.00",
             "Deposits": "", "Balance": "950.00"},
            {"Date": "", "Description": "Second", "Withdrawals": "25.00",
             "Deposits": "", "Balance": "925.00"},
        ]
        df = extractor._build_dataframe(rows)
        assert df.iloc[1]["Date"] == "Jan 01"

    def test_continuation_merge(self, extractor):
        rows = [
            {"Date": "Jan 01", "Description": "Long description", "Withdrawals": "",
             "Deposits": "", "Balance": ""},
            {"Date": "", "Description": "continued here", "Withdrawals": "100.00",
             "Deposits": "", "Balance": "900.00"},
        ]
        df = extractor._build_dataframe(rows)
        assert len(df) == 1
        assert "continued here" in df.iloc[0]["Description"]
        assert df.iloc[0]["Withdrawals"] == "100.00"


class TestExtractSummary:
    def test_extracts_opening_balance(self, extractor):
        lines = ["Opening Balance   $1,234.56", "Some other line"]
        summary = extractor._extract_summary(lines)
        assert summary.get("opening_balance") == "1,234.56"

    def test_extracts_closing_balance(self, extractor):
        lines = ["Closing Balance =   $5,678.90"]
        summary = extractor._extract_summary(lines)
        assert summary.get("closing_balance") == "5,678.90"

    def test_empty_lines(self, extractor):
        summary = extractor._extract_summary([])
        assert summary == {}
