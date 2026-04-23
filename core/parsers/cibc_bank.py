"""CIBC personal account statement parser.

Same downstream schema as the RBC bank parser (`StatementInfo` with
Date/Description/Withdrawals/Deposits/Balance columns) so merge/T777/fee-sort
logic stays unchanged. Only the PDF-level extraction differs.

Key layout characteristics:
- Header row contains tokens `Date`, `Description`, `Withdrawals ($)`,
  `Deposits ($)`, `Balance ($)` (the "($)" is glued to the label by
  pdfplumber's x_tolerance=1 word grouping).
- Transaction rows: date on the left (e.g. "Jan 6"), description sometimes
  spans 2–3 lines, amounts right-aligned in each column.
- Opening / Closing balance rows appear inside the Transaction table.
"""
from __future__ import annotations

import io as _io
import re
from pathlib import Path

import pandas as pd
import pdfplumber

from core.statement_parser import RowPosition, StatementInfo


_Y_TOLERANCE = 4.0

# Map header token (as produced by pdfplumber with x_tolerance=1) → col name.
_HEADER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^Date$", re.I), "date"),
    (re.compile(r"^Description$", re.I), "description"),
    (re.compile(r"^Withdrawals(?:\s*\(\$\))?$", re.I), "withdrawals"),
    (re.compile(r"^Deposits(?:\s*\(\$\))?$", re.I), "deposits"),
    (re.compile(r"^Balance(?:\s*\(\$\))?$", re.I), "balance"),
]


def _match_header(token: str) -> str | None:
    for pat, name in _HEADER_PATTERNS:
        if pat.match(token):
            return name
    return None


# CIBC page-break artifacts — these land in the transaction area when the
# table spans multiple pages. Filter before building the DataFrame so they
# don't poison the balance-continuity arithmetic check.
_JUNK_PATTERNS = re.compile(
    r"continued\s*on\s*next\s*page"
    r"|Page\s*\d+\s*of\s*\d+"
    r"|\b\d+E\s*PER-?\d+/\d+"        # CIBC form code "10774E PER-2018/09"
    r"|Balance\s*forward",
    re.I,
)

# A real CIBC transaction date looks like "Jan 6" / "Feb 28".
_TXN_DATE_RE = re.compile(r"^[A-Z][a-z]{2}\s*\d{1,2}$", re.I)


