"""Source B: layout-text based PDF extractor for cross-verification.

Uses pdfplumber's extract_text(layout=True) and character-position slicing,
providing an independent extraction path from the coordinate-based Source A
(BankStatementParser).
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pdfplumber


@dataclass
class TextExtractionResult:
    """Result from text-based extraction."""
    transactions: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    summary: dict[str, str] = field(default_factory=dict)


# Column keywords expected in the header row
_COL_KEYWORDS = ["Date", "Description", "Withdrawals", "Deposits", "Balance"]

# Noise patterns (same as BankStatementParser._build_dataframe)
_NOISE_RE = re.compile(
    r"(\d+\s*of\s*\d+|RBPDA|\d{7}-\d{3}_|0050750|^\d{4}$)", re.I,
)


class TextBasedExtractor:
    """Layout-text based PDF extractor for cross-verification (Source B)."""

    def extract(self, pdf_source: str | Path | bytes) -> TextExtractionResult:
        """Extract transactions and summary from a bank statement PDF."""
        pages = self._get_page_texts(pdf_source)
        if not pages:
            return TextExtractionResult()

        # Combine all pages for summary extraction
        full_text = "\n".join(pages)

        # Find header across all pages
        all_lines: list[str] = []
        page_offsets: list[int] = []  # line offset for each page
        for page_text in pages:
            page_offsets.append(len(all_lines))
            all_lines.extend(page_text.split("\n"))

        header_idx, bounds = self._find_header(all_lines)
        if header_idx < 0 or not bounds:
            return TextExtractionResult(summary=self._extract_summary(all_lines))

        raw_rows = self._parse_transactions(all_lines, header_idx, bounds)
        summary = self._extract_summary(all_lines[:header_idx])

        df = self._build_dataframe(raw_rows)
        return TextExtractionResult(transactions=df, summary=summary)

    @staticmethod
    def _get_page_texts(pdf_source: str | Path | bytes) -> list[str]:
        """Extract layout text from each page."""
        if isinstance(pdf_source, bytes):
            pdf_input = io.BytesIO(pdf_source)
        else:
            pdf_input = Path(pdf_source)

        texts: list[str] = []
        with pdfplumber.open(pdf_input) as pdf:
            for page in pdf.pages:
                text = page.extract_text(layout=True) or ""
                texts.append(text)
        return texts

    @staticmethod
    def _find_header(lines: list[str]) -> tuple[int, dict[str, tuple[int, int]]]:
        """Find the header row and compute column character boundaries.

        Returns (line_index, {column_name: (start_char, end_char)}).
        Returns (-1, {}) if no header found.
        """
        for i, line in enumerate(lines):
            # Check if this line contains all column keywords (with optional ($) suffix)
            found = {}
            for kw in _COL_KEYWORDS:
                # Match keyword possibly followed by ($) or spaces
                pattern = re.compile(r"\b" + re.escape(kw) + r"(?:\s*\(\$\))?", re.I)
                m = pattern.search(line)
                if m:
                    found[kw] = m.start()

            # Need at least Date, Withdrawals, Deposits, Balance
            required = {"Date", "Withdrawals", "Deposits", "Balance"}
            if required.issubset(found.keys()):
                # Compute boundaries: each column extends from its start to
                # the next column's start (sorted by position)
                sorted_cols = sorted(found.items(), key=lambda kv: kv[1])
                bounds: dict[str, tuple[int, int]] = {}
                for j, (col, start) in enumerate(sorted_cols):
                    if j + 1 < len(sorted_cols):
                        end = sorted_cols[j + 1][1]
                    else:
                        end = len(line) + 50  # last column extends to end
                    bounds[col] = (start, end)
                return i, bounds

        return -1, {}

    @staticmethod
    def _slice_line(line: str, bounds: dict[str, tuple[int, int]]) -> dict[str, str]:
        """Slice a text line by character positions into column values."""
        result: dict[str, str] = {}
        for col, (start, end) in bounds.items():
            val = line[start:end] if start < len(line) else ""
            result[col] = val.strip()
        return result

    def _parse_transactions(
        self,
        lines: list[str],
        header_idx: int,
        bounds: dict[str, tuple[int, int]],
    ) -> list[dict]:
        """Parse transaction rows from lines below the header."""
        rows: list[dict] = []
        # Start from line after header
        for i in range(header_idx + 1, len(lines)):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                continue

            sliced = self._slice_line(line, bounds)

            # Stop at Closing Balance
            if re.search(r"Closing\s*Balance", sliced.get("Description", ""), re.I):
                rows.append(sliced)
                break

            # Also check if the keyword spans columns
            if re.search(r"Closing\s*Balance", stripped, re.I):
                rows.append(sliced)
                break

            rows.append(sliced)

        return rows

    def _extract_summary(self, lines: list[str]) -> dict[str, str]:
        """Extract summary fields from text above the header.

        Reuses the same regex patterns as BankStatementParser._parse_summary.
        """
        text = "\n".join(lines)
        # Also build a space-stripped version for concatenated keyword matching
        text_no_spaces = re.sub(r"\s+", "", text)
        summary: dict[str, str] = {}

        if m := re.search(r"Opening\s*Balance\s*[=:]?\s*\$?([\d,.\-]+)", text, re.I):
            summary["opening_balance"] = m.group(1)
        elif m := re.search(r"openingbalance\s*\$?([\d,.\-]+)", text_no_spaces, re.I):
            summary["opening_balance"] = m.group(1)

        if m := re.search(r"Closing\s*Balance\s*=?\s*\$?([\d,.]+)", text, re.I):
            summary["closing_balance"] = m.group(1)
        elif m := re.search(r"closingbalance.*?=?\s*\$?([\d,.]+)", text_no_spaces, re.I):
            summary["closing_balance"] = m.group(1)

        if m := re.search(r"Deposits?\s+into\s+your\s+account\s+\+?\s*([\d,.]+)", text, re.I):
            summary["total_deposits"] = m.group(1)
        elif m := re.search(r"depositsintoyouraccount\+?([\d,.]+)", text_no_spaces, re.I):
            summary["total_deposits"] = m.group(1)

        if m := re.search(r"Withdrawals?\s+from\s+your\s+account\s+-?\s*([\d,.]+)", text, re.I):
            summary["total_withdrawals"] = m.group(1)
        elif m := re.search(r"withdrawalsfromyouraccount-?([\d,.]+)", text_no_spaces, re.I):
            summary["total_withdrawals"] = m.group(1)

        return summary

    @staticmethod
    def _build_dataframe(rows: list[dict]) -> pd.DataFrame:
        """Build DataFrame with the same post-processing pipeline as Source A.

        Applies: noise filtering, continuation-line merging, date forward-fill.
        This MUST stay in sync with BankStatementParser._build_dataframe.
        """
        if not rows:
            return pd.DataFrame(
                columns=["Date", "Description", "Withdrawals", "Deposits", "Balance"],
            )

        # Normalize keys to standard column names
        standardized: list[dict] = []
        for row in rows:
            std = {
                "Date": row.get("Date", ""),
                "Description": row.get("Description", ""),
                "Withdrawals": row.get("Withdrawals", ""),
                "Deposits": row.get("Deposits", ""),
                "Balance": row.get("Balance", ""),
            }
            standardized.append(std)

        # Truncate at Closing Balance
        truncated: list[dict] = []
        for row in standardized:
            truncated.append(row)
            if re.search(r"Closing\s*Balance", row.get("Description", ""), re.I):
                break

        # Filter noise
        cleaned: list[dict] = []
        for row in truncated:
            all_text = " ".join(v for v in row.values() if v).strip()
            if _NOISE_RE.search(all_text):
                continue
            desc = row.get("Description", "").strip()
            has_amounts = (
                row["Withdrawals"].strip()
                or row["Deposits"].strip()
                or row["Balance"].strip()
            )
            if not desc and not has_amounts:
                continue
            cleaned.append(row)

        # Merge continuation lines (no-date lines with description)
        merged: list[dict] = []
        for row in cleaned:
            has_date = bool(row["Date"].strip())
            has_desc = bool(row["Description"].strip())
            prev_has_amounts = bool(
                merged
                and (merged[-1]["Withdrawals"].strip() or merged[-1]["Deposits"].strip())
            )

            if not has_date and has_desc and merged and not prev_has_amounts:
                merged[-1]["Description"] += " " + row["Description"].strip()
                for col in ("Withdrawals", "Deposits", "Balance"):
                    if row[col].strip() and not merged[-1][col].strip():
                        merged[-1][col] = row[col]
            else:
                merged.append(dict(row))

        # Forward-fill dates
        last_date = ""
        for row in merged:
            if row["Date"].strip():
                last_date = row["Date"].strip()
            else:
                row["Date"] = last_date

        # Clean up whitespace
        for row in merged:
            row["Description"] = re.sub(r"  +", " ", row["Description"])
            row["Date"] = re.sub(r"  +", " ", row["Date"])

        df = pd.DataFrame(merged)
        for col in df.columns:
            df[col] = df[col].str.strip()
        return df
