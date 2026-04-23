# PDF2EXCEL

PDF 表格提取 → Excel 批量转换工具。Streamlit Web UI。

## Quick Start

```bash
streamlit run app.py
python -m pytest tests/ -v --cov=core
```

## Architecture

```
core/extractor.py              PDFExtractor       pdfplumber 提取表格 → list[DataFrame]
core/converter.py              ExcelConverter     DataFrame → .xlsx (openpyxl, 自动列宽)
core/batch.py                  BatchProcessor     ThreadPoolExecutor 并行批处理
core/statement_parser.py       BankStatementParser 银行账单 PDF 坐标解析
core/credit_card_parser.py     CreditCardParser    信用卡账单 PDF 坐标解析 (4列schema)
core/verifier.py               DataVerifier       PDF↔Excel 逐单元格对比验证
core/fee_sort/rule_engine.py   RuleEngine         P0-P7 优先级规则分类引擎
core/fee_sort/field_mapper.py  FieldMapper        合并 Excel → StandardRow 映射
core/fee_sort/output_builder.py OutputBuilder     10 列分类结果 DataFrame → .xlsx
core/fee_sort/output_namer.py  OutputNamer        分类输出文件命名
app.py                         Streamlit UI       上传/预览/转换/下载/数据复核
fee_sort_page.py               Streamlit Page     费用分类页面 (data_editor)
```

## Key Types

- `ExtractionResult(tables, page_count, source)` — 单个 PDF 提取结果
- `FileResult(source, output, table_count, success, error)` — 单文件处理结果
- `BatchResult(results, total, succeeded, failed)` — 批量处理结果
- `CellDiff(sheet, row, column, expected, actual)` — 单元格差异
- `VerificationResult(matched, total_cells, mismatched_cells, diffs, message)` — 验证结果

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

## 主年份判断规则 (Majority Year)

| 规则 | 说明 |
|------|------|
| 数据来源 | 每个 PDF 的 `period_to` 日期（优先），fallback `period_from` |
| 投票排除 | 1月 PDF 不参与主年份投票（因为1月账单可能属于当年或次年） |
| 主年份 | 非1月 PDF 中出现次数最多的年份 |
| 全是1月 | 极端情况：所有 PDF 都是1月 → 取年份最小的 |

## 强制自检清单（每次修改后必须执行）

完成任何UI或数据展示相关的修改后，必须逐项确认：

### 数据显示
- [ ] 年份字段显示的是年份（4位数字，如2024），不是账户尾号或其他数字
- [ ] 金额字段必须右对齐（text-align: right）
- [ ] 日期格式统一为 YYYY-MM-DD

### UI功能
- [ ] 所有按钮都有对应的 onClick 处理函数，不是空函数
- [ ] 所有按钮点击后有可见的响应（状态变化/跳转/提示）

### 执行方式
**每次完成修改后必须运行 `bash verify.sh`，有报错必须修复后再提交。**

完成后必须：
1. 运行 `bash verify.sh` 确认无报错
2. 检查所有新增/修改的数字字段，确认含义与变量名一致
3. 检查所有金额列有右对齐（Excel: `right_align_numbers` / Streamlit: `NumberColumn`）
4. 检查所有按钮点击后有可见响应

## Dependencies

pdfplumber, openpyxl, pandas, streamlit (see pyproject.toml)
