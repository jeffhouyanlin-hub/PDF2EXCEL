"""Priority-based rule engine for credit-card transaction classification.

Parallel to `rule_engine.RuleEngine` (which targets UK business bank statements).
This engine targets Canadian personal credit-card merchant data.

Order matters — the first rule that matches wins. Ordering is tuned so that
merchants which could match multiple categories land in the most specific one
(e.g. "AMAZON.CA PRIME MEMBER" must hit Subscriptions before Online Shopping).
"""
from __future__ import annotations

import re
from enum import Enum

from core.fee_sort.field_mapper import StandardRow
from core.fee_sort.rule_engine import ClassificationResult


class CCCategory(str, Enum):
    """Credit-card personal spending categories."""

    REFUND = "Refund"
    INTEREST = "Interest"
    FINE = "Fine"
    ANNUAL_LICENCE_FEES = "Annual Licence Fees"
    PROFESSIONAL_TRAINING = "Professional Training"
    SUBSCRIPTIONS = "Subscriptions"
    GIFT = "Gift"
    TRAVEL = "Travel"
    ELECTRICITY_FOR_VEHICLE = "Electricity for vehicle"
    AUTO_LEASE = "Auto Lease"
    AUTO_INSURANCE = "Auto Insurance"
    AUTO_REPAIR = "Auto Repair"
    GAS = "Gas"
    PARKING = "Parking"
    TRANSIT = "Transit"
    PHONE = "Phone"
    CLIENT_MEALS = "Client meals"
    RESTAURANTS = "Restaurants"
    GROCERIES = "Groceries"
    MEDICAL = "Medical"
    PERSONAL_CARE = "Personal Care"
    UTILITIES = "Utilities"
    CLIENT_ENTERTAINMENT = "Client entertainment"
    ENTERTAINMENT = "Entertainment"
    MEALS_AND_ENTERTAINMENT = "Meals and Entertainment"
    PERSONAL = "Personal"
    ONLINE_SHOPPING = "Online Shopping"
    OTHER_EXPENSE = "Other Expense"


# Amount thresholds — both user-configurable via CreditCardRuleEngine.__init__.
# Tesla: ≤ threshold → Electricity for vehicle (charging); > → Auto Repair.
# Restaurant: < threshold → Personal (small bill, presumed personal); ≥ → Restaurants (review).
_TESLA_CHARGING_THRESHOLD = 50.0
_RESTAURANT_PERSONAL_THRESHOLD = 50.0


# ---------------------------------------------------------------------------
# Regex patterns (case-insensitive), grouped by category
# ---------------------------------------------------------------------------

# P0 — always excluded (card payments, rewards)
_PAT_CARD_PAYMENT = re.compile(
    r"PAYMENT\s*-\s*THANK\s*YOU|PAIEMENT\s*-\s*MERCI", re.I,
)
_PAT_CASH_BACK_REWARD = re.compile(r"CASH\s*BACK\s*REWARD", re.I)

# P2 — Interest charges (cash advance interest, purchase interest)
_PAT_INTEREST = re.compile(
    r"CASH\s*ADVANCE\s*INTEREST|PURCHASE\s*INTEREST|INTEREST\s*CHARGE",
    re.I,
)

# P3 — Fines (municipal bylaw tickets, traffic violations)
_PAT_FINE = re.compile(
    r"BYLAW\s*FINE|TRAFFIC\s*FINE|PARKING\s*TICKET|\bCITY\s*FINE\b",
    re.I,
)

# P4 — Annual Licence Fees (professional registrations, government registries)
_PAT_ANNUAL_LICENCE_FEES = re.compile(
    r"\bBC\s*REGISTRY\b"
    r"|LAW\s*SOCIETY|\bCPA\s*CANADA\b|CHARTERED\s*PROFESSIONAL"
    r"|ANNUAL\s*LICEN[CS]E|LICEN[CS]E\s*RENEWAL",
    re.I,
)

# P4b — Professional Training (courses, education fees for work)
_PAT_PROFESSIONAL_TRAINING = re.compile(
    r"CANADIAN\s*SECURITIES(?:\s*IN(?:ST)?)?"   # CSI — "IN" or "INST" suffix
    r"|\bCSI\b"
    r"|IFSE\s*INSTITUTE"
    r"|PROFESSIONAL\s*TRAINING|\bTRAINING\s*FEE\b"
    r"|EDUCATION\s*FEE|COURSE\s*FEE",
    re.I,
)

