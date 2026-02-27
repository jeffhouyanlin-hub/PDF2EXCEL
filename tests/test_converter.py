from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from core.converter import ExcelConverter
from core.extractor import ExtractionResult


@pytest.fixture
def sample_result():
    return ExtractionResult(
        tables=[
            pd.DataFrame({"Name": ["Alice", "Bob"], "Age": ["30", "25"]}),
            pd.DataFrame({"City": ["NYC", "LA"], "Pop": ["8M", "4M"]}),
        ],
        page_count=1,
        source="test.pdf",
    )


@pytest.fixture
def converter():
    return ExcelConverter()


class TestConvert:
    def test_sheet_per_table(self, tmp_path, converter, sample_result):
        out = tmp_path / "out.xlsx"
        result = converter.convert(sample_result, out, sheet_per_table=True)
        assert result.exists()
        sheets = pd.read_excel(out, sheet_name=None, engine="openpyxl")
        assert len(sheets) == 2
        assert "Table_1" in sheets
        assert "Table_2" in sheets
        assert list(sheets["Table_1"].columns) == ["Name", "Age"]

    def test_single_sheet(self, tmp_path, converter, sample_result):
        out = tmp_path / "out.xlsx"
        converter.convert(sample_result, out, sheet_per_table=False)
        sheets = pd.read_excel(out, sheet_name=None, engine="openpyxl")
        assert len(sheets) == 1
        assert "All_Tables" in sheets
        assert len(sheets["All_Tables"]) == 4  # 2 + 2 rows combined

    def test_empty_tables(self, tmp_path, converter):
        result = ExtractionResult(tables=[], page_count=0, source="empty.pdf")
        out = tmp_path / "out.xlsx"
        path = converter.convert(result, out)
        assert path.exists()

    def test_output_dir_created(self, tmp_path, converter, sample_result):
        out = tmp_path / "sub" / "dir" / "out.xlsx"
        converter.convert(sample_result, out)
        assert out.exists()
