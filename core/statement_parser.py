from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pdfplumber


@dataclass
class StatementInfo:
    account_number: str = ""
    period_from: str = ""
    period_to: str = ""
    opening_balance: str = ""
    closing_balance: str = ""
    total_deposits: str = ""
    total_withdrawals: str = ""
    transactions: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())


_Y_TOLERANCE = 4.0  # words within this y-distance are on the same line

# Header keywords → column names (match with or without "($)" suffix)
_HEADER_KEYWORDS = {
    "Date": "date",
    "Description": "description",
    "Withdrawals": "withdrawals",
    "Deposits": "deposits",
    "Balance": "balance",
}


class BankStatementParser:
    """基于坐标的银行账单解析器（适用于 RBC 等无框线 PDF）。"""

    def parse(self, pdf_path: str | Path) -> StatementInfo:
        pdf_path = Path(pdf_path)
        info = StatementInfo()
        all_rows: list[dict] = []

        with pdfplumber.open(pdf_path) as pdf:
            # Extract summary (default x_tolerance for concatenated keyword matching)
            full_text = "\n".join(p.extract_text(layout=True) or "" for p in pdf.pages)
            self._parse_summary(full_text, info)

            # Extract transactions from each page using word coordinates
            for page in pdf.pages:
                words = page.extract_words(keep_blank_chars=True, x_tolerance=1)
                rows = self._extract_transaction_rows(words)
                all_rows.extend(rows)

        info.transactions = self._build_dataframe(all_rows)
        return info

    @staticmethod
    def _parse_summary(text: str, info: StatementInfo) -> None:
        if m := re.search(r"accountnumber:\s*([\d-]+)", text, re.I):
            info.account_number = m.group(1)
        if m := re.search(r"From\s*(.+?)\s*to\s*(.+?)$", text, re.M | re.I):
            info.period_from = _add_spaces(m.group(1).replace("\n", " ").strip())
            info.period_to = _add_spaces(m.group(2).strip())
        if m := re.search(r"openingbalance\s+\$?([\d,.\-]+)", text, re.I):
            info.opening_balance = m.group(1)
        if m := re.search(r"closingbalance.*?=\s*\$?([\d,.]+)", text, re.I):
            info.closing_balance = m.group(1)
        if m := re.search(r"depositsintoyouraccount\s+\+?\s*([\d,.]+)", text, re.I):
            info.total_deposits = m.group(1)
        if m := re.search(r"withdrawalsfromyouraccount\s+-?\s*([\d,.]+)", text, re.I):
            info.total_withdrawals = m.group(1)

    def _extract_transaction_rows(self, words: list[dict]) -> list[dict]:
        """从 word 坐标中提取交易行。"""
        # Filter out margin noise (x0 < 40)
        words = [w for w in words if w["x0"] >= 40]

        # Dynamically detect column boundaries from header row
        col_bounds, header_y = self._detect_columns(words)
        if not col_bounds:
            return []

        # Only words below the header
        tx_words = [w for w in words if w["top"] > header_y + 5]
        if not tx_words:
            return []

        lines = self._group_by_y(tx_words)

        rows: list[dict] = []
        for line_words in lines:
            row = self._classify_words(line_words, col_bounds)
            if row:
                rows.append(row)

        return rows

    @staticmethod
    def _detect_columns(words: list[dict]) -> tuple[dict[str, tuple[float, float]], float] | tuple[None, float]:
        """从页面 header 行动态检测列的 x 坐标范围。返回 (bounds, header_y)。"""
        # Find the header row: the y-line where "Withdrawals" appears
        withdrawal_words = [w for w in words if w["text"] == "Withdrawals"]
        if not withdrawal_words:
            return None, 0.0

        # Use the rightmost "Withdrawals" (the column header, not body text)
        withdrawal_word = max(withdrawal_words, key=lambda w: w["x0"])
        header_y = withdrawal_word["top"]

        # Collect all header keywords on the same y-line
        header_words = {}
        for w in words:
            if w["text"] in _HEADER_KEYWORDS and abs(w["top"] - header_y) <= _Y_TOLERANCE:
                col_name = _HEADER_KEYWORDS[w["text"]]
                if col_name not in header_words or abs(w["top"] - header_y) < abs(header_words[col_name][2] - header_y):
                    header_words[col_name] = (w["x0"], w["x1"], w["top"])

        if "withdrawals" not in header_words:
            return None, 0.0

        # Build column bounds: each column extends from its x0 to the next column's x0
        ordered = sorted(
            [(name, (x0, x1)) for name, (x0, x1, _) in header_words.items()],
            key=lambda kv: kv[1][0],
        )
        bounds = {}
        for i, (name, (x0, x1)) in enumerate(ordered):
            if i + 1 < len(ordered):
                next_x0 = ordered[i + 1][1][0]
                bounds[name] = (x0, next_x0 - 1)
            else:
                bounds[name] = (x0, x1 + 50)  # last column extends right

        # Extend date column left to catch dates
        if "date" in bounds:
            bounds["date"] = (bounds["date"][0] - 5, bounds["date"][1])

        return bounds, header_y

    @staticmethod
    def _group_by_y(words: list[dict]) -> list[list[dict]]:
        """按 y 坐标分组，相近的归为同一行。"""
        if not words:
            return []
        sorted_words = sorted(words, key=lambda w: (w["top"], w["x0"]))
        lines: list[list[dict]] = []
        current_line: list[dict] = [sorted_words[0]]
        current_y = sorted_words[0]["top"]

        for w in sorted_words[1:]:
            if abs(w["top"] - current_y) <= _Y_TOLERANCE:
                current_line.append(w)
            else:
                lines.append(current_line)
                current_line = [w]
                current_y = w["top"]
        lines.append(current_line)
        return lines

    @staticmethod
    def _classify_words(line_words: list[dict], col_bounds: dict) -> dict | None:
        """根据动态列边界将 word 分到对应列。"""
        cols = {k: [] for k in col_bounds}
        for w in line_words:
            x_center = (w["x0"] + w["x1"]) / 2
            for col_name, (x_min, x_max) in col_bounds.items():
                if x_min <= w["x0"] <= x_max or x_min <= x_center <= x_max:
                    cols[col_name].append(w["text"])
                    break

        has_content = any(cols[c] for c in cols)
        if not has_content:
            return None

        return {
            "Date": " ".join(cols.get("date", [])),
            "Description": " ".join(cols.get("description", [])),
            "Withdrawals": " ".join(cols.get("withdrawals", [])),
            "Deposits": " ".join(cols.get("deposits", [])),
            "Balance": " ".join(cols.get("balance", [])),
        }

    @staticmethod
    def _build_dataframe(rows: list[dict]) -> pd.DataFrame:
        """构建 DataFrame，合并多行描述，前推日期。"""
        if not rows:
            return pd.DataFrame(columns=["Date", "Description", "Withdrawals", "Deposits", "Balance"])

        # Truncate at "Closing Balance" — everything after is footer
        truncated: list[dict] = []
        for row in rows:
            truncated.append(row)
            desc = row.get("Description", "").strip()
            if re.search(r"Closing\s*Balance", desc, re.I):
                break

        # Filter noise rows (page numbers, barcodes)
        noise_patterns = re.compile(
            r"(\d+\s*of\s*\d+|RBPDA|\d{7}-\d{3}_|0050750|^\d{4}$)",
            re.I,
        )
        cleaned: list[dict] = []
        for row in truncated:
            all_text = " ".join(v for v in row.values() if v).strip()
            if noise_patterns.search(all_text):
                continue
            desc = row.get("Description", "").strip()
            has_amounts = row["Withdrawals"].strip() or row["Deposits"].strip() or row["Balance"].strip()
            if not desc and not has_amounts:
                continue
            cleaned.append(row)

        # Merge continuation lines into previous row
        # Rule: a no-date line is a continuation ONLY if the previous row has no amounts yet
        merged: list[dict] = []
        for row in cleaned:
            has_date = bool(row["Date"].strip())
            has_desc = bool(row["Description"].strip())
            prev_has_amounts = bool(
                merged
                and (merged[-1]["Withdrawals"].strip() or merged[-1]["Deposits"].strip())
            )

            if not has_date and has_desc and merged and not prev_has_amounts:
                # Continuation: previous row had no amounts, append desc + fill amounts
                merged[-1]["Description"] += " " + row["Description"].strip()
                for col in ("Withdrawals", "Deposits", "Balance"):
                    if row[col].strip() and not merged[-1][col].strip():
                        merged[-1][col] = row[col]
            else:
                merged.append(row)

        # Forward-fill dates
        last_date = ""
        for row in merged:
            if row["Date"].strip():
                last_date = row["Date"].strip()
            else:
                row["Date"] = last_date

        # Clean up descriptions (spaces already correct with x_tolerance=1)
        for row in merged:
            # Normalize multiple spaces
            row["Description"] = re.sub(r"  +", " ", row["Description"])
            row["Date"] = re.sub(r"  +", " ", row["Date"])

        df = pd.DataFrame(merged)
        for col in df.columns:
            df[col] = df[col].str.strip()
        return df


def _add_spaces(text: str) -> str:
    """在驼峰式粘连的文字中插入空格。例如 'OnlineBanking' → 'Online Banking'"""
    # lowercase→Uppercase: "OnlineBanking" → "Online Banking"
    result = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    # digit→Uppercase word: "0732ROGERS" → "0732 ROGERS" (only before 2+ uppercase)
    result = re.sub(r"(\d)([A-Z]{2})", r"\1 \2", result)
    # Long uppercase word→digit: "ROGERS11" → "ROGERS 11" (only after 3+ uppercase)
    result = re.sub(r"([A-Z]{3,})(\d)", r"\1 \2", result)
    # Collapse multiple spaces
    result = re.sub(r"  +", " ", result)
    return result
