from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.statement_parser import BankStatementParser, _add_spaces


class TestAddSpaces:
    def test_camel_case(self):
        assert _add_spaces("OnlineBanking") == "Online Banking"

    def test_digit_to_uppercase(self):
        assert _add_spaces("0732ROGERS") == "0732 ROGERS"

    def test_uppercase_to_digit(self):
        assert _add_spaces("ROGERS11") == "ROGERS 11"

    def test_short_code(self):
        # _add_spaces is only used for summary text, not transaction descriptions
        # (x_tolerance=1 handles word separation in transactions)
        result = _add_spaces("XQ9ZK7")
        assert result == "XQ9 ZK7"

    def test_multiple_spaces_collapsed(self):
        assert _add_spaces("a  b   c") == "a b c"


class TestParseSummary:
    def test_extracts_fields(self):
        text = (
            "Youraccountnumber: 03389-5127337\n"
            "FromApril1,2024toApril12,2024\n"
            "Youropeningbalance $0.00\n"
            "YourclosingbalanceonApril12,2024 =$2,788.09\n"
            "Totaldepositsintoyouraccount +9,169.97\n"
            "Totalwithdrawalsfromyouraccount -6,381.88\n"
        )
        from core.statement_parser import StatementInfo
        info = StatementInfo()
        BankStatementParser._parse_summary(text, info)
        assert info.account_number == "03389-5127337"
        assert info.opening_balance == "0.00"
        assert info.closing_balance == "2,788.09"
        assert info.total_deposits == "9,169.97"
        assert info.total_withdrawals == "6,381.88"


class TestBuildDataframe:
    def test_merges_continuation(self):
        rows = [
            {"Date": "1 Apr", "Description": "Online Banking", "Withdrawals": "", "Deposits": "", "Balance": ""},
            {"Date": "", "Description": "ROGERS 11 DIGIT", "Withdrawals": "96.77", "Deposits": "", "Balance": ""},
        ]
        df = BankStatementParser._build_dataframe(rows)
        assert len(df) == 1
        assert "96.77" in df.iloc[0]["Withdrawals"]
        assert "Online Banking" in df.iloc[0]["Description"]
        assert "ROGERS" in df.iloc[0]["Description"]

    def test_no_merge_when_prev_has_amount(self):
        rows = [
            {"Date": "1 Apr", "Description": "Payment A", "Withdrawals": "100.00", "Deposits": "", "Balance": ""},
            {"Date": "", "Description": "Payment B", "Withdrawals": "200.00", "Deposits": "", "Balance": ""},
        ]
        df = BankStatementParser._build_dataframe(rows)
        assert len(df) == 2

    def test_forward_fills_date(self):
        rows = [
            {"Date": "4 Apr", "Description": "A", "Withdrawals": "10", "Deposits": "", "Balance": ""},
            {"Date": "", "Description": "B", "Withdrawals": "20", "Deposits": "", "Balance": ""},
        ]
        df = BankStatementParser._build_dataframe(rows)
        assert df.iloc[1]["Date"] == "4 Apr"

    def test_filters_noise(self):
        rows = [
            {"Date": "", "Description": "Opening Balance", "Withdrawals": "", "Deposits": "", "Balance": "0.00"},
            {"Date": "", "Description": "", "Withdrawals": "", "Deposits": "", "Balance": "1 of 2"},
            {"Date": "", "Description": "Closing Balance", "Withdrawals": "", "Deposits": "100", "Balance": ""},
        ]
        df = BankStatementParser._build_dataframe(rows)
        assert not any("1 of 2" in str(v) for v in df.values.flatten())