# P2 — Subscriptions (match before Online Shopping so Amazon Prime / Google
# services / Billing-style merchants don't leak into retail)
_PAT_SUBSCRIPTIONS = re.compile(
    r"GOOGLE\s*\*\s*(?:GOOGLE|YOUTUBE|TIDAL|ONE|STORE|PLAY|TINDER)"
    r"|\bTINDER\b|\bBUMBLE\b|\bHINGE\b|MATCH\.COM"
    r"|CLAUDE\.AI|CHATGPT|OPENAI|ANTHROPIC"
    r"|NETFLIX|SPOTIFY|DISNEY\s*PLUS|HULU|HBO|CRUNCHYROLL"
    r"|APPLE\.COM|ITUNES|AMAZON\.?CA?\s*PRIME|AMAZON\s*PRIME"
    r"|GODADDY|IONOS|CLOUDFLARE|NAMECHEAP"
    r"|PDFAID|CANVA|ADOBE|NOTION|DROPBOX"
    r"|MICROSOFT\s*36[5O]|OFFICE\s*36[5O]"
    r"|AGI\*?BESTBUY\.CA/BILLING|BESTBUY\.CA/PRO"
    r"|\bBILLING\b|\bSUBSCRIPTION\b",
    re.I,
)

# P5 — Gift (duty-free, gift shops, gift cards)
_PAT_GIFT = re.compile(
    r"DUTY[-\s]*FREE|GIFT\s*SHOP|GIFT\s*CARD|HALLMARK",
    re.I,
)

# P6 — Travel (flights, hotels, rideshares, booking platforms, car rentals)
_PAT_TRAVEL = re.compile(
    r"AIR\s*CANADA|AIRCANADA|WESTJET|PORTER\s*AIRLINES|\bFLAIR\b"
    r"|TRIP\.COM|EXPEDIA|BOOKING\.COM|AIRBNB|HOTELS\.COM|KAYAK"
    r"|\bHOTEL\b|MARRIOTT|HILTON|HYATT|FAIRMONT|SHERATON|RAMADA"
    r"|HOLIDAY\s*INN|BEST\s*WESTERN|HOSTEL"
    r"|HERTZ|\bAVIS\b|ENTERPRISE\s*RENT|BUDGET\s*RENT|NATIONAL\s*CAR"
    r"|\bUBER\b|\bLYFT\b|\*\s*GRAB\b",
    re.I,
)

# P10 — Electricity for vehicle (EV charging; Tesla ≤ threshold handled below)
_PAT_CHARGING_NON_TESLA = re.compile(
    r"CHARGEPOINT|\bEVGO\b|ELECTRIFY\s*CANADA|FLO\s*NETWORK|PETRO[-\s]*CAN\s*EV"
    r"|ON\s*THE\s*RUN\s*EV|HILLVIEW\s*CHEV|\bCHV\d+\b",
    re.I,
)
_PAT_TESLA = re.compile(r"\bTESLA\b", re.I)

# P7b — Auto Lease (vehicle financing / lease companies)
_PAT_AUTO_LEASE = re.compile(
    r"TOYOTA\s*FINANCE|TERM\s*LOAN.*TOYOTA"
    r"|HONDA\s*FINANCIAL|FORD\s*CREDIT|GM\s*FINANCIAL"
    r"|BMW\s*FINANCIAL|MERCEDES[-\s]*BENZ\s*FINANCIAL"
    r"|VEHICLE\s*LEASE|CAR\s*LEASE|AUTO\s*LEASE",
    re.I,
)

# P7c — Auto Insurance (ICBC, private auto insurers)
_PAT_AUTO_INSURANCE = re.compile(
    r"\bICBC\b"
    r"|AUTO\s*INSURANCE|CAR\s*INSURANCE|VEHICLE\s*INSURANCE"
    r"|TD\s*INSURANCE|BELAIRDIRECT|INTACT\s*INSURANCE",
    re.I,
)

# P8 — Auto Repair (car dealerships + generic auto service chains)
_PAT_AUTO_REPAIR = re.compile(
    r"DESTINATION\s*TOYOTA|\bTOYOTA\b|\bHONDA\b|\bFORD\b|CHEVROLET|\bCHEVY\b"
    r"|\bMAZDA\b|\bNISSAN\b|HYUNDAI|\bKIA\b|\bBMW\b|LEXUS|SUBARU|VOLKSWAGEN|\bVW\b"
    r"|AUTO\s*(?:REPAIR|SERVICE|BODY)|AUTOBODY"
    r"|\bMIDAS\b|MEINEKE|JIFFY\s*LUBE|MR\s*LUBE|SPEEDY\s*AUTO|GREAT\s*CANADIAN\s*OIL",
    re.I,
)

