from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pdfplumber


@dataclass
class ExtractionResult:
    tables: list[pd.DataFrame] = field(default_factory=list)
    page_count: int = 0
    source: str = ""


class PDFExtractor:
    """从 PDF 中提取表格数据。"""

    def extract(
        self,
        pdf_path: str | Path,
        pages: list[int] | None = None,
    ) -> ExtractionResult:
        """提取 PDF 中所有表格。

        Args:
            pdf_path: PDF 文件路径
            pages: 指定页码列表（从 1 开始），None 表示全部页
        """
        pdf_path = Path(pdf_path)
        tables: list[pd.DataFrame] = []

        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            target_pages = pdf.pages
            if pages:
                target_pages = [pdf.pages[p - 1] for p in pages if 1 <= p <= page_count]

            for page in target_pages:
                page_tables = page.extract_tables()
                for raw_table in page_tables:
                    if not raw_table:
                        continue
                    df = self._raw_to_dataframe(raw_table)
                    if not df.empty:
                        tables.append(df)

        return ExtractionResult(
            tables=tables,
            page_count=page_count,
            source=str(pdf_path),
        )

    @staticmethod
    def _raw_to_dataframe(raw_table: list[list[str | None]]) -> pd.DataFrame:
        """将 pdfplumber 原始表格转为 DataFrame，首行作为列名。"""
        if len(raw_table) < 2:
            if len(raw_table) == 1:
                return pd.DataFrame([raw_table[0]])
            return pd.DataFrame()

        header = [cell or f"col_{i}" for i, cell in enumerate(raw_table[0])]
        data = raw_table[1:]
        df = pd.DataFrame(data, columns=header)
        # 清理空白
        df = df.map(lambda x: x.strip() if isinstance(x, str) else x)
        return df
