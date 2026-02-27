from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.converter import ExcelConverter
from core.extractor import ExtractionResult, PDFExtractor


@dataclass
class FileResult:
    source: str
    output: str | None = None
    table_count: int = 0
    success: bool = True
    error: str | None = None


@dataclass
class BatchResult:
    results: list[FileResult] = field(default_factory=list)
    total: int = 0
    succeeded: int = 0
    failed: int = 0


class BatchProcessor:
    """批量处理多个 PDF 文件。"""

    def __init__(self, max_workers: int = 4):
        self._extractor = PDFExtractor()
        self._converter = ExcelConverter()
        self._max_workers = max_workers

    def process(
        self,
        paths: list[Path],
        output_dir: Path,
        sheet_per_table: bool = True,
        on_progress: Callable[[int, int, FileResult], None] | None = None,
    ) -> BatchResult:
        """批量处理 PDF 文件。

        Args:
            paths: PDF 文件路径列表
            output_dir: 输出目录
            sheet_per_table: 每个表一个 sheet
            on_progress: 回调 (completed, total, result)
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        batch = BatchResult(total=len(paths))
        completed = 0

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(self._process_one, p, output_dir, sheet_per_table): p
                for p in paths
            }
            for future in as_completed(futures):
                file_result = future.result()
                batch.results.append(file_result)
                if file_result.success:
                    batch.succeeded += 1
                else:
                    batch.failed += 1
                completed += 1
                if on_progress:
                    on_progress(completed, batch.total, file_result)

        return batch

    def _process_one(
        self, pdf_path: Path, output_dir: Path, sheet_per_table: bool
    ) -> FileResult:
        try:
            extraction = self._extractor.extract(pdf_path)
            output_name = pdf_path.stem + ".xlsx"
            output_path = output_dir / output_name
            self._converter.convert(extraction, output_path, sheet_per_table)
            return FileResult(
                source=str(pdf_path),
                output=str(output_path),
                table_count=len(extraction.tables),
            )
        except Exception as e:
            return FileResult(
                source=str(pdf_path),
                success=False,
                error=str(e),
            )
