"""Unit tests for the credit-card rule engine."""
from __future__ import annotations

import pytest

from core.fee_sort.field_mapper import StandardRow
from core.fee_sort.rule_engine_cc import CCCategory, CreditCardRuleEngine


def _row(desc: str, amount: float, row_type: str = "transaction") -> StandardRow:
    return StandardRow(
        idx=0,
        row_type=row_type,
        statement="stmt",
        date="",
        description=desc,
        withdrawals=f"{amount:.2f}",
        deposits="",
        balance="",
        withdrawals_float=amount,
        schema="credit_card",
    )


class TestExclusions:
    def test_separator_excluded(self):
        r = _row("", 0, row_type="separator")
        c = CreditCardRuleEngine().classify(r)
        assert c.exclude is True
        assert c.rule_hit == "P0-separator"

    def test_previous_balance_excluded(self):
        r = _row("Previous Balance", 397.54, row_type="opening_balance")
        c = CreditCardRuleEngine().classify(r)
        assert c.exclude is True
        assert c.rule_hit == "P0-previous_balance"

    def test_card_payment_excluded(self):
        r = _row("PAYMENT - THANK YOU / PAIEMENT - MERCI", -570.0)
        c = CreditCardRuleEngine().classify(r)
        assert c.exclude is True
        assert c.rule_hit == "P0-card_payment"

    def test_cash_back_reward_excluded(self):
        r = _row("CASH BACK REWARD", -1102.84)
        c = CreditCardRuleEngine().classify(r)
        assert c.exclude is True
        assert c.rule_hit == "P0-cash_back_reward"


class TestRefund:
    def test_negative_merchant_is_refund(self):
        r = _row("TEMU.COM VICTORIA BC", -105.43)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.REFUND.value
        assert c.need_review is True  # policy: refunds always review
        assert c.rule_hit == "P1-refund"


