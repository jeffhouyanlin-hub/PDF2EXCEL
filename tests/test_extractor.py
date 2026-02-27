from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.extractor import ExtractionResult, PDFExtractor


class TestRawToDataframe:
    def test_normal_table(self):
        raw = [["Name", "Age"], ["Alice", "30"], ["Bob", "25"]]
        df = PDFExtractor._raw_to_dataframe(raw)
        assert list(df.columns) == ["Name", "Age"]
        assert len(df) == 2
        assert df.iloc[0]["Name"] == "Alice"

    def test_none_header_cells(self):
        raw = [[None, "Value"], ["a", "1"]]
        df = PDFExtractor._raw_to_dataframe(raw)
        assert df.columns[0] == "col_0"
        assert df.columns[1] == "Value"

    def test_single_row(self):
        raw = [["only", "row"]]
        df = PDFExtractor._raw_to_dataframe(raw)
        assert len(df) == 1

    def test_empty(self):
        df = PDFExtractor._raw_to_dataframe([])
        assert df.empty

    def test_whitespace_stripped(self):
        raw = [["Name"], ["  Alice  "]]
        df = PDFExtractor._raw_to_dataframe(raw)
        assert df.iloc[0]["Name"] == "Alice"


class TestExtract:
    def _make_mock_page(self, tables: list[list[list[str | None]]]):
        page = MagicMock()
        page.extract_tables.return_value = tables
        return page

    @patch("core.extractor.pdfplumber.open")
    def test_extract_all_pages(self, mock_open):
        page1 = self._make_mock_page([[["A", "B"], ["1", "2"]]])
        page2 = self._make_mock_page([[["C", "D"], ["3", "4"]]])
        mock_pdf = MagicMock()
        mock_pdf.pages = [page1, page2]
        mock_pdf.__enter__ = lambda s: s
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = mock_pdf

        result = PDFExtractor().extract("test.pdf")
        assert len(result.tables) == 2
        assert result.page_count == 2
        assert result.source == "test.pdf"

    @patch("core.extractor.pdfplumber.open")
    def test_extract_specific_pages(self, mock_open):
        page1 = self._make_mock_page([[["A", "B"], ["1", "2"]]])
        page2 = self._make_mock_page([[["C", "D"], ["3", "4"]]])
        mock_pdf = MagicMock()
        mock_pdf.pages = [page1, page2]
        mock_pdf.__enter__ = lambda s: s
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = mock_pdf

        result = PDFExtractor().extract("test.pdf", pages=[2])
        assert len(result.tables) == 1
        assert result.tables[0].columns.tolist() == ["C", "D"]

    @patch("core.extractor.pdfplumber.open")
    def test_empty_tables_skipped(self, mock_open):
        page = self._make_mock_page([[], [["A"], ["1"]]])
        mock_pdf = MagicMock()
        mock_pdf.pages = [page]
        mock_pdf.__enter__ = lambda s: s
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = mock_pdf

        result = PDFExtractor().extract("test.pdf")
        assert len(result.tables) == 1
