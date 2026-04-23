"""T777 annotator smoke tests."""
from __future__ import annotations

import io

import pandas as pd
import pytest

from core.fee_sort.field_mapper import StandardRow
from core.fee_sort.rule_engine_cc import CCCategory
from core.t777 import (
    SCREEN_DEDUCTIBLE,
    SCREEN_NEED_REVIEW,
    SCREEN_NOT_DEDUCTIBLE,
    T777Annotator,
    build_t777_excel,
)


def _row(desc: str, amount: float, row_type: str = "transaction") -> StandardRow:
    return StandardRow(
        idx=0, row_type=row_type, statement="stmt", date="",
        description=desc, withdrawals=f"{amount:.2f}", deposits="",
        balance="", withdrawals_float=amount, schema="credit_card",
    )


class TestAnnotator:
    def setup_method(self):
        self.a = T777Annotator()

    def test_parking_deductible_9281(self):
        r = _row("IMPARK00011650U DELTA BC", 17.00)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_DEDUCTIBLE
        assert ann.field_en == "Parking"
        assert ann.line_no == 9281

    def test_travel_hotel_needs_review_note(self):
        r = _row("MARRIOTT HOTELS VANCOUVER BC", 320.00)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_DEDUCTIBLE
        assert ann.field_en == "Travelling expenses"
        assert ann.line_no == 9200
        assert "出差" in ann.review_note

    def test_restaurants_over_50_need_review(self):
        """≥$50 restaurants stay as 'need review' (conservative default)."""
        r = _row("SOME GENERIC RESTAURANT VANCOUVER BC", 75.00)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_NEED_REVIEW

    def test_restaurants_under_50_is_personal(self):
        """Post-2026-04-22 rule: Restaurant <$50 defaults to personal."""
        r = _row("MCDONALD'S #26031 WEST VANCOUVEBC", 12.00)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_NOT_DEDUCTIBLE

    def test_client_meals_deductible_8523(self):
        r = _row("AL-HADBAH MEDITERRANEA VANCOUVER BC", 180.00)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_DEDUCTIBLE
        assert ann.field_en == "Meals and entertainment"
        assert ann.line_no == 8523
        assert "50%" in ann.review_note

    def test_groceries_not_deductible(self):
        r = _row("SAFEWAY #4909 WEST VANCOUVEBC", 20.00)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_NOT_DEDUCTIBLE
        assert ann.reason  # must explain why

    def test_fine_not_deductible(self):
        r = _row("CITY OF VAN-BYLAW FINE VANCOUVER BC", 75.00)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_NOT_DEDUCTIBLE
        assert "罚款" in ann.reason

    def test_motor_vehicle_has_no_line_no(self):
        r = _row("SHELL C01217 WEST VANCOUVEBC", 60.00)
        ann = self.a.annotate(r)
        assert ann.field_en == "Motor vehicle expenses"
        assert ann.line_no is None
        assert ann.screening == SCREEN_NEED_REVIEW

    def test_excluded_card_payment(self):
        r = _row("PAYMENT - THANK YOU / PAIEMENT - MERCI", -570.00)
        ann = self.a.annotate(r)
        # Excluded rows get a neutral annotation, NOT a deductible one.
        assert ann.screening == ""
        assert "非支出行" in ann.reason

    def test_refund_follows_merchant_category(self):
        """Shoppers refund should inherit Personal Care → 疑似不可抵扣."""
        r = _row("SHOPPERS DRUG MART #22 VANCOUVER BC", -19.03)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_NOT_DEDUCTIBLE
        assert "退款" in ann.reason
        assert "Personal Care" in ann.reason

    def test_subscriptions_now_not_deductible(self):
        r = _row("GOOGLE *TIDAL 650-253-0000 NS", 12.99)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_NOT_DEDUCTIBLE
        assert ann.field_en == ""
        assert ann.line_no is None

    def test_professional_training_is_deductible(self):
        r = _row("CANADIAN SECURITIES IN TORONTO ON", 450.00)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_DEDUCTIBLE
        assert ann.field_en == "Other expenses"
        assert ann.line_no == 9270

    def test_restaurant_small_personal_has_flip_hint(self):
        r = _row("MCDONALD'S #26031 WEST VANCOUVEBC", 12.00)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_NOT_DEDUCTIBLE
        assert "Client Meals" in ann.review_note  # hint to flip if business meal

    def test_bank_deposit_row_excluded(self):
        """Bank deposit rows are income, not expenses — drop from T777."""
        r = StandardRow(
            idx=0, row_type="transaction", statement="stmt", date="",
            description="SALARY TRANSFER FROM EMPLOYER",
            withdrawals="", deposits="2500.00", balance="",
            withdrawals_float=0.0, deposits_float=2500.00,
            schema="bank",
        )
        ann = self.a.annotate(r)
        assert ann.screening == ""  # excluded
        assert "非支出行" in ann.reason

    def test_toyota_finance_is_motor_vehicle_lease(self):
        r = _row("TERM LOAN TOYOTA FINANCE TORONTO ON", 380.50)
        ann = self.a.annotate(r)
        assert ann.field_en == "Motor vehicle expenses"
        assert ann.line_no is None
        assert "Lease" in ann.reason

    def test_icbc_is_motor_vehicle_insurance(self):
        r = _row("AUTO INSURANCE ICBC VICTORIA BC", 208.66)
        ann = self.a.annotate(r)
        assert ann.field_en == "Motor vehicle expenses"
        assert "Insurance" in ann.reason
        assert "RAV4" in ann.review_note  # user's per-vehicle hint

    def test_utilities_now_not_deductible(self):
        """Post-2026-04-22 rule: no home office → utilities not deductible."""
        r = _row("BC HYDRO VANCOUVER BC", 120.00)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_NOT_DEDUCTIBLE
        assert "居家办公" in ann.reason

    def test_bank_expense_uses_cc_merchant_rules(self):
        """Bank expense rows run through CC merchant rules for T777 categorization."""
        r = StandardRow(
            idx=0, row_type="transaction", statement="stmt", date="",
            description="IMPARK00011650U DELTA BC",
            withdrawals="17.00", deposits="", balance="",
            withdrawals_float=17.00, deposits_float=0.0,
            schema="bank",
        )
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_DEDUCTIBLE
        assert ann.field_en == "Parking"
        assert ann.line_no == 9281

    def test_refund_of_parking_still_deductible(self):
        """Refund of a parking charge inherits Parking's deductible mapping."""
        r = _row("IMPARK00011650U DELTA BC", -17.00)
        ann = self.a.annotate(r)
        assert ann.screening == SCREEN_DEDUCTIBLE
        assert ann.field_en == "Parking"
        assert ann.line_no == 9281
        assert "退款" in ann.review_note


