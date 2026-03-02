from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class CellDiff:
    """单个单元格差异。"""

    sheet: str
    row: int  # 0-based 行号
    column: str  # 列名
    expected: str  # PDF 提取值
    actual: str  # Excel 值


@dataclass
class VerificationResult:
    """验证结果。"""

    matched: bool
    total_cells: int
    mismatched_cells: int
    diffs: list[CellDiff] = field(default_factory=list)
    message: str = ""


def _normalize(value: object) -> str:
    """将单元格值归一化为可比较字符串。

    处理: NaN → "", 数值归一化(round 2 + 去尾零), 字符串 strip + 压缩空白。
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    s = re.sub(r"\s+", " ", s)
    # 尝试数值归一化：round(2) + 去尾零，确保 1.0 == 1 == "1.00"
    try:
        num = float(s)
        if not (num != num):  # not NaN
            # round to 2 decimal places, normalize trailing zeros
            rounded = f"{round(num, 2):.2f}"
            return rounded.rstrip("0").rstrip(".")
    except (ValueError, OverflowError):
        pass
    return s


class DataVerifier:
    """对比 PDF 提取数据与 Excel 输出。"""

    def verify(
        self,
        extracted: list[pd.DataFrame],
        excel_path: str | Path,
    ) -> VerificationResult:
        """对比提取的 DataFrame 列表与 Excel 文件。

        每个 DataFrame 对应一个 sheet (Table_1, Table_2, ...)。
        """
        excel_path = Path(excel_path)
        all_diffs: list[CellDiff] = []
        total_cells = 0

        sheet_names = pd.ExcelFile(excel_path).sheet_names
        for i, expected_df in enumerate(extracted):
            if i < len(sheet_names):
                actual_df = pd.read_excel(excel_path, sheet_name=sheet_names[i])
                diffs, cells = self._compare_dfs(expected_df, actual_df, sheet_names[i])
                all_diffs.extend(diffs)
                total_cells += cells

        matched = len(all_diffs) == 0
        message = (
            "数据审核通过，EXCEL文件与PDF文件内容一致 / "
            "Verification passed, Excel matches PDF"
            if matched
            else f"发现 {len(all_diffs)} 处不一致 / "
            f"Found {len(all_diffs)} mismatch(es)"
        )
        return VerificationResult(
            matched=matched,
            total_cells=total_cells,
            mismatched_cells=len(all_diffs),
            diffs=all_diffs,
            message=message,
        )

    def verify_statement(
        self,
        transactions: pd.DataFrame,
        excel_path: str | Path,
    ) -> VerificationResult:
        """银行账单模式：只比对 Transactions sheet。"""
        excel_path = Path(excel_path)
        sheet_names = pd.ExcelFile(excel_path).sheet_names
        if "Transactions" not in sheet_names:
            return VerificationResult(
                matched=False,
                total_cells=0,
                mismatched_cells=0,
                message="Excel 中未找到 Transactions sheet / "
                "Transactions sheet not found in Excel",
            )

        actual_df = pd.read_excel(excel_path, sheet_name="Transactions")
        diffs, total_cells = self._compare_dfs(transactions, actual_df, "Transactions")
        matched = len(diffs) == 0
        message = (
            "数据审核通过，EXCEL文件与PDF文件内容一致 / "
            "Verification passed, Excel matches PDF"
            if matched
            else f"发现 {len(diffs)} 处不一致 / Found {len(diffs)} mismatch(es)"
        )
        return VerificationResult(
            matched=matched,
            total_cells=total_cells,
            mismatched_cells=len(diffs),
            diffs=diffs,
            message=message,
        )

    @staticmethod
    def _compare_dfs(
        expected: pd.DataFrame,
        actual: pd.DataFrame,
        sheet_name: str,
    ) -> tuple[list[CellDiff], int]:
        """逐单元格比较两个 DataFrame，返回 (差异列表, 总单元格数)。"""
        diffs: list[CellDiff] = []

        # 统一列名
        expected = expected.copy()
        actual = actual.copy()
        expected.columns = [str(c).strip() for c in expected.columns]
        actual.columns = [str(c).strip() for c in actual.columns]

        # 取行列交集
        max_rows = min(len(expected), len(actual))
        common_cols = [c for c in expected.columns if c in actual.columns]
        total_cells = max_rows * len(common_cols)

        for row_idx in range(max_rows):
            for col in common_cols:
                exp_val = _normalize(expected.iloc[row_idx][col])
                act_val = _normalize(actual.iloc[row_idx][col])
                if exp_val != act_val:
                    diffs.append(
                        CellDiff(
                            sheet=sheet_name,
                            row=row_idx,
                            column=col,
                            expected=exp_val,
                            actual=act_val,
                        )
                    )

        # 行数不一致也算差异
        if len(expected) != len(actual):
            extra_rows = abs(len(expected) - len(actual))
            extra_cols = len(common_cols) if common_cols else 1
            total_cells += extra_rows * extra_cols
            for row_idx in range(max_rows, max(len(expected), len(actual))):
                for col in common_cols or ["(row)"]:
                    exp_val = ""
                    act_val = ""
                    if row_idx < len(expected):
                        exp_val = _normalize(expected.iloc[row_idx].get(col, ""))
                    if row_idx < len(actual):
                        act_val = _normalize(actual.iloc[row_idx].get(col, ""))
                    diffs.append(
                        CellDiff(
                            sheet=sheet_name,
                            row=row_idx,
                            column=col,
                            expected=exp_val,
                            actual=act_val,
                        )
                    )

        return diffs, total_cells
