"""How each property sale is classified, for the analytics layer.

Every sale gets exactly one sale_class. The rules, first match wins:

  1. An estate deed (ESTATE_DEEDS) passing property for nothing: coded love
     and affection (3) or priced at NOMINAL_PRICE or less -> estate_family.
     A missing price is not nominal.
  2. Any other estate deed, or sale code 37 (ESTATE SALE) -> estate_sale.
  3. Otherwise the class mapped from the sale code (SALE_CLASSES); a code not
     in the mapping, or no code at all, -> unclassified.

The sale code is the key, not its description: 47 codes share 27
descriptions, many of them "OTHER".

Changing the mapping (engineer only, in code, reviewed through git):
  - A new county code: add it to SALE_CLASSES. Until then its sales are
    unclassified and the sale_code_unmapped check flags them and alerts.
  - Adding or removing a code changes that check's version, which records a
    'definition_changed' notice instead of an alert. That is by design.
"""
from __future__ import annotations

ESTATE_DEEDS = frozenset({
    "FD",  # FIDUCIARY DEED
    "EX",  # EXECUTORS DEED
    "AD",  # ADMIN DEED
    "TD",  # TRUSTEE DEED
    "SV",  # SURVIVORSHIP (usually spouse to spouse, but it follows a death)
    "AF",  # AFFIDAVIT OF SURVIVORSHIP
    "TO",  # TRANSFER ON DEATH DEED
    "WB",  # WILL BOOK
})

NOMINAL_PRICE = 10
NOT_ANALYZED_CODE = "AA"

_FAMILY_CODE = "3"
_ESTATE_SALE_CODE = "37"

SALE_CLASSES: dict[str, str] = {
    "3": "family",           # LOVE AND AFFECTION SALE
    "37": "estate_sale",     # ESTATE SALE
    "0": "market",           # VALID SALE
    "PV": "market",          # CLEAN AND GREEN VALID PREFERENTIAL
    "1": "distressed",       # SHERIFF SALE
    "2": "distressed",       # CITY TREASURER SALE
    "32": "distressed",      # COURT ORDERED SALE
    "33": "distressed",      # SALE OF PREVIOUS FORECLOSURE - INVALID
    "34": "distressed",      # SHORT SALE - INVALID
    "BK": "distressed",      # BANK/FINANCIAL INSTITUTION
    **{code: "other" for code in (
        "H",    # MULTI-PARCEL SALE
        "14",   # TIME ON MARKET (INSUFF/EXCESS)
        "36",   # QUIT CLAIM
        "9",    # OTHER INVALID SALES INDICATED
        "99",   # CORRECTIVE DEED / DUPLICATE SALE
        "AA",   # SALE NOT ANALYZED
        "UR", "Z", "U", "28", "ND", "5", "X", "29", "VB", "V", "XX", "0X",
        "S", "4", "8", "50", "UX", "VH", "PX", "YY", "21",   # all "OTHER"
        "35",   # CORPORATION TRANSFER
        "GV",   # GOVERNMENT SALE
        "16",   # BUILDING NOT YET ASSESSED
        "DT",   # DATE OF TRANSFER (RECORD YR <> SALE YR)
        "27",   # NO ASSESSED VALUATION
        "N",    # NOT APPLICABLE
        "13",   # EXEMPT BUYER OR SELLER
        "PA",   # PREFERENTIAL ASMT
        "6",    # CHANGED AFTER SALE
        "19",   # INCLUDES PERSONAL PROPERTY
    )},
}


def _literals(values) -> str:
    return ", ".join(f"'{v}'" for v in sorted(values))


def mapped_codes_sql() -> str:
    """The mapped sale codes as a sorted SQL literal list: '0', '1', ..."""
    return _literals(SALE_CLASSES)


def sale_class_sql(sale_code: str = "sale_code", instrument_type: str = "instrument_type",
                   price: str = "price") -> str:
    """A SQL CASE expression giving the sale_class of a row."""
    estate_deed = f"{instrument_type} IN ({_literals(ESTATE_DEEDS)})"
    mapped = "\n        ".join(f"WHEN {sale_code} = '{code}' THEN '{cls}'"
                               for code, cls in sorted(SALE_CLASSES.items()))
    return f"""CASE
        WHEN {estate_deed} AND ({sale_code} = '{_FAMILY_CODE}' OR {price} <= {NOMINAL_PRICE})
            THEN 'estate_family'
        WHEN {estate_deed} OR {sale_code} = '{_ESTATE_SALE_CODE}' THEN 'estate_sale'
        {mapped}
        ELSE 'unclassified'
    END"""