# P6 — Gas
_PAT_GAS = re.compile(
    r"\bSHELL\b|PETRO[-\s]*CAN(?!\s*EV)|\bESSO\b|CHEVRON|\bHUSKY\b"
    r"|ULTRAMAR|\bMOBIL\b|CO-?OP\s*GAS|\bPIONEER\s*GAS",
    re.I,
)

# P7 — Parking
_PAT_PARKING = re.compile(
    r"IMPARK|EASYPARK|PARKCHAMP|INDIGO\s*PARK|PAYBYPHONE|PARK(?:ING)?\s*LOT"
    r"|CP\d+\s*METERS|ADV\s*PARKING|\bPARKING\b|\bGARAGE\b|KING\s*PARK",
    re.I,
)

# P8 — Transit
_PAT_TRANSIT = re.compile(
    r"\bBCF\b|BC\s*FERRIES|COMPASS\s*(?:ACCOUNT|CARD)"
    r"|TRANSLINK|GO\s*TRANSIT|SKYTRAIN|VIA\s*RAIL",
    re.I,
)

# P9 — Restaurants
_PAT_RESTAURANTS = re.compile(
    r"MCDONALD|\bA\s*&\s*W\b|TIM\s*HORTON|STARBUCKS|SUBWAY|KFC|BURGER\s*KING|WENDY"
    r"|SUSHI|RAMEN|PIZZA|FRESHSLICE|\bPHO\b|CURRY|\bDIMSUM\b|\bBAO\b"
    r"|\bPUB\b|\bBAR\b|\bCAFE\b|COFFEE\b|BISTRO|BREWERY|EATERY|GRILL\b|KITCHEN\b"
    r"|\bDINER\b|\bDINING\b|NOODLE|YOGURT|\bTCBY\b|\bICE\s*CREAM\b"
    r"|RESTAURANT|RESTAURA|CUISIN(?:E)?|TERMINAL\s*CITY\s*CLUB"
    r"|CINDY|FORTUNE\s*FEAST|\bOEB\b|\bOSAKA\b|SQUARE\s*RIGGER|GREEN\s*DAY"
    r"|VANCOUVER\s*RED\s*STAR|PERSIA\s*FOODS"
    r"|LAO\s*CAI|YUMMY\s*BAO|SOUTH\s*SILK\s*ROAD|ABURI|SAMURAI"
    r"|KITANOYA|\bGUU\b|\bBEAN[-\s]|\bBEANS?\b",
    re.I,
)

# P10 — Groceries (supermarkets, convenience stores, Chinese grocers)
_PAT_GROCERIES = re.compile(
    r"SAFEWAY|LOBLAWS|REAL\s*CDN|SUPERSTORE|\bT&T\b|FRESH\s*ST\s*MARKET"
    r"|WHOLE\s*FOODS|\bCOSTCO\b|URBAN\s*FARE|KINS\s*MARKET|SAVE[-\s]ON[-\s]FOODS"
    r"|WAL[-\s]?MART|\bCHOICES\b|SUNGIVEN|PERSIA\s*FOODS"
    r"|7[-\s]?ELEVEN|SEVEN[-\s]ELEVEN"
    r"|DOLLARAMA|DOLLAR\s*TREE|\bDOLLAR\s*PLUS\b|LILY'?S\s*DOLLAR"
    r"|CHANGLUCK|\bCONVENIENC|\bVENDING",
    re.I,
)

# P13 — Phone (mobile carriers + telecom); matched before Utilities.
_PAT_PHONE = re.compile(
    r"\bTELUS\b|\bROGERS\b|\bBELL\b|\bFIDO\b|KOODO|VIRGIN\s*MOBILE"
    r"|\bSHAW\b|FREEDOM\s*MOBILE|\bCHATR\b|PUBLIC\s*MOBILE",
    re.I,
)

