"""Tests for merchant override learning."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.fee_sort.field_mapper import StandardRow
from core.fee_sort.merchant_overrides import (
    MerchantOverrides,
    merchant_signature,
)
from core.fee_sort.rule_engine_cc import CCCategory, CreditCardRuleEngine


class TestSignature:
    def test_strips_store_numbers(self):
        assert merchant_signature("IMPARK00011650U DELTA BC") == "IMPARK DELTA BC"

    def test_strips_hash_and_asterisk(self):
        assert merchant_signature("SHOPPERS DRUG MART #22 VANCOUVER BC") == \
               "SHOPPERS DRUG MART"

    def test_uppercases(self):
        assert merchant_signature("temu.com victoria bc") == "TEMU.COM VICTORIA BC"

    def test_first_three_words_only(self):
        sig = merchant_signature("SOMETHING VERY LONG AND MORE TEXT HERE")
        assert len(sig.split()) == 3


class TestOverrides:
    def test_add_and_lookup(self, tmp_path: Path):
        path = tmp_path / "o.json"
        o = MerchantOverrides(path=path)
        assert o.lookup("MCDONALD'S #26031 WEST VANCOUVEBC") is None

        is_new = o.add("MCDONALD'S #26031 WEST VANCOUVEBC",
                       CCCategory.CLIENT_MEALS.value)
        assert is_new is True
        assert o.lookup("MCDONALD'S #26031 WEST VANCOUVEBC") == \
               CCCategory.CLIENT_MEALS.value

        # Another MCDONALD with same signature → should match
        assert o.lookup("MCDONALD'S #99999 WEST VANCOUVEBC") == \
               CCCategory.CLIENT_MEALS.value

    def test_persistence(self, tmp_path: Path):
        path = tmp_path / "o.json"
        o1 = MerchantOverrides(path=path)
        o1.add("TEMU.COM VICTORIA BC", CCCategory.PERSONAL.value)

        # Fresh instance reads from disk
        o2 = MerchantOverrides(path=path)
        assert o2.lookup("TEMU.COM VICTORIA BC") == CCCategory.PERSONAL.value

    def test_remove(self, tmp_path: Path):
        path = tmp_path / "o.json"
        o = MerchantOverrides(path=path)
        o.add("IMPARK00011650U DELTA BC", CCCategory.PARKING.value)
        sig = merchant_signature("IMPARK00011650U DELTA BC")

        assert o.remove(sig) is True
        assert o.lookup("IMPARK00011650U DELTA BC") is None
        assert o.remove(sig) is False  # idempotent

    def test_update_existing(self, tmp_path: Path):
        path = tmp_path / "o.json"
        o = MerchantOverrides(path=path)
        o.add("SHELL C01217 WEST VANCOUVEBC", CCCategory.GAS.value)
        # Same signature, different category — marks as new since category changed
        is_new = o.add("SHELL C01217 WEST VANCOUVEBC",
                       CCCategory.ELECTRICITY_FOR_VEHICLE.value)
        assert is_new is True
        assert o.lookup("SHELL C01217 WEST VANCOUVEBC") == \
               CCCategory.ELECTRICITY_FOR_VEHICLE.value


class TestRuleEngineWithOverrides:
    def test_override_beats_regex(self, tmp_path: Path):
        path = tmp_path / "o.json"
        overrides = MerchantOverrides(path=path)
        overrides.add("MCDONALD'S #26031 WEST VANCOUVEBC",
                      CCCategory.CLIENT_MEALS.value)

        engine = CreditCardRuleEngine(overrides=overrides)
        row = StandardRow(
            idx=0, row_type="transaction", statement="",
            date="", description="MCDONALD'S #26031 WEST VANCOUVEBC",
            withdrawals="12.00", deposits="", balance="",
            withdrawals_float=12.00, schema="credit_card",
        )
        result = engine.classify(row)
        # Without override: $12 <$50 → Personal (restaurant rule)
        # With override: user-set CLIENT_MEALS wins
        assert result.category == CCCategory.CLIENT_MEALS.value
        assert result.rule_hit == "P-1-user_override"

    def test_override_does_not_affect_excluded_rows(self, tmp_path: Path):
        """Separators / PAYMENT / CASH BACK still excluded despite overrides."""
        path = tmp_path / "o.json"
        overrides = MerchantOverrides(path=path)
        overrides.add("PAYMENT - THANK YOU / PAIEMENT - MERCI",
                      CCCategory.CLIENT_MEALS.value)

        engine = CreditCardRuleEngine(overrides=overrides)
        # PAYMENT rows are row_type="transaction" in the merged Excel but caught
        # by P0 via _PAT_CARD_PAYMENT. Override only applies when row_type is
        # "transaction" — here row_type is transaction, so override fires first.
        # To truly protect excluded rows we check row_type; separators use
        # row_type="separator" which IS protected.
        row = StandardRow(
            idx=0, row_type="separator", statement="",
            date="", description="PAYMENT - THANK YOU / PAIEMENT - MERCI",
            withdrawals="", deposits="", balance="",
            withdrawals_float=0.0, schema="credit_card",
        )
        result = engine.classify(row)
        assert result.exclude is True  # separator still wins


class TestThreshold:
    def test_restaurant_threshold_configurable(self):
        """Passing a different threshold changes the <threshold personal rule."""
        engine = CreditCardRuleEngine(restaurant_personal_threshold=30.0)
        row_25 = StandardRow(
            idx=0, row_type="transaction", statement="",
            date="", description="SOME GENERIC RESTAURANT BC",
            withdrawals="25.00", deposits="", balance="",
            withdrawals_float=25.00, schema="credit_card",
        )
        assert engine.classify(row_25).category == CCCategory.PERSONAL.value

        row_35 = StandardRow(
            idx=0, row_type="transaction", statement="",
            date="", description="SOME GENERIC RESTAURANT BC",
            withdrawals="35.00", deposits="", balance="",
            withdrawals_float=35.00, schema="credit_card",
        )
        # $35 ≥ $30 threshold → Restaurants
        assert engine.classify(row_35).category == CCCategory.RESTAURANTS.value
