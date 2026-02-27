from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.batch import BatchProcessor, BatchResult, FileResult
from core.extractor import ExtractionResult


@pytest.fixture
def processor():
    return BatchProcessor(max_workers=2)


class TestBatchProcessor:
    @patch.object(BatchProcessor, "_process_one")
    def test_process_success(self, mock_one, processor, tmp_path):
        mock_one.return_value = FileResult(
            source="a.pdf", output="a.xlsx", table_count=2
        )
        result = processor.process([Path("a.pdf")], tmp_path)
        assert result.total == 1
        assert result.succeeded == 1
        assert result.failed == 0

    @patch.object(BatchProcessor, "_process_one")
    def test_process_with_failure(self, mock_one, processor, tmp_path):
        mock_one.side_effect = [
            FileResult(source="a.pdf", output="a.xlsx", table_count=1),
            FileResult(source="b.pdf", success=False, error="bad pdf"),
        ]
        result = processor.process([Path("a.pdf"), Path("b.pdf")], tmp_path)
        assert result.total == 2
        assert result.succeeded == 1
        assert result.failed == 1

    @patch.object(BatchProcessor, "_process_one")
    def test_progress_callback(self, mock_one, processor, tmp_path):
        mock_one.return_value = FileResult(source="a.pdf", output="a.xlsx")
        calls = []
        processor.process(
            [Path("a.pdf")],
            tmp_path,
            on_progress=lambda c, t, r: calls.append((c, t)),
        )
        assert calls == [(1, 1)]

    @patch("core.batch.PDFExtractor")
    @patch("core.batch.ExcelConverter")
    def test_process_one_integration(self, mock_conv_cls, mock_ext_cls, tmp_path):
        mock_ext = mock_ext_cls.return_value
        mock_ext.extract.return_value = ExtractionResult(
            tables=[pd.DataFrame({"A": [1]})], page_count=1, source="x.pdf"
        )
        mock_conv = mock_conv_cls.return_value
        mock_conv.convert.return_value = tmp_path / "x.xlsx"

        proc = BatchProcessor()
        fr = proc._process_one(Path("x.pdf"), tmp_path, True)
        assert fr.success
        assert fr.table_count == 1

    def test_process_one_error_handled(self, tmp_path):
        proc = BatchProcessor()
        fr = proc._process_one(Path("/nonexistent.pdf"), tmp_path, True)
        assert not fr.success
        assert fr.error is not None
