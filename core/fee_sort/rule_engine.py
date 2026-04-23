"""Priority-based rule engine for transaction classification (P0–P7)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from core.fee_sort.field_mapper import StandardRow


class Category(str, Enum):
    DIRECTOR_SALARY = "Director Salary"
    HMRC = "HMRC"
    BANK_CHARGE = "Bank Charge"
    OFFICE = "Office"
    INSURANCE = "Insurance"
    PROFESSIONAL_FEE = "Professional Fee"
    OTHER_EXPENSE = "Other Expense"


@dataclass
class ClassificationResult:
    exclude: bool = False
    category: str = ""
    detail: str = ""
    rule_hit: str = ""
    need_review: bool = False


# ---------------------------------------------------------------------------
# Compiled patterns (case-insensitive)
# ---------------------------------------------------------------------------

_PAT_SALARY = re.compile(
    r"\b(salary|payroll|director\s*pay|wages)\b", re.I,
)
_PAT_HMRC = re.compile(
    r"\b(HMRC|VAT|PAYE|corporation\s*tax|NATIONAL\s*INSURANCE)\b", re.I,
)
_PAT_BANK_CHARGE = re.compile(
    r"\b(bank\s*(fee|charge)|service\s*charge|interest\s*charge|monthly\s*fee)\b", re.I,
)
_PAT_OFFICE = re.compile(
    r"\b(rent|utilit(?:y|ies)|electric(?:ity)?|water|office\s*suppl(?:y|ies)|telephone|broadband)\b",
    re.I,
)
_PAT_INSURANCE = re.compile(
    r"\b(insurance|premium|AVIVA)\b", re.I,
)
_PAT_PROFESSIONAL = re.compile(
    r"\b(accountant|solicitor|legal\s*fee|audit|consulting)\b", re.I,
)

# P0 own-account transfer patterns
_PAT_OWN_TRANSFER = re.compile(
    r"\b(transfer\s*(to|from)\s*self|inter[- ]?account|own\s*account)\b", re.I,
)


class RuleEngine:
    """Classify a StandardRow using priority rules P0–P7.  First match wins."""

    def __init__(
        self,
        account_numbers: list[str] | None = None,
        aviva_threshold: float = 500.0,
    ) -> None:
        self._account_numbers = account_numbers or []
        self._aviva_threshold = aviva_threshold

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def classify(self, row: StandardRow) -> ClassificationResult:
        """Return classification for *row*.  Iterates P0 → P7; first match wins."""
        for checker in (
            self._check_p0,
            self._check_p1,
            self._check_p2,
            self._check_p3,
            self._check_p4,
            self._check_p5,
            self._check_p6,
        ):
            result = checker(row)
            if result is not None:
                return result
        return self._check_p7(row)

    # ------------------------------------------------------------------
    # Priority checkers
    # ------------------------------------------------------------------

    def _check_p0(self, row: StandardRow) -> ClassificationResult | None:
        """P0 Exclude: Opening Balance, separator, own-account transfer."""
        desc = row.description.strip()

        # Separator rows (empty description)
        if row.row_type == "separator":
            return ClassificationResult(exclude=True, rule_hit="P0-separator")

        # Opening Balance
        if row.row_type == "opening_balance":
            return ClassificationResult(
                exclude=True, detail="Opening Balance", rule_hit="P0-opening_balance",
            )

        # Own-account transfer (description pattern)
        if _PAT_OWN_TRANSFER.search(desc):
            return ClassificationResult(
                exclude=True, detail="Own-account transfer", rule_hit="P0-own_transfer",
            )

        # Own-account transfer (account number appears in description)
        for acct in self._account_numbers:
            if acct and acct in desc:
                return ClassificationResult(
                    exclude=True,
                    detail=f"Own-account transfer ({acct})",
                    rule_hit="P0-own_transfer_acct",
                )

        return None

    def _check_p1(self, row: StandardRow) -> ClassificationResult | None:
        """P1 Director Salary."""
        m = _PAT_SALARY.search(row.description)
        if m:
            return ClassificationResult(
                category=Category.DIRECTOR_SALARY.value,
                detail=m.group(0),
                rule_hit="P1-salary",
            )
        return None

    def _check_p2(self, row: StandardRow) -> ClassificationResult | None:
        """P2 HMRC."""
        m = _PAT_HMRC.search(row.description)
        if m:
            return ClassificationResult(
                category=Category.HMRC.value,
                detail=m.group(0),
                rule_hit="P2-HMRC",
            )
        return None

    def _check_p3(self, row: StandardRow) -> ClassificationResult | None:
        """P3 Bank Charge."""
        m = _PAT_BANK_CHARGE.search(row.description)
        if m:
            return ClassificationResult(
                category=Category.BANK_CHARGE.value,
                detail=m.group(0),
                rule_hit="P3-bank_charge",
            )
        return None

    def _check_p4(self, row: StandardRow) -> ClassificationResult | None:
        """P4 Office."""
        m = _PAT_OFFICE.search(row.description)
        if m:
            return ClassificationResult(
                category=Category.OFFICE.value,
                detail=m.group(0),
                rule_hit="P4-office",
            )
        return None

    def _check_p5(self, row: StandardRow) -> ClassificationResult | None:
        """P5 Insurance.  AVIVA with amount > threshold → Need_Review."""
        m = _PAT_INSURANCE.search(row.description)
        if m:
            need_review = False
            if re.search(r"\bAVIVA\b", row.description, re.I):
                if row.withdrawals_float > self._aviva_threshold:
                    need_review = True
            return ClassificationResult(
                category=Category.INSURANCE.value,
                detail=m.group(0),
                rule_hit="P5-insurance",
                need_review=need_review,
            )
        return None

    def _check_p6(self, row: StandardRow) -> ClassificationResult | None:
        """P6 Professional Fee."""
        m = _PAT_PROFESSIONAL.search(row.description)
        if m:
            return ClassificationResult(
                category=Category.PROFESSIONAL_FEE.value,
                detail=m.group(0),
                rule_hit="P6-professional",
            )
        return None

    def _check_p7(self, row: StandardRow) -> ClassificationResult:
        """P7 Fallback — Other Expense + Need_Review."""
        return ClassificationResult(
            category=Category.OTHER_EXPENSE.value,
            detail="",
            rule_hit="P7-fallback",
            need_review=True,
        )