# P16 — Medical (dental, clinics, specialists; separate from Personal Care)
_PAT_MEDICAL = re.compile(
    r"DENTAL|DENTIST|\bORTHO\b|ENDODONTIC|PERIODONTIC|\bCHIRO\b"
    r"|CLINIC\b|MEDICAL\s*CENTRE|MEDICAL\s*CENTER|\bHOSPITAL\b"
    r"|PHYSIOTHERAPY|PHYSIO\b|\bDOCTOR\b|\bPHYSICIAN\b|\bOPTOMETRY\b|OPTICAL",
    re.I,
)

# P17 — Personal Care (drugstores, haircut, spa, salon — excludes dental/medical)
_PAT_PERSONAL_CARE = re.compile(
    r"SHOPPERS\s*DRUG|LONDON\s*DRUGS|REXALL|PHARMACY|PHARMA"
    r"|GREAT\s*CLIPS|CHATTERS|SALON|\bSPA\b|MASSAGE|\bNAIL\b|BARBER",
    re.I,
)

# P18 — Utilities (hydro, gas utility, municipal service charges, no telecom)
_PAT_UTILITIES = re.compile(
    r"BC\s*HYDRO|FORTIS\s*BC|FORTISBC|HYDRO\s*QUEBEC|ENMAX"
    r"|DISTRICT\s*OF\s*WEST\s*VANCO|GREATER\s*VANCOUVER\s*POWE",
    re.I,
)

# P13 — Entertainment (attractions, cinemas, sports, recreation)
_PAT_ENTERTAINMENT = re.compile(
    r"CINEMA|CINEPLEX|IMAX|THEATRE|THEATER"
    r"|\bZOO\b|AQUARIUM|MUSEUM|GALLERY"
    r"|SEA\s*TO\s*SKY\s*GONDOLA|GROUSE\s*MOUNTAIN|GREATER\s*VANCOUVER\s*ZOO"
    r"|SPORTS\s*JUNKIES|\bSKI\b|\bGOLF\b|BOWLING|CASINO"
    r"|TICKETMASTER|\bTICKET\b",
    re.I,
)

# P21 — Meals & Entertainment (catch-all for hospitality/experiences that don't
# cleanly fit Restaurants or Entertainment — boat charters, immersive shows, etc.)
_PAT_MEALS_AND_ENTERTAINMENT = re.compile(
    r"\bYACHT\b|BOAT\s*CHARTER|\bCRUISE\b"
    r"|HARRY\s*POTTER|IMMERSIVE|ESCAPE\s*ROOM",
    re.I,
)

# Client Meals — business meals taken with clients at specific venues. The
# list comes from the user's manual annotation (餐费 sheet). Every hit still
# requires manual review (user policy: "每一个都需要人工审核").
# Matched BEFORE Restaurants so these venues route to Client Meals.
_PAT_CLIENT_MEALS = re.compile(
    r"AL[-\s]*HADBAH|MEDITERRANEA"
    r"|CONTINENTAL\s*SEAFOOD"
    r"|OEB\s*AMBLESIDE"
    r"|CINDY'?S?\s*PALACE"
    r"|VANCOUVER\s*RED\s*STAR"
    r"|UNIVERSITY\s*GOLF\s*CLUB"
    r"|HOKKAIDO\s*RAMEN|SANTOUK"
    r"|JADE\s*PALACE|JADE\s*GARDEN"
    r"|LIFT\s*BAR\s*GRILL"
    r"|GOLDEN\s*CITY\s*RESTAURANT"
    r"|NONG\s*GENG\s*JI"
    r"|FEVER\*?\s*AUTHENTIC"
    r"|HAPPY\s*VALLEY\s*VILLAGE"
    r"|MAGS\s*99"
    r"|NORI\s*JAPANESE"
    r"|SEAPORT\s*CITY"
    r"|FORTUNE\s*FEAST"
    r"|CHANG\s*AN\s*RESTAURANT"
    r"|TST-?LA\s*PIAZZA|LA\s*PIAZZA\s*DARIO"
    r"|SAKURA\s*ICHIBAN"
    r"|HING\s*LUNG"
    r"|\bSURA\b\s*VANCOUVER"
    r"|BREAKWATER\s*BISTRO"
    r"|HIKO\s*SUSHI"
    r"|DAI\s*JANG\s*KUM"
    r"|BUBBLE\s*WAFFLE"
    r"|PAJO'?S"
    r"|CHEF\s*KITCHEN"
    r"|KOSOO\s*RESTAURANT"
    r"|ICHIBAN\s*JAPANESE"
    r"|LSP\*?\s*SQUAMISH\s*SAMURAI"
    r"|RICKY'?S\s*ALL\s*DAY"
    r"|KITANOYA|\bGUU\b"
    r"|MEET\s*FRESH"
    r"|BOAT\s*HOUSE"
    r"|SQ\s*\*\s*MINAMI"
    r"|MENYA\s*RAIZO"
    r"|RENS\s*CLUB"
    r"|PRINCE\s*RESTAURANT"
    r"|TST-?TP\s*FORT"
    r"|TOKU\s*JAPANESE"
    r"|PUTIEN"
    r"|HAPPY\s*LAMB\s*HOT\s*POT"
    r"|CACTUS\s*CLUB"
    r"|TOKYO\s*MAZESOBA"
    r"|SEAPORT|OSAKA\s*PARK\s*ROYAL"
    r"|LAO\s*CAI|SOUTH\s*SILK\s*ROAD"
    r"|HORIN\s*RAMEN|DA\s*CHUAN\s*JIA"
    r"|TERMINAL\s*CITY\s*CLUB",
    re.I,
)

