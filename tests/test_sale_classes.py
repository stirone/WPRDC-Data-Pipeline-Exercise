import duckdb
import pytest

from wprdc.sale_classes import ESTATE_DEEDS, SALE_CLASSES, mapped_codes_sql, sale_class_sql

CLASSES = {"estate_family", "estate_sale", "family", "market", "distressed", "other"}


def classify(code, instr, price):
    return duckdb.connect().execute(
        f"SELECT {sale_class_sql('c', 'i', 'p')} FROM (SELECT ?::VARCHAR c, ?::VARCHAR i, ?::BIGINT p)",
        [code, instr, price]).fetchone()[0]


def test_mapping_covers_all_codes():
    assert len(SALE_CLASSES) == 47
    assert set(SALE_CLASSES.values()) <= CLASSES
    assert SALE_CLASSES["3"] == "family"
    assert SALE_CLASSES["37"] == "estate_sale"
    assert SALE_CLASSES["0"] == SALE_CLASSES["PV"] == "market"
    assert {c for c, k in SALE_CLASSES.items() if k == "distressed"} == {"1", "2", "32", "33", "34", "BK"}


def test_estate_deeds():
    assert ESTATE_DEEDS == {"FD", "EX", "AD", "TD", "SV", "AF", "TO", "WB"}


@pytest.mark.parametrize("code, instr, price, expected", [
    ("3", "FD", 1, "estate_family"),
    ("0", "EX", 10, "estate_family"),
    ("0", "FD", 150000, "estate_sale"),
    ("0", "SV", None, "estate_sale"),
    ("3", "FD", None, "estate_family"),
    ("37", "DE", 1, "estate_sale"),
    ("3", "DE", 1, "family"),
    ("3", None, 1, "family"),
    ("0", "DE", 200000, "market"),
    ("1", "SD", 5000, "distressed"),
    ("H", "DE", 1, "other"),
    ("ZZ", "DE", 1, "unclassified"),
    (None, "DE", 1, "unclassified"),
])
def test_classification(code, instr, price, expected):
    assert classify(code, instr, price) == expected


def test_mapped_codes_sql_is_sorted_literal_list():
    # Sorted, so the sale_code_unmapped check's version only changes with the mapping.
    assert mapped_codes_sql() == ", ".join(f"'{c}'" for c in sorted(SALE_CLASSES))
    assert "'3'" in mapped_codes_sql()