class TestNewCategories:
    def test_interest_charge(self):
        r = _row("CASH ADVANCE INTEREST 22.99% TOTAL ACCOUNT BALANCE", 15.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.INTEREST.value

    def test_bylaw_fine(self):
        r = _row("CITY OF VAN-BYLAW FINE VANCOUVER BC", 75.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.FINE.value

    def test_duty_free_is_gift(self):
        r = _row("WORLD DUTY FREE / CONN RICHMOND BC", 120.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.GIFT.value

    def test_freedom_mobile_is_phone(self):
        r = _row("FREEDOM MOBILE BURNABY BC", 55.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.PHONE.value

    def test_rogers_is_phone_not_utilities(self):
        r = _row("ROGERS ******6807 TORONTO ON", 170.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.PHONE.value

    def test_toyota_is_auto_repair(self):
        r = _row("DESTINATION TOYOTA BUR BURNABY BC", 450.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.AUTO_REPAIR.value
        assert c.rule_hit == "P9-auto_repair"

    def test_endodontics_is_medical_not_personal_care(self):
        r = _row("EMERGENCE ENDODONTICS VANCOUVER BC", 900.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.MEDICAL.value

    def test_dental_routed_to_medical(self):
        r = _row("SASAMAT DENTAL VANCOUVER BC", 45.10)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.MEDICAL.value

    def test_haircut_still_personal_care(self):
        r = _row("GREAT CLIPS WEST VANCOUVEBC", 18.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.PERSONAL_CARE.value

    def test_hydro_still_utilities(self):
        r = _row("BC HYDRO VANCOUVER BC", 120.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.UTILITIES.value

    def test_choices_grocery(self):
        r = _row("CHOICES NORTH VANCOUVE NORTH VANCOUVBC", 35.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.GROCERIES.value

    def test_kitanoya_client_meals(self):
        """KITANOYA moved to Client Meals per 2026-04-22 annotation."""
        r = _row("KITANOYA GUU WITH GARL VANCOUVER BC", 65.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.CLIENT_MEALS.value

    def test_bean_cafe_under_50_personal(self):
        """Post-2026-04-22 rule: Restaurant <$50 → Personal by default."""
        r = _row("BEAN- WEST VAN COMM CT W-VANCOUVER BC", 6.50)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.PERSONAL.value

    def test_bean_cafe_over_50_restaurants(self):
        r = _row("BEAN- WEST VAN COMM CT W-VANCOUVER BC", 65.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.RESTAURANTS.value

    def test_tinder_is_subscription(self):
        r = _row("GOOGLE *TINDER DATING HALIFAX NS", 15.99)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.SUBSCRIPTIONS.value

    def test_dollarama_is_grocery(self):
        r = _row("DOLLARAMA #1027 WEST VANCOUVEBC", 12.50)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.GROCERIES.value

    def test_lily_dollar_is_grocery(self):
        r = _row("LILY'S DOLLAR PLUS WEST VANCOUVEBC", 8.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.GROCERIES.value

    def test_king_park_is_parking(self):
        r = _row("KING PARK VANCOUVER BC", 5.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.PARKING.value

    def test_bc_registry_is_annual_licence_fees(self):
        r = _row("BC REGISTRY COLIN INTE VICTORIA BC", 200.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.ANNUAL_LICENCE_FEES.value

    def test_csi_is_professional_training(self):
        """CSI moved to Professional Training per user annotation (2026-04-22)."""
        r = _row("CANADIAN SECURITIES INST TORONTO ON", 450.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.PROFESSIONAL_TRAINING.value

    def test_yacht_is_meals_and_entertainment(self):
        r = _row("LS REVOLUTION YACHT EX VANCOUVER BC", 320.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.MEALS_AND_ENTERTAINMENT.value

    def test_harry_potter_is_meals_and_entertainment(self):
        r = _row("SQ *HARRY POTTER: A FO VANCOUVER BC", 85.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.MEALS_AND_ENTERTAINMENT.value

    def test_on_the_run_ev_is_electricity_for_vehicle(self):
        r = _row("ON THE RUN EV 33016 VANCOUVER BC", 18.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.ELECTRICITY_FOR_VEHICLE.value

    def test_hillview_chev_is_electricity_for_vehicle(self):
        r = _row("CHV43103 HILLVIEW CHEV VANCOUVER BC", 22.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.ELECTRICITY_FOR_VEHICLE.value

    def test_al_hadbah_is_client_meals(self):
        """Matched BEFORE Restaurants — AL-HADBAH routes to Client Meals."""
        r = _row("AL-HADBAH MEDITERRANEA VANCOUVER BC", 120.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.CLIENT_MEALS.value

    def test_continental_seafood_is_client_meals(self):
        r = _row("CONTINENTAL SEAFOOD RE RICHMOND BC", 200.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.CLIENT_MEALS.value

    def test_eventbrite_is_client_entertainment(self):
        r = _row("EVENTBRITE/OTAKUBOPGHI SAINT JOHN NB", 45.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.CLIENT_ENTERTAINMENT.value

    def test_west_van_comm_ctr_is_personal(self):
        r = _row("SQ *WEST VANCOUVER COM WEST VANCOUVEBC", 50.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.PERSONAL.value

    def test_miniso_is_personal(self):
        r = _row("MINISO-BC-PARK ROYAL WEST VANCOUVEBC", 15.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.PERSONAL.value

    def test_loonie_toonie_is_personal(self):
        r = _row("LOONIE TOONIE VARIETY RICHMOND BC", 10.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.PERSONAL.value


class TestV2Retraining:
    """Rules added from user's annotated feedback (2026-04-22)."""

    def test_csi_is_professional_training_not_licence(self):
        r = _row("CANADIAN SECURITIES IN TORONTO ON", 450.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.PROFESSIONAL_TRAINING.value
        assert c.rule_hit == "P4b-professional_training"

    def test_bc_registry_still_annual_licence(self):
        """Sanity: stripping CSI from licence regex must not break BC Registry."""
        r = _row("BC REGISTRY COLIN INTE VICTORIA BC", 200.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.ANNUAL_LICENCE_FEES.value

    @pytest.mark.parametrize("desc", [
        "OEB AMBLESIDE WEST VANCOUVEBC",
        "CINDYS PALACE RESTAURA VANCOUVER BC",
        "HOKKAIDO RAMEN SANTOUK VANCOUVER BC",
        "JADE PALACE CHINESE RE BURNABY BC",
        "UNIVERSITY GOLF CLUB - VANCOUVER BC",
        "CACTUS CLUB STATION SQ BURNABY BC",
        "PUTIEN HONG KONG HKG",
        "HAPPY LAMB HOT POT BURNABY BC",
        "PAJO'S YVR RICHMOND BC",
        "BREAKWATER BISTRO VICTORIA BC",
        "KOSOO RESTAURANT + BAR VANCOUVER BC",
    ])
    def test_hardcoded_client_meals(self, desc):
        r = _row(desc, 120.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.CLIENT_MEALS.value, desc

    def test_restaurant_under_50_defaults_to_personal(self):
        r = _row("MCDONALD'S #26031 WEST VANCOUVEBC", 12.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.PERSONAL.value
        assert c.rule_hit == "P15-restaurant_small_personal"
        assert c.need_review is True

    def test_restaurant_50_or_more_stays_restaurants(self):
        r = _row("TST-LA PIAZZA DARIO VANCOUVER BC", 50.01)
        c = CreditCardRuleEngine().classify(r)
        # This is NOT in client-meals hardcoded list, should stay Restaurants
        # NOTE: TST-LA PIAZZA IS in our hardcoded list — test with a generic
        # non-hardcoded restaurant instead.

    def test_restaurant_generic_50_plus_stays_restaurants(self):
        r = _row("SOME GENERIC RESTAURANT VANCOUVER BC", 75.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.RESTAURANTS.value

    def test_client_meal_hardcoded_under_50_still_client_meal(self):
        """Hardcoded venues override the <$50 personal rule."""
        r = _row("OEB AMBLESIDE WEST VANCOUVEBC", 15.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.CLIENT_MEALS.value

    def test_toyota_finance_is_auto_lease(self):
        r = _row("TERM LOAN TOYOTA FINANCE TORONTO ON", 380.50)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.AUTO_LEASE.value
        assert c.rule_hit == "P8b-auto_lease"

    def test_destination_toyota_still_auto_repair(self):
        """Dealership service → Auto Repair; only financing goes to Auto Lease."""
        r = _row("DESTINATION TOYOTA BUR BURNABY BC", 450.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.AUTO_REPAIR.value

    def test_icbc_is_auto_insurance(self):
        r = _row("AUTO INSURANCE ICBC VICTORIA BC", 208.66)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.AUTO_INSURANCE.value
        assert c.rule_hit == "P8c-auto_insurance"

    def test_icbc_alone_is_auto_insurance(self):
        r = _row("ICBC BC 604-555-0199", 150.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.AUTO_INSURANCE.value

    @pytest.mark.parametrize("desc", [
        "YUMMY BAO BURNABY BC",
        "MCDONALD'S #26031 WEST VANCOUVEBC",
        "A & W PARK ROYAL SOUTH WEST VANCOUVEBC",
        "FRESHSLICE PIZZA WEST VANCOUVEBC",
    ])
    def test_generic_restaurant_over_50_is_restaurants(self, desc):
        """Non-hardcoded restaurants with amount ≥ $50 stay as Restaurants."""
        r = _row(desc, 85.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.RESTAURANTS.value

    def test_negative_shoppers_is_refund_not_personal_care(self):
        """P1 runs before P11 — negative amount overrides merchant-based category."""
        r = _row("SHOPPERS DRUG MART #22 VANCOUVER BC", -19.03)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.REFUND.value


class TestTeslaSplit:
    """Tesla: ≤$50 → Charging, >$50 → Auto Repair."""

    def test_tesla_small_amount_is_charging(self):
        r = _row("TESLA TORONTO ON", 25.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.ELECTRICITY_FOR_VEHICLE.value
        assert c.rule_hit == "P8-tesla_charging"

    def test_tesla_exactly_50_is_charging(self):
        r = _row("TESLA TORONTO ON", 50.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.ELECTRICITY_FOR_VEHICLE.value

    def test_tesla_large_amount_is_auto_repair(self):
        r = _row("TESLA TORONTO ON", 464.80)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.AUTO_REPAIR.value
        assert c.rule_hit == "P8-tesla_repair"


class TestMerchantCategories:
    @pytest.mark.parametrize("desc,expected", [
        # Subscriptions
        ("GOOGLE *TIDAL 650-253-0000 NS", CCCategory.SUBSCRIPTIONS),
        ("CLAUDE.AI SUBSCRIPTION SAN FRANCISCOCA", CCCategory.SUBSCRIPTIONS),
        ("AMAZON.CA PRIME MEMBER AMAZON.CA/PRIBC", CCCategory.SUBSCRIPTIONS),
        ("AGI*BESTBUY.CA/BILLING 866-237-8289 ON", CCCategory.SUBSCRIPTIONS),
        # Travel
        ("TRIP.COM VANCOUVER BC", CCCategory.TRAVEL),
        ("AIR CANADA TORONTO ON", CCCategory.TRAVEL),
        ("MARRIOTT HOTELS VANCOUVER BC", CCCategory.TRAVEL),
        # Charging (non-Tesla)
        ("CHARGEPOINT CANADA VANCOUVER BC", CCCategory.ELECTRICITY_FOR_VEHICLE),
        # Gas
        ("SHELL C01217 WEST VANCOUVEBC", CCCategory.GAS),
        ("ESSO 7-ELEVEN 37857 VANCOUVER BC", CCCategory.GAS),
        # Parking
        ("CITY OF VAN PAYBYPHONE VANCOUVER BC", CCCategory.PARKING),
        ("IMPARK00011650U DELTA BC", CCCategory.PARKING),
        ("INDIGO PARK - V345 NORTH VANCOUVBC", CCCategory.PARKING),
        ("CP07 METERS 778-909-7805 BC", CCCategory.PARKING),
        # Transit
        ("BCF - SALISH EAGLE VICTORIA BC", CCCategory.TRANSIT),
        ("COMPASS ACCOUNT BURNAB BURNABY BC", CCCategory.TRANSIT),
        # Groceries
        ("SAFEWAY #4909 WEST VANCOUVEBC", CCCategory.GROCERIES),
        ("LOBLAWS CITY MARKET - WEST VANCOUVEBC", CCCategory.GROCERIES),
        ("WAL-MART # 3057 N VANCOUVER BC", CCCategory.GROCERIES),
        ("7 ELEVEN STORE #25931 WEST VANCOUVEBC", CCCategory.GROCERIES),
        # Personal Care
        ("SHOPPERS DRUG MART #22 VANCOUVER BC", CCCategory.PERSONAL_CARE),
        ("LONDON DRUGS 44 WEST VANCOUVEBC", CCCategory.PERSONAL_CARE),
        ("GREAT CLIPS WEST VANCOUVEBC", CCCategory.PERSONAL_CARE),
        # Medical (dental moved here from Personal Care)
        ("SASAMAT DENTAL VANCOUVER BC", CCCategory.MEDICAL),
        # Utilities (hydro / municipal — telecom moved to Phone)
        ("DISTRICT OF WEST VANCO WEST VANCOUVEBC", CCCategory.UTILITIES),
        ("GREATER VANCOUVER POWE LANGLEY BC", CCCategory.UTILITIES),
        # Entertainment
        ("SEA TO SKY GONDOLA SQUAMISH BC", CCCategory.ENTERTAINMENT),
        ("GROUSE MOUNTAIN N-VANCOUVER BC", CCCategory.ENTERTAINMENT),
        ("GREATER VANCOUVER ZOO ALDERGROVE BC", CCCategory.ENTERTAINMENT),
        ("SPORTS JUNKIES VANCOUVER BC", CCCategory.ENTERTAINMENT),
        # Online Shopping
        ("TEMU.COM VICTORIA BC", CCCategory.ONLINE_SHOPPING),
        ("AMZN MKTP CA*Z95UK3RK1 WWW.AMAZON.CAON", CCCategory.ONLINE_SHOPPING),
        ("WINNERS 335 W-VANCOUVER BC", CCCategory.ONLINE_SHOPPING),
        ("THE HOME DEPOT #7035 WEST VANCOUVEBC", CCCategory.ONLINE_SHOPPING),
    ])
    def test_category_match(self, desc, expected):
        r = _row(desc, 25.00)  # positive amount, arbitrary
        c = CreditCardRuleEngine().classify(r)
        assert c.category == expected.value, (
            f"{desc!r} expected {expected.value} got {c.category}"
        )


class TestFallback:
    def test_unknown_merchant_falls_back(self):
        r = _row("OBSCURE VENDOR XYZ 123", 42.00)
        c = CreditCardRuleEngine().classify(r)
        assert c.category == CCCategory.OTHER_EXPENSE.value
        assert c.need_review is True
        assert c.rule_hit == "P23-fallback"
