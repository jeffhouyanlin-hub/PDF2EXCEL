"""Central registry of bank/credit-card statement parsers.

Add a new parser in 3 steps:
  1. Implement a class with methods `parse`, `get_row_positions`,
     `get_summary_positions` that return the same types as the RBC parsers
     (`StatementInfo` for `schema="bank"`, `CreditCardInfo` for
     `schema="credit_card"`).
  2. Expose a module-level `can_parse(pdf_path) -> bool` quick probe (first-page
     text keyword match).
  3. Add a `ParserEntry` below in `PARSERS`, with a stable `key`, UI `label`,
     `schema`, and auto-detect `priority` (lower = tried first).

Downstream code (merge, T777, fee-sort, verification UI) branches only on
`schema`, so a new parser for the same schema inherits all features for free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.credit_card_parser import CreditCardParser
from core.parsers.cibc_bank import CIBCBankParser
from core.statement_parser import BankStatementParser


@dataclass(frozen=True)
class ParserEntry:
    key: str               # stable internal id, e.g. "rbc_cc"
    label: str             # UI display text
    schema: str            # "bank" | "credit_card"
    priority: int          # auto-detect order (lower = tried first)
    parse: Callable[[str | Path], Any]
    get_row_positions: Callable[[str | Path], tuple[list[str], list]]
    get_summary_positions: Callable[..., list]
    can_parse: Callable[[str | Path], bool]
    # Whether core.text_extractor.TextBasedExtractor yields a usable Source-B
    # extraction for this parser's PDFs. Only RBC bank statements are currently
    # supported; other banks skip Layer 1 cross-verification.
    has_source_b: bool = False
    # Whether arithmetic checks (balance continuity etc.) should run.
    run_arithmetic: bool = True


# Module-level singletons to avoid re-instantiating on every call.
_rbc_bank = BankStatementParser()
_rbc_cc = CreditCardParser()
_cibc_bank = CIBCBankParser()


def _rbc_bank_can_parse(pdf_path: str | Path) -> bool:
    """RBC bank statement probe — 'Royal Bank' + account number pattern."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            text = pdf.pages[0].extract_text(layout=True) or ""
        if "Royal Bank" not in text and "RBC" not in text:
            return False
        if "Mastercard" in text or "Cash Back" in text:
            return False  # it's a credit card, not bank account
        # Bank accounts have a "Withdrawals" column header
        return "Withdrawals" in text
    except Exception:
        return False


def _rbc_cc_can_parse(pdf_path: str | Path) -> bool:
    """RBC Mastercard probe — 'Mastercard' + POSTING column anchor."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            text = pdf.pages[0].extract_text() or ""
        return (
            ("Mastercard" in text or "RBC" in text)
            and "POSTING" in text.upper()
        )
    except Exception:
        return False


# ----------------------------------------------------------------------
# Registry — add new parsers here.
# ----------------------------------------------------------------------
PARSERS: list[ParserEntry] = [
    ParserEntry(
        key="rbc_cc",
        label="RBC 信用卡账单 / RBC Credit Card",
        schema="credit_card",
        priority=10,  # try CC before bank (POSTING anchor is very specific)
        parse=_rbc_cc.parse,
        get_row_positions=_rbc_cc.get_row_positions,
        get_summary_positions=_rbc_cc.get_summary_positions,
        can_parse=_rbc_cc_can_parse,
    ),
    ParserEntry(
        key="cibc_bank",
        label="CIBC 银行账单 / CIBC Bank",
        schema="bank",
        priority=20,
        parse=_cibc_bank.parse,
        get_row_positions=_cibc_bank.get_row_positions,
        get_summary_positions=_cibc_bank.get_summary_positions,
        can_parse=CIBCBankParser.can_parse,
    ),
    ParserEntry(
        key="rbc_bank",
        label="RBC 银行账单 / RBC Bank",
        schema="bank",
        priority=30,
        parse=_rbc_bank.parse,
        get_row_positions=_rbc_bank.get_row_positions,
        get_summary_positions=_rbc_bank.get_summary_positions,
        can_parse=_rbc_bank_can_parse,
        has_source_b=True,   # TextBasedExtractor supports RBC layout
        run_arithmetic=True,
    ),
]


# ----------------------------------------------------------------------
# Lookup helpers
# ----------------------------------------------------------------------

def get_parser(key: str) -> ParserEntry | None:
    """Look up a parser by its stable key."""
    for p in PARSERS:
        if p.key == key:
            return p
    return None


def list_parsers(schema: str | None = None) -> list[ParserEntry]:
    """Return registry entries, optionally filtered by schema."""
    if schema is None:
        return list(PARSERS)
    return [p for p in PARSERS if p.schema == schema]


def auto_detect(pdf_path: str | Path) -> ParserEntry | None:
    """Try each parser's `can_parse` in priority order; first hit wins."""
    for entry in sorted(PARSERS, key=lambda e: e.priority):
        try:
            if entry.can_parse(pdf_path):
                return entry
        except Exception:
            continue
    return None


def label_to_key(label: str) -> str | None:
    for p in PARSERS:
        if p.label == label:
            return p.key
    return None
