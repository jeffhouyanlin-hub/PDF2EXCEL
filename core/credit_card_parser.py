"""RBC Mastercard (credit card) statement parser.

Parallel to BankStatementParser but for the 4-column credit-card layout:
    Transaction Date | Posting Date | Activity Description | Amount

The Mastercard PDF differs from the bank account statement in several ways:
- Different column schema (single Amount column vs Withdrawals/Deposits/Balance).
- Reference numbers sit on a separate line under each transaction (noise).
- A right-side "sidebar" on page 1 carries summary/contact/rate info that
  must be excluded by an x-coordinate cap.
- Summary fields: Previous Balance / New Balance / Payment Due Date /
  Credit Limit / Minimum Payment.
"""
from __future__ import annotations

import io as _io
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pdfplumber

from core.statement_parser import RowPosition


@dataclass
class CreditCardInfo:
    """Parsed content of a credit card statement."""

    bank_name: str = ""
    account_number: str = ""  # preserved as-is, e.g. "541590******6031"
    period_from: str = ""
    period_to: str = ""
    previous_balance: str = ""
    new_balance: str = ""
    minimum_payment: str = ""
    payment_due_date: str = ""
    credit_limit: str = ""
    transactions: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())


_Y_TOLERANCE = 4.0

# Header token → internal column name, as regex patterns.
# pdfplumber's extract_words may tokenize differently between PDFs:
#   "tight" layout (Jan 2025): each word separate — 'ACTIVITY', 'DESCRIPTION', 'AMOUNT'
#   "loose" layout (Mar 2025): space-joined tokens — 'ACTIVITY DESCRIPTION', 'AMOUNT ($)'
_HEADER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^TRANSACTION$", re.I), "txn_date"),
    (re.compile(r"^POSTING$", re.I), "post_date"),
    (re.compile(r"^(?:ACTIVITY(?:\s+DESCRIPTION)?|DESCRIPTION)$", re.I), "description"),
    (re.compile(r"^AMOUNT\b.*$", re.I), "amount"),
]


def _match_header(token: str) -> str | None:
    for pat, name in _HEADER_PATTERNS:
        if pat.match(token):
            return name
    return None

# Right-side sidebar on page 1 starts around x=385. Hard cap excludes it.
_SIDEBAR_RIGHT_CAP = 385.0

# Reference numbers: 11+ digit strings on their own line.
_REF_NUMBER_RE = re.compile(r"^\d{11,}$")

# Transaction date format: three-letter month + day, e.g. "DEC 05" or "JAN5".
_TXN_DATE_RE = re.compile(r"^[A-Z]{3}\s*\d{1,2}$", re.I)

# Foreign currency continuation line ("ForeignCurrency-USD48.77 Exchangerate-1.451302").
_FX_LINE_RE = re.compile(r"Foreign\s*Currency|Exchange\s*rate", re.I)

# Footer markers: once any of these appear, stop parsing the page.
# Used for both per-page truncation and final-DataFrame truncation.
_FOOTER_MARKERS = re.compile(
    r"RBC\s*ROYAL\s*BANK"
    r"|CREDIT\s*CARD\s*PAYMENT\s*CENTRE"
    r"|CREDIT\s*BALANCE\b"
    r"|AMOUNT\s*PAID\b"
    r"|P\.?O\.?\s*BOX",
    re.I,
)


