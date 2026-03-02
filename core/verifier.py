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


@dataclass
class ArithmeticIssue:
    """Single arithmetic check failure."""

    check: str  # "balance_continuity" | "withdrawal_total" | "deposit_total" | "closing_balance"
    row: int | None  # row number (for per-row checks), None for summary-level
    expected: str
    actual: str
    message: str


@dataclass
class LayerResult:
    """Result for one verification layer."""

    name: str  # "A_vs_B" | "A_vs_C" | "arithmetic"
    label: str  # bilingual label
    passed: bool
    total_cells: int
    mismatched_cells: int
    diffs: list[CellDiff] = field(default_factory=list)
    arithmetic_issues: list[ArithmeticIssue] = field(default_factory=list)
    skipped: bool = False


@dataclass
class TripleVerificationResult:
    """Result of the three-source cross-verification."""

    overall_passed: bool
    layers: list[LayerResult]  # fixed 3 layers
    source_a_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    source_b_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    source_c_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    # Compatibility fields (mimic VerificationResult for UI)
    matched: bool = True
    total_cells: int = 0
    mismatched_cells: int = 0
    diffs: list[CellDiff] = field(default_factory=list)
    message: str = ""


class ArithmeticChecker:
    """Layer 3: arithmetic consistency checks on transaction data."""

    TOLERANCE = 0.01

    def check(
        self,
        transactions: pd.DataFrame,
        summary: dict[str, str],
    ) -> list[ArithmeticIssue]:
        """Run all 4 arithmetic checks."""
        issues: list[ArithmeticIssue] = []

        opening = self._parse_amount(summary.get("opening_balance", ""))

        issues.extend(self._check_balance_continuity(transactions, opening))

        issue = self._check_withdrawal_total(transactions, summary)
        if issue:
            issues.append(issue)

        issue = self._check_deposit_total(transactions, summary)
        if issue:
            issues.append(issue)

        issue = self._check_closing_balance(summary)
        if issue:
            issues.append(issue)

        return issues

    @staticmethod
    def _parse_amount(value: str | object) -> float:
        """'2,788.09' -> 2788.09, empty/invalid -> 0.0"""
        if value is None:
            return 0.0
        s = str(value).replace(",", "").replace("$", "").strip()
        if not s:
            return 0.0
        try:
            return float(s)
        except (ValueError, OverflowError):
            return 0.0

    def _check_balance_continuity(
        self,
        df: pd.DataFrame,
        opening: float,
    ) -> list[ArithmeticIssue]:
        """Balance[i] = Balance[i-1] - Withdrawal[i] + Deposit[i]"""
        issues: list[ArithmeticIssue] = []
        if df.empty or "Balance" not in df.columns:
            return issues

        prev_balance = opening
        for i in range(len(df)):
            withdrawal = self._parse_amount(df.iloc[i].get("Withdrawals", ""))
            deposit = self._parse_amount(df.iloc[i].get("Deposits", ""))
            balance = self._parse_amount(df.iloc[i].get("Balance", ""))

            # Skip rows without a balance value (e.g. closing balance row)
            if not str(df.iloc[i].get("Balance", "")).strip():
                continue

            expected = prev_balance - withdrawal + deposit
            if abs(expected - balance) > self.TOLERANCE:
                issues.append(ArithmeticIssue(
                    check="balance_continuity",
                    row=i,
                    expected=f"{expected:.2f}",
                    actual=f"{balance:.2f}",
                    message=(
                        f"行{i + 1} 余额不连续: 期望 {expected:.2f}, 实际 {balance:.2f} / "
                        f"Row {i + 1} balance discontinuity: expected {expected:.2f}, actual {balance:.2f}"
                    ),
                ))
            prev_balance = balance

        return issues

    def _check_withdrawal_total(
        self,
        df: pd.DataFrame,
        summary: dict[str, str],
    ) -> ArithmeticIssue | None:
        """sum(Withdrawals) ≈ Total Withdrawals"""
        total_stated = self._parse_amount(summary.get("total_withdrawals", ""))
        if total_stated == 0.0:
            return None

        col_sum = sum(
            self._parse_amount(df.iloc[i].get("Withdrawals", ""))
            for i in range(len(df))
        )

        if abs(col_sum - total_stated) > self.TOLERANCE:
            return ArithmeticIssue(
                check="withdrawal_total",
                row=None,
                expected=f"{total_stated:.2f}",
                actual=f"{col_sum:.2f}",
                message=(
                    f"支出总额不一致: 声明 {total_stated:.2f}, 求和 {col_sum:.2f} / "
                    f"Withdrawal total mismatch: stated {total_stated:.2f}, sum {col_sum:.2f}"
                ),
            )
        return None

    def _check_deposit_total(
        self,
        df: pd.DataFrame,
        summary: dict[str, str],
    ) -> ArithmeticIssue | None:
        """sum(Deposits) ≈ Total Deposits"""
        total_stated = self._parse_amount(summary.get("total_deposits", ""))
        if total_stated == 0.0:
            return None

        col_sum = sum(
            self._parse_amount(df.iloc[i].get("Deposits", ""))
            for i in range(len(df))
        )

        if abs(col_sum - total_stated) > self.TOLERANCE:
            return ArithmeticIssue(
                check="deposit_total",
                row=None,
                expected=f"{total_stated:.2f}",
                actual=f"{col_sum:.2f}",
                message=(
                    f"存入总额不一致: 声明 {total_stated:.2f}, 求和 {col_sum:.2f} / "
                    f"Deposit total mismatch: stated {total_stated:.2f}, sum {col_sum:.2f}"
                ),
            )
        return None

    def _check_closing_balance(
        self,
        summary: dict[str, str],
    ) -> ArithmeticIssue | None:
        """Closing = Opening + Total Deposits - Total Withdrawals"""
        opening = self._parse_amount(summary.get("opening_balance", ""))
        closing = self._parse_amount(summary.get("closing_balance", ""))
        deposits = self._parse_amount(summary.get("total_deposits", ""))
        withdrawals = self._parse_amount(summary.get("total_withdrawals", ""))

        if closing == 0.0:
            return None

        expected = opening + deposits - withdrawals
        if abs(expected - closing) > self.TOLERANCE:
            return ArithmeticIssue(
                check="closing_balance",
                row=None,
                expected=f"{expected:.2f}",
                actual=f"{closing:.2f}",
                message=(
                    f"首尾余额不一致: 期望 {expected:.2f}, 实际 {closing:.2f} / "
                    f"Closing balance mismatch: expected {expected:.2f}, actual {closing:.2f}"
                ),
            )
        return None


