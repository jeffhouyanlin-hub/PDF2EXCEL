# PDF2EXCEL

PDF 表格提取 → Excel 批量转换工具。Streamlit Web UI。

## Quick Start

```bash
streamlit run app.py
python -m pytest tests/ -v --cov=core
```

## Architecture

```
core/extractor.py   PDFExtractor     pdfplumber 提取表格 → list[DataFrame]
core/converter.py   ExcelConverter   DataFrame → .xlsx (openpyxl, 自动列宽)
core/batch.py       BatchProcessor   ThreadPoolExecutor 并行批处理
app.py              Streamlit UI     上传/预览/转换/下载
```

## Key Types

- `ExtractionResult(tables, page_count, source)` — 单个 PDF 提取结果
- `FileResult(source, output, table_count, success, error)` — 单文件处理结果
- `BatchResult(results, total, succeeded, failed)` — 批量处理结果

## Data Flow

`PDF upload → PDFExtractor.extract() → ExtractionResult → ExcelConverter.convert() → .xlsx`

批量: `list[Path] → BatchProcessor.process() → BatchResult → ZIP download`

## Conventions

- Python ≥ 3.10, type hints with `from __future__ import annotations`
- Tests in `tests/`, mirror `core/` structure (`test_extractor.py`, `test_converter.py`, `test_batch.py`)
- Use `pytest` + `pytest-cov`, target 80%+ coverage
- Dataclasses for data containers, no NamedTuple
- `Path` over `str` for file paths in internal APIs
- Commit messages: `<type>: <description>` (feat, fix, refactor, test, chore)

## Dependencies

pdfplumber, openpyxl, pandas, streamlit (see pyproject.toml)