class CreditCardParser:
    """Coordinate-based RBC Mastercard statement parser."""

    def parse(self, pdf_path: str | Path) -> CreditCardInfo:
        pdf_path = Path(pdf_path)
        info = CreditCardInfo()
        all_rows: list[dict] = []

        with pdfplumber.open(pdf_path) as pdf:
            full_text = "\n".join(p.extract_text(layout=True) or "" for p in pdf.pages)
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
        """Return (raw header tokens, merged row positions).

        Positions align with DataFrame rows from parse().
        """
        pdf_path = Path(pdf_path)
        headers: list[str] = []
        raw_entries: list[tuple[RowPosition, dict]] = []

        with pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                words = page.extract_words(keep_blank_chars=True, x_tolerance=1)

                col_bounds, header_y = self._detect_columns(words)
                if not col_bounds:
                    continue

                # Apply left/right x-cap to remove sidebar + left margin noise
                left_bound = min(x0 for x0, _ in col_bounds.values()) - 5
                words = [
                    w for w in words
                    if left_bound <= w["x0"] and w["x1"] <= _SIDEBAR_RIGHT_CAP
                ]

                if not headers:
                    header_words = [
                        w for w in words
                        if abs(w["top"] - header_y) <= 8.0
                        and _match_header(w["text"]) is not None
                    ]
                    header_words.sort(key=lambda w: w["x0"])
                    headers = [w["text"] for w in header_words]

                # Skip the "DATE / DATE" sub-header line right below main headers.
                tx_words = [w for w in words if w["top"] > header_y + 12]
                if not tx_words:
                    continue

                col_centers = self._compute_col_centers(col_bounds)
                lines = self._group_by_y(tx_words)
                truncated_on_this_page = False
                for line_words in lines:
                    if truncated_on_this_page:
                        break
                    row = self._classify_words(line_words, col_bounds, col_centers)
                    if not row:
                        continue
                    joined = " ".join(row.values())
                    if _FOOTER_MARKERS.search(joined):
                        truncated_on_this_page = True
                        continue
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
        """Search each (item, value) pair in PDF lines, return positions.

        Parallel to BankStatementParser.get_summary_positions.
        """
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
                        "text": text,
                        "y_top": y_top,
                        "y_bottom": y_bottom,
                        "page_idx": page_idx,
                        "page_height": page.height,
                        "is_summary": is_summary,
                    })

        results: list[RowPosition | None] = []
        for item in summary_items:
            value = str(item.get("value", "")).strip()
            if not value:
                results.append(None)
                continue
            pos = self._search_value_in_lines(
                value, str(item.get("item", "")), lines_index
            )
            results.append(pos)
        return results

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_summary(text: str, info: CreditCardInfo) -> None:
        # Two PDF variants exist: one with concatenated tokens (no spaces
        # between field name and value), one with spaces. Normalize to a
        # whitespace-stripped form for field-name matching.
        compact = re.sub(r"\s+", "", text)

        if re.search(r"Mastercard", text, re.I):
            info.bank_name = "RBC Mastercard"

        # Account number: "541590******6031" (6 digits + 6 asterisks + 4 digits)
        if m := re.search(r"(\d{6}\*{6}\d{4})", compact):
            info.account_number = m.group(1)

        # Period: "STATEMENT FROM FEB 06 TO MAR 5, 2025" — the "from" side may
        # omit the year if it falls in the same year as "to".
        if m := re.search(
            r"STATEMENTFROM(.+?)TO([A-Z]{3}\d{1,2},\d{4})",
            compact, re.I,
        ):
            info.period_from = _normalize_date(m.group(1))
            info.period_to = _normalize_date(m.group(2))

        # Strict money pattern: prevents greedy capture of adjacent reference
        # number digits. Matches $-?N[,NNN]*.NN only.
        money = r"-?\$?\d{1,3}(?:,\d{3})*\.\d{2}"
        num_plain = r"\d{1,3}(?:,\d{3})*\.\d{2}"

        if m := re.search(rf"PREVIOUSACCOUNTBALANCE({money})", compact, re.I):
            info.previous_balance = m.group(1).replace("$", "")

        if m := re.search(rf"TOTALACCOUNTBALANCE({money})", compact, re.I):
            info.new_balance = m.group(1).replace("$", "")
        elif m := re.search(rf"CREDITBALANCE({money})", compact, re.I):
            info.new_balance = m.group(1).replace("$", "")

        if m := re.search(rf"MINIMUMPAYMENT({money})", compact, re.I):
            info.minimum_payment = m.group(1).replace("$", "")

        if m := re.search(r"PAYMENTDUEDATE([A-Z]{3}\d{1,2},\d{4})", compact, re.I):
            info.payment_due_date = _normalize_date(m.group(1))

        if m := re.search(rf"CREDITLIMIT\$?({num_plain})", compact, re.I):
            info.credit_limit = m.group(1)

    def _extract_transaction_rows(self, words: list[dict]) -> list[dict]:
        col_bounds, header_y = self._detect_columns(words)
        if not col_bounds:
            return []

        left_bound = min(x0 for x0, _ in col_bounds.values()) - 5
        words = [
            w for w in words
            if left_bound <= w["x0"] and w["x1"] <= _SIDEBAR_RIGHT_CAP
        ]

        tx_words = [w for w in words if w["top"] > header_y + 12]
        if not tx_words:
            return []

        lines = self._group_by_y(tx_words)
        col_centers = self._compute_col_centers(col_bounds)

        rows: list[dict] = []
        for line_words in lines:
            row = self._classify_words(line_words, col_bounds, col_centers)
            if row is None:
                continue
            # Per-page footer truncation: stop at first footer marker.
            joined = " ".join(row.values())
            if _FOOTER_MARKERS.search(joined):
                break
            rows.append(row)
        return rows

    @staticmethod
    def _detect_columns(
        words: list[dict],
    ) -> tuple[dict[str, tuple[float, float]] | None, float]:
        """Detect column x-ranges using 'POSTING' as the header anchor."""
        posting_words = [
            w for w in words
            if w["text"] == "POSTING" and w["x0"] < _SIDEBAR_RIGHT_CAP
        ]
        if not posting_words:
            return None, 0.0

        # If multiple, the leftmost POSTING in the transaction area is the header.
        header_y = min(posting_words, key=lambda w: w["top"])["top"]

        # Allow a wider y-window to catch header tokens split across 2 visual
        # lines ("TRANSACTION POSTING" on line 1, "ACTIVITY DESCRIPTION" /
        # "AMOUNT ($)" on line 2 a few points below).
        header_words: dict[str, tuple[float, float, float]] = {}
        for w in words:
            if w["x1"] > _SIDEBAR_RIGHT_CAP:
                continue
            if abs(w["top"] - header_y) > 8.0:
                continue
            name = _match_header(w["text"])
            if name is None:
                continue
            if name in header_words:
                x0_old, x1_old, top_old = header_words[name]
                header_words[name] = (
                    min(x0_old, w["x0"]),
                    max(x1_old, w["x1"]),
                    top_old,
                )
            else:
                header_words[name] = (w["x0"], w["x1"], w["top"])

        # Require all four columns; otherwise this isn't a Mastercard page.
        required = {"txn_date", "post_date", "description", "amount"}
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
                # Last column (amount) extends to sidebar cap.
                bounds[name] = (x0, _SIDEBAR_RIGHT_CAP)

        # Extend txn_date column left to catch words starting slightly earlier.
        if "txn_date" in bounds:
            bounds["txn_date"] = (bounds["txn_date"][0] - 5, bounds["txn_date"][1])

        # Narrow post_date to its actual content width: only "MMM DD" fits, so
        # give description column everything past post_date header.x1 + buffer.
        # Without this, description text starting at ~x=129 (e.g. "SHOPPERS")
        # gets misclassified into post_date, whose midpoint boundary is ~152.
        if "post_date" in bounds and "description" in bounds:
            post_x0, _old_post_x1 = bounds["post_date"]
            post_header_x1 = header_words["post_date"][1]
            post_end = post_header_x1 + 6
            bounds["post_date"] = (post_x0, post_end)
            bounds["description"] = (post_end, bounds["description"][1])

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
            col_centers = {
                name: (x0 + x1) / 2 for name, (x0, x1) in col_bounds.items()
            }

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

        return {
            "Transaction Date": " ".join(cols.get("txn_date", [])),
            "Posting Date": " ".join(cols.get("post_date", [])),
            "Activity Description": " ".join(cols.get("description", [])),
            "Amount": " ".join(cols.get("amount", [])),
        }

    @staticmethod
    def _filter_noise(
        entries: list[tuple[RowPosition, dict]]
    ) -> list[tuple[RowPosition, dict]]:
        """Drop reference-number-only rows and fully empty rows."""
        out: list[tuple[RowPosition, dict]] = []
        for pos, row in entries:
            txn = row.get("Transaction Date", "").strip()
            post = row.get("Posting Date", "").strip()
            desc = row.get("Activity Description", "").strip()
            amt = row.get("Amount", "").strip()

            if not any([txn, post, desc, amt]):
                continue

            # Reference-number-only row: the only non-empty field is a
            # long digit string (may land in post_date or description).
            non_empty = [v for v in (txn, post, desc, amt) if v]
            if len(non_empty) == 1 and _REF_NUMBER_RE.match(non_empty[0]):
                continue

            # Boilerplate text in txn_date column.
            if txn and not _TXN_DATE_RE.match(txn):
                continue
            # Row without date/description can't be a real tx or continuation.
            if not txn and not desc:
                continue

            out.append((pos, row))
        return out

    @staticmethod
    def _merge_positions_by_content(
        entries: list[tuple[RowPosition, dict]],
    ) -> list[RowPosition]:
        """Merge continuation lines (FX line, desc overflow) into previous row."""
        merged: list[tuple[RowPosition, dict]] = []
        for pos, row in entries:
            has_txn_date = bool(row["Transaction Date"].strip())
            desc = row.get("Activity Description", "").strip()
            is_continuation = not has_txn_date and bool(desc)

            if is_continuation and merged:
                prev_pos, prev_row = merged[-1]
                prev_row["Activity Description"] += " " + desc
                if pos.page_index == prev_pos.page_index:
                    merged[-1] = (
                        RowPosition(
                            prev_pos.page_index,
                            prev_pos.y_top,
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
                line_text = line["text"]
                clean_line = line_text.replace(",", "").replace("$", "")
                if value in line_text or clean_value in clean_line:
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

    # ------------------------------------------------------------------
    # DataFrame build
    # ------------------------------------------------------------------

    def _build_dataframe(self, rows: list[dict]) -> pd.DataFrame:
        cols = ["Transaction Date", "Posting Date", "Activity Description", "Amount"]
        if not rows:
            return pd.DataFrame(columns=cols)

        # Truncate: per-page footer already stripped by _extract_transaction_rows,
        # but keep a safety net here for any footer-like row that slipped through.
        truncated: list[dict] = []
        for row in rows:
            joined = " ".join(row.values())
            if _FOOTER_MARKERS.search(joined):
                break
            truncated.append(row)

        # Drop reference-number-only rows, fully empty rows, and rows whose
        # Transaction Date doesn't look like a real date (boilerplate text
        # like "Time to Pay" that bleeds into the txn_date column).
        cleaned: list[dict] = []
        for row in truncated:
            txn = row.get("Transaction Date", "").strip()
            post = row.get("Posting Date", "").strip()
            desc = row.get("Activity Description", "").strip()
            amt = row.get("Amount", "").strip()
            if not any([txn, post, desc, amt]):
                continue
            non_empty = [v for v in (txn, post, desc, amt) if v]
            if len(non_empty) == 1 and _REF_NUMBER_RE.match(non_empty[0]):
                continue
            # Valid row: either txn_date matches date pattern, or txn_date is
            # empty (continuation line to be merged).
            if txn and not _TXN_DATE_RE.match(txn):
                continue
            # A no-date row with no description isn't a continuation either —
            # drop stray post_date-only or amount-only boilerplate.
            if not txn and not desc:
                continue
            cleaned.append(row)

        # Merge continuation lines (FX, desc overflow) — any no-date row with
        # description is treated as a continuation of the previous transaction.
        merged: list[dict] = []
        for row in cleaned:
            has_txn_date = bool(row["Transaction Date"].strip())
            desc = row.get("Activity Description", "").strip()

            if not has_txn_date and desc and merged:
                merged[-1]["Activity Description"] += " " + desc
                # Continuation may also carry an amount (rare); fill if empty.
                if row["Amount"].strip() and not merged[-1]["Amount"].strip():
                    merged[-1]["Amount"] = row["Amount"]
            else:
                merged.append(dict(row))

        # Normalize whitespace in descriptions.
        for row in merged:
            row["Activity Description"] = re.sub(
                r"  +", " ", row["Activity Description"]
            )

        df = pd.DataFrame(merged, columns=cols)
        for c in df.columns:
            df[c] = df[c].astype(str).str.strip()
        # Strip the dollar sign from Amount so downstream numeric parsing and
        # Excel output don't carry the currency symbol (keep the sign).
        df["Amount"] = df["Amount"].str.replace("$", "", regex=False)
        return df


def _normalize_date(text: str) -> str:
    """Insert spaces in concatenated date tokens: 'DEC06,2024' → 'DEC 06, 2024'."""
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    s = re.sub(r",(\d)", r", \1", s)
    s = re.sub(r"  +", " ", s).strip()
    return s