# Client Entertainment — events/tickets purchased for client hosting.
# Matched BEFORE Entertainment.
_PAT_CLIENT_ENTERTAINMENT = re.compile(
    r"EVENTBRITE|TICKETMASTER\s*CLIENT",
    re.I,
)

# Personal — non-deductible personal spending on specific merchants user has
# flagged as personal consumption.
_PAT_PERSONAL = re.compile(
    r"SQ\s*\*\s*WEST\s*VANCOUVER\s*COM"        # West Vancouver Community Centre
    r"|LOONIE\s*TOONIE|MINISO",
    re.I,
)

# P14 — Online Shopping (catch retail last; includes physical big-box since user
# has not requested a separate "Retail" category)
_PAT_ONLINE_SHOPPING = re.compile(
    r"TEMU|AMAZON|AMZN"
    r"|EBAY|ETSY|ALIEXPRESS|ALIPAY|WAYFAIR"
    r"|HOME\s*DEPOT|\bIKEA\b|CANADIAN\s*TIRE|\bLOWES?\b|LOWE'S|\bRONA\b"
    r"|BEST\s*BUY|BESTBUY|WINNERS|MARSHALLS|WALMART\.CA"
    r"|\bPAYPAL\b",
    re.I,
)


class CreditCardRuleEngine:
    """Classify a credit-card StandardRow into one of CCCategory values."""

    def __init__(
        self,
        tesla_charging_threshold: float = _TESLA_CHARGING_THRESHOLD,
        restaurant_personal_threshold: float = _RESTAURANT_PERSONAL_THRESHOLD,
        overrides: "MerchantOverrides | None" = None,  # noqa: F821
    ) -> None:
        self._tesla_threshold = tesla_charging_threshold
        self._restaurant_threshold = restaurant_personal_threshold
        self._overrides = overrides

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def classify(self, row: StandardRow) -> ClassificationResult:
        """Iterate priority checkers; first match wins. Falls back to Other Expense."""
        # P-1: user-learned override wins over everything EXCEPT excluded rows
        # (separators / opening balance / PAYMENT / CASH BACK are still auto-filtered).
        if self._overrides is not None and row.row_type == "transaction":
            override_cat = self._overrides.lookup(row.description)
            if override_cat:
                return ClassificationResult(
                    category=override_cat,
                    detail="user override",
                    rule_hit="P-1-user_override",
                )
        for checker in (
            self._check_p0_exclude,
            self._check_p1_refund,
            self._check_p2_interest,
            self._check_p3_fine,
            self._check_p4_annual_licence_fees,
            self._check_p4b_professional_training,
            self._check_p5_subscriptions,
            self._check_p6_gift,              # before Travel so airport DUTY FREE → Gift
            self._check_p7_travel,
            self._check_p8_tesla,             # Tesla split by amount
            self._check_p8b_auto_lease,       # vehicle financing (specific)
            self._check_p8c_auto_insurance,   # vehicle insurance (specific)
            self._check_p9_auto_repair,       # other dealerships / auto service
            self._check_p10_charging,
            self._check_p11_gas,
            self._check_p12_parking,
            self._check_p13_transit,
            self._check_p14_phone,            # before Utilities
            self._check_client_meals,         # before Restaurants
            self._check_p15_restaurants,
            self._check_p16_groceries,
            self._check_p17_medical,          # before Personal Care (dental→medical)
            self._check_p18_personal_care,
            self._check_p19_utilities,
            self._check_client_entertainment,  # before Entertainment
            self._check_p20_entertainment,
            self._check_p21_meals_and_entertainment,  # catch-all for hospitality
            self._check_personal,              # before Online Shopping
            self._check_p22_online_shopping,
        ):
            result = checker(row)
            if result is not None:
                return result
        return self._check_fallback(row)

    # ------------------------------------------------------------------
    # Priority checkers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_p0_exclude(row: StandardRow) -> ClassificationResult | None:
        if row.row_type == "separator":
            return ClassificationResult(exclude=True, rule_hit="P0-separator")
        if row.row_type == "opening_balance":
            return ClassificationResult(
                exclude=True,
                detail="Previous Balance",
                rule_hit="P0-previous_balance",
            )
        desc = row.description
        if _PAT_CARD_PAYMENT.search(desc):
            return ClassificationResult(
                exclude=True, detail="Card payment", rule_hit="P0-card_payment",
            )
        if _PAT_CASH_BACK_REWARD.search(desc):
            return ClassificationResult(
                exclude=True, detail="Cash back reward", rule_hit="P0-cash_back_reward",
            )
        return None

    @staticmethod
    def _check_p1_refund(row: StandardRow) -> ClassificationResult | None:
        """Any non-excluded row with a negative amount is a merchant refund."""
        if row.withdrawals_float < 0:
            return ClassificationResult(
                category=CCCategory.REFUND.value,
                detail="",
                rule_hit="P1-refund",
                need_review=True,  # user policy: refunds always need review
            )
        return None

    @staticmethod
    def _check_p2_interest(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_INTEREST.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.INTEREST.value,
                detail=m.group(0),
                rule_hit="P2-interest",
            )
        return None

    @staticmethod
    def _check_p3_fine(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_FINE.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.FINE.value,
                detail=m.group(0),
                rule_hit="P3-fine",
            )
        return None

    @staticmethod
    def _check_p4_annual_licence_fees(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_ANNUAL_LICENCE_FEES.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.ANNUAL_LICENCE_FEES.value,
                detail=m.group(0),
                rule_hit="P4-annual_licence_fees",
            )
        return None

    @staticmethod
    def _check_p4b_professional_training(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_PROFESSIONAL_TRAINING.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.PROFESSIONAL_TRAINING.value,
                detail=m.group(0),
                rule_hit="P4b-professional_training",
            )
        return None

    @staticmethod
    def _check_p5_subscriptions(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_SUBSCRIPTIONS.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.SUBSCRIPTIONS.value,
                detail=m.group(0),
                rule_hit="P5-subscriptions",
            )
        return None

    @staticmethod
    def _check_p6_gift(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_GIFT.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.GIFT.value,
                detail=m.group(0),
                rule_hit="P6-gift",
            )
        return None

    @staticmethod
    def _check_p7_travel(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_TRAVEL.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.TRAVEL.value,
                detail=m.group(0),
                rule_hit="P7-travel",
            )
        return None

    def _check_p8_tesla(self, row: StandardRow) -> ClassificationResult | None:
        """Tesla split: ≤ threshold → Charging, > threshold → Auto Repair."""
        if not _PAT_TESLA.search(row.description):
            return None
        amount = row.withdrawals_float
        if amount <= self._tesla_threshold:
            return ClassificationResult(
                category=CCCategory.ELECTRICITY_FOR_VEHICLE.value,
                detail=f"Tesla ≤${self._tesla_threshold:.0f}",
                rule_hit="P8-tesla_charging",
            )
        return ClassificationResult(
            category=CCCategory.AUTO_REPAIR.value,
            detail=f"Tesla >${self._tesla_threshold:.0f}",
            rule_hit="P8-tesla_repair",
        )

    @staticmethod
    def _check_p8b_auto_lease(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_AUTO_LEASE.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.AUTO_LEASE.value,
                detail=m.group(0),
                rule_hit="P8b-auto_lease",
            )
        return None

    @staticmethod
    def _check_p8c_auto_insurance(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_AUTO_INSURANCE.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.AUTO_INSURANCE.value,
                detail=m.group(0),
                rule_hit="P8c-auto_insurance",
            )
        return None

    @staticmethod
    def _check_p9_auto_repair(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_AUTO_REPAIR.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.AUTO_REPAIR.value,
                detail=m.group(0),
                rule_hit="P9-auto_repair",
            )
        return None

    @staticmethod
    def _check_p10_charging(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_CHARGING_NON_TESLA.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.ELECTRICITY_FOR_VEHICLE.value,
                detail=m.group(0),
                rule_hit="P10-charging",
            )
        return None

    @staticmethod
    def _check_client_meals(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_CLIENT_MEALS.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.CLIENT_MEALS.value,
                detail=m.group(0),
                rule_hit="P14b-client_meals",
            )
        return None

    @staticmethod
    def _check_client_entertainment(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_CLIENT_ENTERTAINMENT.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.CLIENT_ENTERTAINMENT.value,
                detail=m.group(0),
                rule_hit="P19b-client_entertainment",
            )
        return None

    @staticmethod
    def _check_personal(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_PERSONAL.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.PERSONAL.value,
                detail=m.group(0),
                rule_hit="P21b-personal",
            )
        return None

    @staticmethod
    def _check_p11_gas(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_GAS.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.GAS.value,
                detail=m.group(0),
                rule_hit="P11-gas",
            )
        return None

    @staticmethod
    def _check_p12_parking(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_PARKING.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.PARKING.value,
                detail=m.group(0),
                rule_hit="P12-parking",
            )
        return None

    @staticmethod
    def _check_p13_transit(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_TRANSIT.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.TRANSIT.value,
                detail=m.group(0),
                rule_hit="P13-transit",
            )
        return None

    @staticmethod
    def _check_p14_phone(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_PHONE.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.PHONE.value,
                detail=m.group(0),
                rule_hit="P14-phone",
            )
        return None

    def _check_p15_restaurants(self, row: StandardRow) -> ClassificationResult | None:
        m = _PAT_RESTAURANTS.search(row.description)
        if not m:
            return None
        # < threshold 餐厅默认按个人消费处理。仍 flag need_review 以便手动翻转
        # 为 Client Meals 可抵扣。Client Meals 硬编码命中的商户在 P14b 之前
        # 已拦截；此处只处理未硬编码的普通餐厅。
        thresh = self._restaurant_threshold
        if 0 < row.withdrawals_float < thresh:
            return ClassificationResult(
                category=CCCategory.PERSONAL.value,
                detail=f"Restaurant <${thresh:.0f} ({m.group(0)})",
                rule_hit="P15-restaurant_small_personal",
                need_review=True,
            )
        return ClassificationResult(
            category=CCCategory.RESTAURANTS.value,
            detail=m.group(0),
            rule_hit="P15-restaurants",
        )

    @staticmethod
    def _check_p16_groceries(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_GROCERIES.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.GROCERIES.value,
                detail=m.group(0),
                rule_hit="P16-groceries",
            )
        return None

    @staticmethod
    def _check_p17_medical(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_MEDICAL.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.MEDICAL.value,
                detail=m.group(0),
                rule_hit="P17-medical",
            )
        return None

    @staticmethod
    def _check_p18_personal_care(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_PERSONAL_CARE.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.PERSONAL_CARE.value,
                detail=m.group(0),
                rule_hit="P18-personal_care",
            )
        return None

    @staticmethod
    def _check_p19_utilities(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_UTILITIES.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.UTILITIES.value,
                detail=m.group(0),
                rule_hit="P19-utilities",
            )
        return None

    @staticmethod
    def _check_p20_entertainment(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_ENTERTAINMENT.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.ENTERTAINMENT.value,
                detail=m.group(0),
                rule_hit="P20-entertainment",
            )
        return None

    @staticmethod
    def _check_p21_meals_and_entertainment(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_MEALS_AND_ENTERTAINMENT.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.MEALS_AND_ENTERTAINMENT.value,
                detail=m.group(0),
                rule_hit="P21-meals_and_entertainment",
            )
        return None

    @staticmethod
    def _check_p22_online_shopping(row: StandardRow) -> ClassificationResult | None:
        m = _PAT_ONLINE_SHOPPING.search(row.description)
        if m:
            return ClassificationResult(
                category=CCCategory.ONLINE_SHOPPING.value,
                detail=m.group(0),
                rule_hit="P22-online_shopping",
            )
        return None

    @staticmethod
    def _check_fallback(row: StandardRow) -> ClassificationResult:
        return ClassificationResult(
            category=CCCategory.OTHER_EXPENSE.value,
            detail="",
            rule_hit="P23-fallback",
            need_review=True,
        )
