"""Unit tests for core.fee_sort.output_namer."""

from __future__ import annotations

from core.fee_sort.output_namer import OutputNamer


class TestOutputNamer:
    def test_full_metadata(self):
        name = OutputNamer.generate("Barclays", "12345678", "Barclays_5678_20230101-20231231")
        assert name == "Barclays_5678_FeeSort_20230101_20231231.xlsx"

    def test_missing_bank_name(self):
        name = OutputNamer.generate("", "12345678", "Bank_5678_20230601-20231231")
        assert name.startswith("Bank_5678_FeeSort_")

    def test_short_account(self):
        name = OutputNamer.generate("HSBC", "12", "HSBC_12_20230101-20230630")
        assert "_12_FeeSort_" in name

    def test_no_date_range_in_filename(self):
        name = OutputNamer.generate("NatWest", "99998888", "some_random_name")
        assert name == "NatWest_8888_FeeSort_start_end.xlsx"

    def test_bank_name_with_spaces(self):
        name = OutputNamer.generate("Bank of Scotland", "44445555", "BankofScotland_5555_20230101-20230331")
        assert name.startswith("BankofScotland_5555_FeeSort_")
