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

        # Process each page independently: detect header per page,
        # extract transactions with page-specific column boundaries.
        summary_lines: list[str] = []
        all_raw_rows: list[dict] = []
        found_first_header = False

        for page_text in pages:
            page_lines = page_text.split("\n")
            header_idx, bounds = self._find_header(page_lines)

            if header_idx < 0 or not bounds:
                # No header on this page — if we haven't seen a header yet,
                # these are summary lines; otherwise skip the page
                if not found_first_header:
                    summary_lines.extend(page_lines)
                continue

            if not found_first_header:
                # Lines before the first header on this page are summary
                summary_lines.extend(page_lines[:header_idx])
                found_first_header = True

            # Parse transactions from this page using its own bounds
            page_rows = self._parse_page_transactions(page_lines, header_idx, bounds)
            all_raw_rows.extend(page_rows)

        summary = self._extract_summary(summary_lines)
        df = self._build_dataframe(all_raw_rows)
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
            # Check if this line contains the required column keywords
            found: dict[str, tuple[int, int]] = {}
            for kw in _COL_KEYWORDS:
                # Match keyword possibly followed by ($) or spaces
                pattern = re.compile(r"\b" + re.escape(kw) + r"(?:\s*\(\$\))?", re.I)
                m = pattern.search(line)
                if m:
                    found[kw] = (m.start(), m.end())

            # Need at least Date, Withdrawals, Deposits, Balance
            required = {"Date", "Withdrawals", "Deposits", "Balance"}
            if required.issubset(found.keys()):
                # Build adjusted start positions: for Balance, use midpoint
                # between Deposits keyword end and Balance keyword start.
                # Balance values are right-aligned and often extend leftward
                # past the header keyword position (e.g. "14,951.88" wider than "Balance").
                col_starts = {col: pos[0] for col, pos in found.items()}
                if "Balance" in found and "Deposits" in found:
                    dep_match_end = found["Deposits"][1]
                    bal_match_start = found["Balance"][0]
                    col_starts["Balance"] = (dep_match_end + bal_match_start) // 2

                # Compute boundaries: each column extends from its (adjusted)
                # start to the next column's start (sorted by position)
                sorted_cols = sorted(col_starts.items(), key=lambda kv: kv[1])
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

        # Post-process: fix text overflow from Description into numeric columns.
        # Layout text can have long descriptions that spill into Withdrawals/Deposits.
        # Patterns: "ZR 41.59" (text+amount), "742 300.00" (account-number+amount),
        # or pure text overflow like "SINC" (no amount at all).
        for num_col in ("Withdrawals", "Deposits"):
            val = result.get(num_col, "")
            if not val:
                continue
            # Pattern 1: overflow text/number followed by a currency amount (XX.XX)
            m = re.match(r"^(.+?)\s+([\d,]+\.\d{2})$", val)
            if m:
                overflow_text = m.group(1)
                numeric_part = m.group(2)
                result[num_col] = numeric_part
                result["Description"] = (result.get("Description", "") + " " + overflow_text).strip()
                continue
            # Pattern 2: pure text overflow (no valid currency amount at all)
            if not re.match(r"^[\d,.]+$", val):
                result["Description"] = (result.get("Description", "") + " " + val).strip()
                result[num_col] = ""

        return result

    def _parse_page_transactions(
        self,
        lines: list[str],
        header_idx: int,
        bounds: dict[str, tuple[int, int]],
    ) -> list[dict]:
        """Parse transaction rows from lines below the header on a single page."""
        rows: list[dict] = []
        for i in range(header_idx + 1, len(lines)):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                continue

            sliced = self._slice_line(line, bounds)

            # Stop at Closing Balance
            desc = sliced.get("Description", "")
            if re.search(r"Closing\s*Balance", desc, re.I):
                rows.append(sliced)
                break
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
    def _normalize_text(text: str) -> str:
        """Normalize layout-text artifacts: fix missing spaces in dates and descriptions.

        Layout text from pdfplumber often has concatenated words (e.g. '14Oct',
        'TermLoan', 'HydroBillPmt') due to tight character spacing in the PDF.
        """
        # Fix date: digit immediately followed by month name → insert space
        text = re.sub(r"(\d)(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", r"\1 \2", text)
        # Fix camelCase-like concatenation: lowercase→Uppercase
        text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
        # Fix digit→Uppercase (2+ chars): "9CGSX9" should stay, but "298TOYOTA" → "298 TOYOTA"
        text = re.sub(r"(\d)([A-Z]{2})", r"\1 \2", text)
        # Collapse multiple spaces
        text = re.sub(r"  +", " ", text)
        return text.strip()

    @staticmethod
    def _build_dataframe(rows: list[dict]) -> pd.DataFrame:
        """Build DataFrame with the same post-processing pipeline as Source A.

        Applies: noise filtering, continuation-line merging, date forward-fill,
        text normalization.
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

        # Normalize text artifacts from layout extraction
        for row in merged:
            row["Date"] = TextBasedExtractor._normalize_text(row["Date"])
            row["Description"] = TextBasedExtractor._normalize_text(row["Description"])
            row["Description"] = re.sub(r"  +", " ", row["Description"])
            row["Date"] = re.sub(r"  +", " ", row["Date"])

        df = pd.DataFrame(merged)
        for col in df.columns:
            df[col] = df[col].str.strip()
        return df
