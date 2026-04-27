"""Build 10-column Fee Sort output DataFrame and Excel bytes."""

from __future__ import annotations

import io
import re
from typing import Sequence

import pandas as pd
from openpyxl.utils import get_column_letter

from core.fee_sort.field_mapper import StandardRow
from core.fee_sort.rule_engine import ClassificationResult

# Fixed output columns
OUTPUT_COLUMNS = [
    "序号",
    "Bank_Acc",
    "Year",
    "Date",
    "Description",
    "Withdrawals",
    "Exclude",
    "Category",
    "Detail",
    "Rule_Hit",
    "Need_Review",
]


class OutputBuilder:
    """Assemble classification results into the 10-column output."""

    @staticmethod
    def build(
        rows: Sequence[StandardRow],
        classifications: Sequence[ClassificationResult],
        bank_acc: str,
        year: str,
    ) -> pd.DataFrame:
        """Build output DataFrame, skipping separator rows.

        Args:
            rows: StandardRow list from FieldMapper.
            classifications: ClassificationResult list (parallel to rows).
            bank_acc: e.g. "Barclays_1234".
            year: e.g. "2023".
        Returns:
            DataFrame with OUTPUT_COLUMNS.
        """
        records: list[dict] = []
        seq = 0
        for row, cls in zip(rows, classifications):
            if row.row_type == "separator":
                continue
            if row.row_type == "opening_balance":
                continue
            if re.search(r"(Opening|Closing|Previous)\s*Balance", row.description, re.I):
                continue

            if row.schema == "credit_card":
                # Keep the signed amount so refunds/payments display with "-"
                # and the numeric column can sum correctly. Rule engine decides
                # whether the row is excluded; OutputBuilder does not drop them.
                withdrawals_display = row.withdrawals
            else:
                # Bank statements: deposit-only rows are income, not a fee.
                if row.deposits_float > 0 and row.withdrawals_float == 0:
                    continue
                withdrawals_display = row.withdrawals if row.withdrawals_float > 0 else ""

            seq += 1
            records.append({
                "序号": seq,
                "Bank_Acc": bank_acc,
                "Year": year,
                "Date": row.date,
                "Description": row.description,
                "Withdrawals": withdrawals_display,
                "Exclude": "Y" if cls.exclude else "",
                "Category": cls.category,
                "Detail": cls.detail,
                "Rule_Hit": cls.rule_hit,
                "Need_Review": "Y" if cls.need_review else "",
            })
        return pd.DataFrame(records, columns=OUTPUT_COLUMNS)

    @staticmethod
    def to_excel_bytes(df: pd.DataFrame) -> bytes:
        """Convert DataFrame to .xlsx bytes with auto column widths."""
        from core.converter import right_align_numbers

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="FeeSort", index=False)
            ws = writer.sheets["FeeSort"]
            for col_idx, col_name in enumerate(df.columns, 1):
                max_len = len(str(col_name))
                for val in df.iloc[:, col_idx - 1]:
                    max_len = max(max_len, len(str(val)) if val else 0)
                ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 1, 50)
            right_align_numbers(ws)
        return buf.getvalue()