class TestBuildExcel:
    def test_excel_has_t777_columns_and_summary_sheet(self, tmp_path):
        # Build a minimal merged Excel
        cols = ["Transaction Date", "Posting Date",
                "Activity Description", "Amount", "Statement"]
        rows = [
            {"Transaction Date": "DEC 06, 2024", "Posting Date": "",
             "Activity Description": "Previous Balance", "Amount": "397.54",
             "Statement": "Jan2025"},
            {"Transaction Date": "DEC 05", "Posting Date": "DEC 09",
             "Activity Description": "IMPARK00011650U DELTA BC",
             "Amount": "17.00", "Statement": "Jan2025"},
            {"Transaction Date": "DEC 08", "Posting Date": "DEC 11",
             "Activity Description": "SAFEWAY #4909 WEST VANCOUVEBC",
             "Amount": "14.27", "Statement": "Jan2025"},
            {"Transaction Date": "DEC 10", "Posting Date": "DEC 10",
             "Activity Description": "PAYMENT - THANK YOU / PAIEMENT - MERCI",
             "Amount": "-570.00", "Statement": "Jan2025"},
        ]
        df = pd.DataFrame(rows, columns=cols)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            df.to_excel(w, sheet_name="Transactions", index=False)
        row_mapping = [("Jan2025", -1), ("Jan2025", 0), ("Jan2025", 1), ("Jan2025", 2)]

        out = build_t777_excel(buf.getvalue(), row_mapping)

        # Read back
        xls = pd.ExcelFile(io.BytesIO(out))
        assert set(xls.sheet_names) >= {"Transactions", "T777 Summary"}

        tx = pd.read_excel(xls, sheet_name="Transactions")
        for col in ("税务初筛结果", "T777 Field (English)", "Line No.",
                    "可扣性判断说明", "识别置信度", "人工复核建议"):
            assert col in tx.columns

        # IMPARK row should be deductible; SAFEWAY row should be not deductible.
        impark_row = tx[tx["Activity Description"].str.contains("IMPARK")].iloc[0]
        assert impark_row["税务初筛结果"] == SCREEN_DEDUCTIBLE
        assert impark_row["Line No."] == 9281

        safeway_row = tx[tx["Activity Description"].str.contains("SAFEWAY")].iloc[0]
        assert safeway_row["税务初筛结果"] == SCREEN_NOT_DEDUCTIBLE

        # Non-transaction rows must be dropped from T777 output.
        assert not any(tx["Activity Description"].str.contains("Previous Balance"))
        assert not any(tx["Activity Description"].str.contains("PAYMENT"))
        # 序号 must be contiguous 1..N on the surviving rows.
        assert list(tx["序号"]) == list(range(1, len(tx) + 1))
