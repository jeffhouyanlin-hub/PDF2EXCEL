"""End-to-end smoke test: parser → merged Excel → FieldMapper → RuleEngine."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from core.credit_card_parser import CreditCardParser
from core.fee_sort.field_mapper import FieldMapper
from core.fee_sort.rule_engine import RuleEngine

PDF_DIR = Path("/Users/vox/Desktop/RBC 2025/Mastercard 6031")
SAMPLE_PDF = PDF_DIR / "MasterCard Statement-6031 2025-01-06.pdf"


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="Sample PDF not available")
def test_parser_yields_expected_shape() -> None:
    info = CreditCardParser().parse(SAMPLE_PDF)
    assert info.bank_name == "RBC Mastercard"
    assert info.account_number == "541590******6031"
    assert info.period_from == "DEC 06, 2024"
    assert info.period_to == "JAN 06, 2025"
    assert info.previous_balance == "397.54"
    assert info.new_balance == "-825.72"
    assert len(info.transactions) >= 50  # 58 in practice
    assert list(info.transactions.columns) == [
        "Transaction Date", "Posting Date", "Activity Description", "Amount",
    ]
    # Every row must have a real transaction date (MMM DD).
    import re
    pat = re.compile(r"^[A-Z]{3}\s*\d{1,2}$", re.I)
    assert all(pat.match(d) for d in info.transactions["Transaction Date"])


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="Sample PDF not available")
def test_get_row_positions_aligns_with_parse() -> None:
    """get_row_positions() must produce positions parallel to parse() rows."""
    p = CreditCardParser()
    info = p.parse(SAMPLE_PDF)
    headers, positions = p.get_row_positions(SAMPLE_PDF)
    assert len(positions) == len(info.transactions)
    assert "POSTING" in headers
    # Positions must reference valid pages.
    for pos in positions:
        assert pos.page_index >= 0
        assert pos.y_top < pos.y_bottom


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="Sample PDF not available")
def test_field_mapper_sign_mapping(tmp_path: Path) -> None:
    """Positive Amount → withdrawals; negative → deposits (abs value)."""
    # Build a minimal credit-card merged Excel in memory
    cols = ["Transaction Date", "Posting Date", "Activity Description", "Amount", "Statement"]
    rows = [
        {"Transaction Date": "DEC 06, 2024", "Posting Date": "",
         "Activity Description": "Previous Balance", "Amount": "397.54",
         "Statement": "Jan2025"},
        {"Transaction Date": "DEC 05", "Posting Date": "DEC 09",
         "Activity Description": "SHOPPERS", "Amount": "$40.23",
         "Statement": "Jan2025"},
        {"Transaction Date": "DEC 10", "Posting Date": "DEC 10",
         "Activity Description": "PAYMENT THANK YOU", "Amount": "-$570.00",
         "Statement": "Jan2025"},
    ]
    df = pd.DataFrame(rows, columns=cols)
    out = tmp_path / "merged.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Transactions", index=False)

    row_mapping = [("Jan2025", -1), ("Jan2025", 0), ("Jan2025", 1)]
    std_rows = FieldMapper.map(out.read_bytes(), row_mapping)

    assert len(std_rows) == 3
    # Every CC row carries schema="credit_card".
    assert all(r.schema == "credit_card" for r in std_rows)
    # Previous Balance → row_type=opening_balance
    assert std_rows[0].row_type == "opening_balance"
    # Positive amount → signed withdrawals (positive)
    assert std_rows[1].row_type == "transaction"
    assert std_rows[1].withdrawals_float == 40.23
    assert "$" not in std_rows[1].withdrawals
    assert std_rows[1].withdrawals == "40.23"
    # Negative amount (refund) → signed withdrawals (negative)
    assert std_rows[2].withdrawals_float == -570.00
    assert "$" not in std_rows[2].withdrawals
    assert std_rows[2].withdrawals == "-570.00"
    # Deposits column unused for credit cards.
    assert std_rows[2].deposits == ""
    assert std_rows[2].deposits_float == 0.0


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="Sample PDF not available")
def test_rule_engine_classifies_cc_rows() -> None:
    """Basic sanity: rule engine runs on credit card rows without errors."""
    from core.fee_sort.field_mapper import StandardRow
    engine = RuleEngine()

    ob = StandardRow(
        idx=0, row_type="opening_balance", statement="Jan2025",
        date="DEC 06, 2024", description="Previous Balance",
        withdrawals="", deposits="", balance="",
    )
    assert engine.classify(ob).exclude

    tx = StandardRow(
        idx=1, row_type="transaction", statement="Jan2025",
        date="DEC 05", description="SHOPPERS DRUG MART",
        withdrawals="$19.03", deposits="", balance="",
        withdrawals_float=19.03,
    )
    result = engine.classify(tx)
    assert not result.exclude
    # SHOPPERS doesn't match any specific category → fallback P7
    assert result.rule_hit == "P7-fallback"