class CIBCBankParser:
    """Coordinate-based CIBC personal account statement parser."""

    def parse(self, pdf_path: str | Path) -> StatementInfo:
        pdf_path = Path(pdf_path)
        info = StatementInfo()
        all_rows: list[dict] = []

        with pdfplumber.open(pdf_path) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            self._parse_summary(full_text, info)

            for page in pdf.pages:
                words = page.extract_words(keep_blank_chars=True, x_tolerance=1)
                rows = self._extract_transaction_rows(words)
                all_rows.extend(rows)

        info.transactions = self._build_dataframe(all_rows)
        return info

    def get_row_positions(
        self, pdf_path: str | Path
    ) -> tuple[list[str], list[RowPosition]]:
        pdf_path = Path(pdf_path)
        headers: list[str] = []
        raw_entries: list[tuple[RowPosition, dict]] = []

        with pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                words = page.extract_words(keep_blank_chars=True, x_tolerance=1)
                col_bounds, header_y = self._detect_columns(words)
                if not col_bounds:
                    continue

                if not headers:
                    header_words = [
                        w for w in words
                        if abs(w["top"] - header_y) <= _Y_TOLERANCE
                        and _match_header(w["text"]) is not None
                    ]
                    header_words.sort(key=lambda w: w["x0"])
                    headers = [w["text"] for w in header_words]

                tx_words = [w for w in words if w["top"] > header_y + 5]
                if not tx_words:
                    continue

                col_centers = self._compute_col_centers(col_bounds)
                lines = self._group_by_y(tx_words)
                for line_words in lines:
                    row = self._classify_words(line_words, col_bounds, col_centers)
                    if not row:
                        continue
                    # Stop at Closing balance footer
                    if re.search(r"Closing\s*Balance", row.get("Description", ""), re.I):
                        break
                    y_top = min(w["top"] for w in line_words)
                    y_bottom = max(w["bottom"] for w in line_words)
                    pos = RowPosition(page_idx, y_top, y_bottom, page.height)
                    raw_entries.append((pos, row))

        cleaned = self._filter_noise(raw_entries)
        merged = self._merge_positions_by_content(cleaned)
        return headers, merged

    def get_summary_positions(
        self,
        pdf_source: str | Path | bytes,
        summary_items: list[dict],
    ) -> list[RowPosition | None]:
        """Search summary field values in PDF lines (same API as RBC parsers)."""
        if isinstance(pdf_source, bytes):
            pdf_input = _io.BytesIO(pdf_source)
        else:
            pdf_input = Path(pdf_source)

        lines_index: list[dict] = []
        tx_header_page = -1
        tx_header_y = 0.0

        with pdfplumber.open(pdf_input) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                words = page.extract_words(keep_blank_chars=True, x_tolerance=1)
                if not words:
                    continue

                if tx_header_page < 0:
                    _, hy = self._detect_columns(words)
                    if hy > 0:
                        tx_header_page = page_idx
                        tx_header_y = hy

                grouped = self._group_by_y(words)
                for line_words in grouped:
                    text = " ".join(w["text"] for w in line_words)
                    y_top = min(w["top"] for w in line_words)
                    y_bottom = max(w["bottom"] for w in line_words)
                    is_summary = (
                        tx_header_page < 0 and page_idx == 0
                    ) or (
                        tx_header_page >= 0 and (
                            page_idx < tx_header_page
                            or (page_idx == tx_header_page and y_top < tx_header_y)
                        )
                    )
                    lines_index.append({
                        "text": text, "y_top": y_top, "y_bottom": y_bottom,
                        "page_idx": page_idx, "page_height": page.height,
                        "is_summary": is_summary,
                    })

        results: list[RowPosition | None] = []
        for item in summary_items:
            value = str(item.get("value", "")).strip()
            if not value:
                results.append(None)
                continue
            pos = self._search_value_in_lines(
                value, str(item.get("item", "")), lines_index,
            )
            results.append(pos)
        return results

    # ------------------------------------------------------------------
    # Detection / parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def can_parse(pdf_path: str | Path) -> bool:
        """Quick probe — does this look like a CIBC statement?"""
        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = pdf.pages[0].extract_text() or ""
            return "CIBC" in text and "Account Statement" in text
        except Exception:
            return False

    @staticmethod
    def _parse_summary(text: str, info: StatementInfo) -> None:
        info.bank_name = "CIBC"

        # "Account number\n73-40885"
        if m := re.search(r"Account\s*number[:\s]*(\d+-\d+)", text, re.I):
            info.account_number = m.group(1)

        # "For Jan 1 to Jan 31, 2025"
        if m := re.search(
            r"For\s+([A-Z][a-z]+\s+\d{1,2})\s+to\s+([A-Z][a-z]+\s+\d{1,2}(?:,\s*\d{4})?)",
            text, re.I,
        ):
            info.period_from = m.group(1).strip()
            info.period_to = m.group(2).strip()
            # Back-fill year on period_from if missing
            if info.period_from and not re.search(r"\d{4}", info.period_from):
                yr_match = re.search(r"\d{4}", info.period_to)
                if yr_match:
                    info.period_from = f"{info.period_from}, {yr_match.group(0)}"

        # Skip past the date ("Jan 1, 2025") to the $ sign before the balance.
        if m := re.search(r"Opening\s*balance.*?\$([\d,.\-]+)", text, re.I):
            info.opening_balance = m.group(1)
        if m := re.search(r"Closing\s*balance.*?\$([\d,.\-]+)", text, re.I):
            info.closing_balance = m.group(1)

        if m := re.search(r"Withdrawals\s*-\s*([\d,.\-]+)", text, re.I):
            info.total_withdrawals = m.group(1)

        if m := re.search(r"Deposits\s*\+\s*([\d,.\-]+)", text, re.I):
            info.total_deposits = m.group(1)

    def _extract_transaction_rows(self, words: list[dict]) -> list[dict]:
        col_bounds, header_y = self._detect_columns(words)
        if not col_bounds:
            return []

        tx_words = [w for w in words if w["top"] > header_y + 5]
        if not tx_words:
            return []

        lines = self._group_by_y(tx_words)
        col_centers = self._compute_col_centers(col_bounds)

        rows: list[dict] = []
        for line_words in lines:
            row = self._classify_words(line_words, col_bounds, col_centers)
            if not row:
                continue
            if re.search(r"Closing\s*Balance", row.get("Description", ""), re.I):
                rows.append(row)
                break
            rows.append(row)
        return rows

    @staticmethod
    def _detect_columns(
        words: list[dict],
    ) -> tuple[dict[str, tuple[float, float]] | None, float]:
        """Find the Transaction table header; 'Withdrawals ($)' is the anchor."""
        # Find header row where both 'Date' and 'Description' appear on same y
        # (distinct from summary-section mentions of Withdrawals/Deposits).
        date_words = [w for w in words if w["text"].strip() == "Date"]
        if not date_words:
            return None, 0.0
        header_y = 0.0
        for dw in date_words:
            y = dw["top"]
            # Check if 'Description' is on same line
            if any(
                w["text"].strip() == "Description" and abs(w["top"] - y) <= _Y_TOLERANCE
                for w in words
            ):
                header_y = y
                break
        if header_y == 0.0:
            return None, 0.0

        header_words: dict[str, tuple[float, float, float]] = {}
        for w in words:
            if abs(w["top"] - header_y) > _Y_TOLERANCE:
                continue
            name = _match_header(w["text"].strip())
            if name is None:
                continue
            if name in header_words:
                x0_old, x1_old, top_old = header_words[name]
                header_words[name] = (
                    min(x0_old, w["x0"]), max(x1_old, w["x1"]), top_old,
                )
            else:
                header_words[name] = (w["x0"], w["x1"], w["top"])

        required = {"date", "description", "withdrawals", "deposits", "balance"}
        if not required.issubset(header_words.keys()):
            return None, 0.0

        ordered = sorted(
            [(name, (x0, x1)) for name, (x0, x1, _) in header_words.items()],
            key=lambda kv: kv[1][0],
        )
        bounds: dict[str, tuple[float, float]] = {}
        for i, (name, (x0, x1)) in enumerate(ordered):
            if i + 1 < len(ordered):
                next_x0 = ordered[i + 1][1][0]
                mid = (x1 + next_x0) / 2
                bounds[name] = (x0, mid)
            else:
                bounds[name] = (x0, x1 + 60)  # last column extends right

        # Widen date column slightly (header is narrow but content like "Jan 31" is short)
        if "date" in bounds:
            bounds["date"] = (bounds["date"][0] - 3, bounds["date"][1])
        # Description column: widen left edge if necessary so multi-line desc text hits it
        # (description text typically starts right at the header x0).

        return bounds, header_y

    @staticmethod
    def _compute_col_centers(
        col_bounds: dict[str, tuple[float, float]]
    ) -> dict[str, float]:
        return {name: (x0 + x1) / 2 for name, (x0, x1) in col_bounds.items()}

    @staticmethod
    def _group_by_y(words: list[dict]) -> list[list[dict]]:
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
    def _classify_words(
        line_words: list[dict],
        col_bounds: dict,
        col_centers: dict[str, float] | None = None,
    ) -> dict | None:
        if col_centers is None:
            col_centers = {name: (x0 + x1) / 2 for name, (x0, x1) in col_bounds.items()}

        cols: dict[str, list[str]] = {k: [] for k in col_bounds}
        for w in line_words:
            x_center = (w["x0"] + w["x1"]) / 2
            candidates = [
                name for name, (x_min, x_max) in col_bounds.items()
                if x_min <= x_center <= x_max
            ]
            if len(candidates) == 1:
                cols[candidates[0]].append(w["text"])
            elif len(candidates) > 1:
                best = min(candidates, key=lambda c: abs(x_center - col_centers[c]))
                cols[best].append(w["text"])
            else:
                best = min(col_centers, key=lambda c: abs(x_center - col_centers[c]))
                cols[best].append(w["text"])

        if not any(cols[c] for c in cols):
            return None

        def _clean(val: str) -> str:
            return val.replace("$", "").strip()

        return {
            "Date": " ".join(cols.get("date", [])),
            "Description": " ".join(cols.get("description", [])),
            "Withdrawals": _clean(" ".join(cols.get("withdrawals", []))),
            "Deposits": _clean(" ".join(cols.get("deposits", []))),
            "Balance": _clean(" ".join(cols.get("balance", []))),
        }

    @staticmethod
    def _filter_noise(
        entries: list[tuple[RowPosition, dict]]
    ) -> list[tuple[RowPosition, dict]]:
        out: list[tuple[RowPosition, dict]] = []
        for pos, row in entries:
            vals = [row.get(c, "").strip() for c in
                    ("Date", "Description", "Withdrawals", "Deposits", "Balance")]
            if not any(vals):
                continue
            out.append((pos, row))
        return out

    @staticmethod
    def _merge_positions_by_content(
        entries: list[tuple[RowPosition, dict]],
    ) -> list[RowPosition]:
        merged: list[tuple[RowPosition, dict]] = []
        for pos, row in entries:
            has_date = bool(row["Date"].strip())
            has_desc = bool(row["Description"].strip())
            prev_has_amounts = bool(
                merged and (merged[-1][1]["Withdrawals"].strip()
                            or merged[-1][1]["Deposits"].strip())
            )
            if not has_date and has_desc and merged and not prev_has_amounts:
                prev_pos, prev_row = merged[-1]
                prev_row["Description"] += " " + row["Description"].strip()
                for col in ("Withdrawals", "Deposits", "Balance"):
                    if row[col].strip() and not prev_row[col].strip():
                        prev_row[col] = row[col]
                if pos.page_index == prev_pos.page_index:
                    merged[-1] = (
                        RowPosition(
                            prev_pos.page_index, prev_pos.y_top,
                            max(prev_pos.y_bottom, pos.y_bottom),
                            prev_pos.page_height,
                        ),
                        prev_row,
                    )
            else:
                merged.append((pos, dict(row)))
        return [p for p, _ in merged]

    @staticmethod
    def _search_value_in_lines(
        value: str, item_name: str, lines_index: list[dict]
    ) -> RowPosition | None:
        clean_value = value.replace(",", "").replace("$", "").strip()
        for prefer_summary in (True, False):
            candidates = []
            for line in lines_index:
                if prefer_summary and not line["is_summary"]:
                    continue
                if value in line["text"] or clean_value in line["text"].replace(",", "").replace("$", ""):
                    candidates.append(line)
            if not candidates:
                continue
            if len(candidates) == 1:
                c = candidates[0]
                return RowPosition(c["page_idx"], c["y_top"], c["y_bottom"], c["page_height"])
            item_parts = [p.lower() for p in item_name.split() if len(p) > 2]
            for c in candidates:
                if any(part in c["text"].lower() for part in item_parts):
                    return RowPosition(c["page_idx"], c["y_top"], c["y_bottom"], c["page_height"])
            c = candidates[0]
            return RowPosition(c["page_idx"], c["y_top"], c["y_bottom"], c["page_height"])
        return None

    def _build_dataframe(self, rows: list[dict]) -> pd.DataFrame:
        cols = ["Date", "Description", "Withdrawals", "Deposits", "Balance"]
        if not rows:
            return pd.DataFrame(columns=cols)

        # Truncate at Closing balance
        truncated: list[dict] = []
        for row in rows:
            desc = row.get("Description", "").strip()
            if re.search(r"Closing\s*Balance", desc, re.I):
                break
            truncated.append(row)

        # Noise filter: drop empty rows and page-break artifacts
        # (e.g. "Page 1 of 2", "Balance forward", form ID footer).
        cleaned: list[dict] = []
        for row in truncated:
            if not any(row[c].strip() for c in cols):
                continue
            joined = " ".join(row[c] for c in cols)
            if _JUNK_PATTERNS.search(joined):
                continue
            # Drop rows whose Date cell contains non-date junk (e.g. form ID).
            # Real txn rows either have a valid "Mmm DD" or are continuation
            # rows with empty Date.
            d = row["Date"].strip()
            if d and not _TXN_DATE_RE.match(d):
                continue
            cleaned.append(row)

        # Merge continuation lines (multi-line description under a transaction).
        # Rule: a no-date row with ONLY description (no amounts) is a
        # continuation. A no-date row WITH amounts is a separate transaction
        # on the same date (CIBC prints only the first date in a same-day
        # block) — forward-fill the date later.
        merged: list[dict] = []
        for row in cleaned:
            has_date = bool(row["Date"].strip())
            has_desc = bool(row["Description"].strip())
            has_amounts = bool(row["Withdrawals"].strip() or row["Deposits"].strip())
            if not has_date and has_desc and not has_amounts and merged:
                merged[-1]["Description"] += " " + row["Description"].strip()
            else:
                merged.append(dict(row))

        # Forward-fill dates (CIBC rarely needs it but be defensive)
        last_date = ""
        for row in merged:
            if row["Date"].strip():
                last_date = row["Date"].strip()
            else:
                row["Date"] = last_date

        # Skip "Opening balance" row (it's a header row inside the table, not a transaction)
        merged = [
            r for r in merged
            if not re.search(r"Opening\s*Balance", r.get("Description", ""), re.I)
        ]

        for row in merged:
            row["Description"] = re.sub(r"\s+", " ", row["Description"]).strip()

        df = pd.DataFrame(merged, columns=cols)
        for c in df.columns:
            df[c] = df[c].astype(str).str.strip()
        return df
