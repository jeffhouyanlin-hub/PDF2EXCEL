"""Unit tests for Fee Sort year filtering logic."""

from __future__ import annotations

import pytest

from core.fee_sort.field_mapper import StandardRow, _to_float
from fee_sort_page import (
    _determine_majority_year,
    _filter_january_rows,
    _tx_month,
)


def _sr(
    idx: int = 0,
    statement: str = "pdf1",
    date: str = "Jan 15",
    description: str = "PAYMENT",
    withdrawals: str = "100.00",
    deposits: str = "",
    row_type: str = "transaction",
) -> StandardRow:
    return StandardRow(
        idx=idx,
        row_type=row_type,
        statement=statement,
        date=date,
        description=description,
        withdrawals=withdrawals,
        deposits=deposits,
        balance="1000",
        withdrawals_float=_to_float(withdrawals),
        deposits_float=_to_float(deposits),
    )


class TestTxMonth:
    def test_jan(self):
        assert _tx_month("Jan 03") == 1

    def test_dec(self):
        assert _tx_month("Dec 28") == 12

    def test_empty(self):
        assert _tx_month("") is None

    def test_numeric_date(self):
        assert _tx_month("15/03/2025") == 3


class TestDetermineMajorityYear:
    def test_jan_and_apr_same_year(self):
        """Jan 2025 + Apr 2025 → main year = 2025."""
        files = {
            "jan_pdf": {"summary_pdf_vals": {"Period From": "January 1, 2025", "Period To": "January 31, 2025"}},
            "apr_pdf": {"summary_pdf_vals": {"Period From": "April 1, 2025", "Period To": "April 30, 2025"}},
        }
        assert _determine_majority_year(files) == 2025

    def test_jan_next_year_and_apr(self):
        """Jan 2026 + Apr 2025 → main year = 2025."""
        files = {
            "jan_pdf": {"summary_pdf_vals": {"Period From": "January 1, 2026", "Period To": "January 31, 2026"}},
            "apr_pdf": {"summary_pdf_vals": {"Period From": "April 1, 2025", "Period To": "April 30, 2025"}},
        }
        assert _determine_majority_year(files) == 2025

    def test_multiple_same_year(self):
        """Multiple months in 2025 → main year = 2025."""
        files = {
            "mar": {"summary_pdf_vals": {"Period From": "March 1, 2025", "Period To": "March 31, 2025"}},
            "apr": {"summary_pdf_vals": {"Period From": "April 1, 2025", "Period To": "April 30, 2025"}},
            "may": {"summary_pdf_vals": {"Period From": "May 1, 2025", "Period To": "May 31, 2025"}},
        }
        assert _determine_majority_year(files) == 2025

    def test_jan_period_from_in_prev_year(self):
        """Jan 2025 statement with period_from in Dec 2024 + Oct 2025 → main year = 2025.

        This is the real-world case: a January statement's period_from is in
        December of the previous year (e.g. Dec 11, 2024 to Jan 10, 2025).
        Year and month must be determined from period_to, not period_from.
        """
        files = {
            "jan_pdf": {"summary_pdf_vals": {
                "Period From": "December 11, 2024",
                "Period To": "January 10, 2025",
            }},
            "oct_pdf": {"summary_pdf_vals": {
                "Period From": "September 11, 2025",
                "Period To": "October 10, 2025",
            }},
        }
        assert _determine_majority_year(files) == 2025

    def test_all_january_picks_smallest_year(self):
        """All PDFs are January → pick smallest year."""
        files = {
            "jan25": {"summary_pdf_vals": {"Period From": "December 11, 2024", "Period To": "January 10, 2025"}},
            "jan26": {"summary_pdf_vals": {"Period From": "December 11, 2025", "Period To": "January 10, 2026"}},
        }
        assert _determine_majority_year(files) == 2025

    def test_no_files(self):
        assert _determine_majority_year({}) is None


class TestFilterJanuaryRows:
    def _stem_period(self, stem: str, year: int, month: int):
        return {stem: (year, month)}

    def test_jan_main_year_removes_dec(self):
        """Jan 2025 (main year=2025): Dec rows removed, Jan rows kept."""
        rows = [
            _sr(statement="jan_pdf", date="Dec 28", description="DEC TX"),
            _sr(statement="jan_pdf", date="Jan 05", description="JAN TX"),
        ]
        mapping = [("jan_pdf", 0), ("jan_pdf", 1)]
        stem_period = {"jan_pdf": (2025, 1)}

        filtered = _filter_january_rows(rows, mapping, stem_period, majority_year=2025)
        assert len(filtered) == 1
        assert filtered[0][0].description == "JAN TX"

    def test_jan_next_year_removes_jan(self):
        """Jan 2026 (main year=2025): Jan rows removed, Dec rows kept."""
        rows = [
            _sr(statement="jan_pdf", date="Dec 28", description="DEC TX"),
            _sr(statement="jan_pdf", date="Jan 05", description="JAN TX"),
        ]
        mapping = [("jan_pdf", 0), ("jan_pdf", 1)]
        stem_period = {"jan_pdf": (2026, 1)}

        filtered = _filter_january_rows(rows, mapping, stem_period, majority_year=2025)
        assert len(filtered) == 1
        assert filtered[0][0].description == "DEC TX"

    def test_non_january_pdf_untouched(self):
        """April PDF rows are not filtered."""
        rows = [
            _sr(statement="apr_pdf", date="Apr 15", description="APR TX"),
        ]
        mapping = [("apr_pdf", 0)]
        stem_period = {"apr_pdf": (2025, 4)}

        filtered = _filter_january_rows(rows, mapping, stem_period, majority_year=2025)
        assert len(filtered) == 1

    def test_separator_and_ob_preserved(self):
        """Separator and Opening Balance rows always kept."""
        rows = [
            _sr(row_type="separator", statement="", date=""),
            _sr(row_type="opening_balance", statement="jan_pdf", date="Jan 01", description="Opening Balance"),
            _sr(statement="jan_pdf", date="Dec 28", description="DEC TX"),
        ]
        mapping = [("__separator__", -1), ("jan_pdf", -1), ("jan_pdf", 0)]
        stem_period = {"jan_pdf": (2025, 1)}

        filtered = _filter_january_rows(rows, mapping, stem_period, majority_year=2025)
        # separator + OB kept, Dec TX removed
        assert len(filtered) == 2
        assert filtered[0][0].row_type == "separator"
        assert filtered[1][0].row_type == "opening_balance"