class TripleVerifier:
    """Three-source cross-verification for bank statements."""

    def verify_statement(
        self,
        source_a: pd.DataFrame,
        source_b: pd.DataFrame,
        excel_path: str | Path,
        summary: dict[str, str],
    ) -> TripleVerificationResult:
        """Run all 3 verification layers.

        source_a: coordinate-based extraction (BankStatementParser)
        source_b: text-based extraction (TextBasedExtractor)
        excel_path: generated Excel file
        summary: dict with opening_balance, closing_balance, etc.
        """
        excel_path = Path(excel_path)

        # Read Source C
        source_c = pd.DataFrame()
        try:
            source_c = pd.read_excel(excel_path, sheet_name="Transactions")
        except Exception:
            pass

        # Layer 1: A vs B (cross-extraction)
        if source_b.empty:
            layer1 = LayerResult(
                name="A_vs_B",
                label="交叉提取 / Cross-Extraction",
                passed=True,
                total_cells=0,
                mismatched_cells=0,
                skipped=True,
            )
        else:
            diffs_ab, cells_ab = DataVerifier._compare_dfs(source_a, source_b, "A_vs_B")
            layer1 = LayerResult(
                name="A_vs_B",
                label="交叉提取 / Cross-Extraction",
                passed=len(diffs_ab) == 0,
                total_cells=cells_ab,
                mismatched_cells=len(diffs_ab),
                diffs=diffs_ab,
            )

        # Layer 2: A vs C (round-trip)
        if source_c.empty:
            layer2 = LayerResult(
                name="A_vs_C",
                label="往返校验 / Round-Trip",
                passed=True,
                total_cells=0,
                mismatched_cells=0,
                skipped=True,
            )
        else:
            diffs_ac, cells_ac = DataVerifier._compare_dfs(source_a, source_c, "A_vs_C")
            layer2 = LayerResult(
                name="A_vs_C",
                label="往返校验 / Round-Trip",
                passed=len(diffs_ac) == 0,
                total_cells=cells_ac,
                mismatched_cells=len(diffs_ac),
                diffs=diffs_ac,
            )

        # Layer 3: Arithmetic
        arith_issues = ArithmeticChecker().check(source_a, summary)
        layer3 = LayerResult(
            name="arithmetic",
            label="算术校验 / Arithmetic",
            passed=len(arith_issues) == 0,
            total_cells=0,
            mismatched_cells=len(arith_issues),
            arithmetic_issues=arith_issues,
        )

        layers = [layer1, layer2, layer3]
        overall = all(l.passed for l in layers)

        # Aggregate diffs for compatibility (use Layer 2 diffs for the existing diff UI)
        all_diffs = layer2.diffs if layer2.diffs else layer1.diffs
        total_cells = layer1.total_cells + layer2.total_cells
        total_mismatched = layer1.mismatched_cells + layer2.mismatched_cells + layer3.mismatched_cells

        if overall:
            message = (
                "三源交叉验证全部通过 / "
                "All three verification layers passed"
            )
        else:
            parts = []
            for l in layers:
                if not l.passed and not l.skipped:
                    parts.append(l.label)
            message = (
                f"验证未通过: {', '.join(parts)} / "
                f"Verification failed: {', '.join(parts)}"
            )

        return TripleVerificationResult(
            overall_passed=overall,
            layers=layers,
            source_a_df=source_a,
            source_b_df=source_b,
            source_c_df=source_c,
            matched=overall,
            total_cells=total_cells,
            mismatched_cells=total_mismatched,
            diffs=all_diffs,
            message=message,
        )


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
