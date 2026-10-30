"""Build the analytics star schema and the family transfers output table.

    python -m wprdc.analytics

Run after python -m wprdc.transform. Every table in the analytics schema is
rebuilt in full from clean.property_sales: the fact table analytics.sales
(one row per sale, with its sale_class from wprdc/sale_classes.py) and the
parcel, municipality, school_district, sale_type and instrument_type
dimensions. The output table is the view analytics.family_transfers_by_year.

Everything happens in one transaction: if any step fails, the run is recorded
as failed and the previous analytics tables stay. The same clean data always
gives the same tables.
"""
from __future__ import annotations

import argparse

import duckdb

from wprdc.db import connect, run_context
from wprdc.sale_classes import ESTATE_DEEDS, NOT_ANALYZED_CODE, SALE_CLASSES, sale_class_sql
from wprdc.transform import CLEAN

SALES = "analytics.sales"
OUTPUT = "analytics.family_transfers_by_year"
BUILD_INFO = "analytics.build_info"
TABLES = (BUILD_INFO, "analytics.municipality", "analytics.school_district", "analytics.parcel",
          "analytics.sale_type", "analytics.instrument_type", SALES)

# "1st Ward - PITTSBURGH" -> PITTSBURGH. Matched on the whole name: code 911 is
# Forward, a township.
WARD_PATTERN = r"^\d+(st|nd|rd|th) Ward\s*-\s*(.+)$"

PARCEL_COLUMNS = ("full_address", "house_number", "house_fraction", "street_direction", "street",
                  "street_suffix", "unit_desc", "unit_no", "city", "state", "zip",
                  "municipality_code", "school_code")


class AnalyticsError(RuntimeError):
    pass


def _descriptions(code: str, desc: str) -> str:
    """Each non-null code with its most frequent description (ties: alphabetical first)."""
    return f"""
        SELECT {code}, {desc} FROM (
            SELECT {code}, {desc}, count(*) AS n FROM {CLEAN}
            WHERE {code} IS NOT NULL GROUP BY ALL)
        QUALIFY row_number() OVER (PARTITION BY {code} ORDER BY n DESC, {desc}) = 1"""


def _build_build_info(con: duckdb.DuckDBPyConnection) -> None:
    """The snapshot this build came from. The output view reads its cutoff year
    here, not from the clean layer, so a later transform without an analytics
    rebuild does not change the output."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {BUILD_INFO} AS
        SELECT max(snapshot_id) AS snapshot_id, max(download_date) AS download_date FROM {CLEAN}
        """)


def _build_municipality(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE analytics.municipality AS
        WITH m AS (
            SELECT municipality_code,
                   regexp_replace(trim(municipality), '\\s+', ' ', 'g') AS municipality
            FROM ({_descriptions("municipality_code", "municipality")})
        )
        SELECT municipality_code, municipality,
               regexp_full_match(municipality, '{WARD_PATTERN}') AS is_ward,
               CASE WHEN regexp_full_match(municipality, '{WARD_PATTERN}')
                    THEN trim(regexp_extract(municipality, '{WARD_PATTERN}', 2))
                    ELSE municipality END AS municipality_rollup
        FROM m ORDER BY municipality_code
        """)


def _build_school_district(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE analytics.school_district AS
        {_descriptions("school_code", "school_district")}
        ORDER BY school_code
        """)


def _build_parcel(con: duckdb.DuckDBPyConnection) -> None:
    """One row per PARID, its attributes taken from its latest sale."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE analytics.parcel AS
        SELECT PARID, {", ".join(PARCEL_COLUMNS)}
        FROM {CLEAN}
        QUALIFY row_number() OVER (
            PARTITION BY PARID
            ORDER BY sale_date DESC NULLS LAST, record_date DESC NULLS LAST,
                     sale_key DESC, source_row_number DESC) = 1
        ORDER BY PARID
        """)


def _build_sale_type(con: duckdb.DuckDBPyConnection) -> None:
    mapping = ", ".join(f"('{code}', '{cls}')" for code, cls in sorted(SALE_CLASSES.items()))
    con.execute(
        f"""
        CREATE OR REPLACE TABLE analytics.sale_type AS
        WITH mapped(sale_code, sale_class) AS (VALUES {mapping}),
        described AS ({_descriptions("sale_code", "sale_desc")})
        SELECT sale_code, d.sale_desc, coalesce(m.sale_class, 'unclassified') AS sale_class
        FROM mapped m FULL OUTER JOIN described d USING (sale_code)
        ORDER BY sale_code
        """)


def _build_instrument_type(con: duckdb.DuckDBPyConnection) -> None:
    estate = ", ".join(f"'{t}'" for t in sorted(ESTATE_DEEDS))
    con.execute(
        f"""
        CREATE OR REPLACE TABLE analytics.instrument_type AS
        SELECT instrument_type, instrument_desc, instrument_type IN ({estate}) AS is_estate_deed
        FROM ({_descriptions("instrument_type", "instrument_desc")})
        ORDER BY instrument_type
        """)


def _build_sales(con: duckdb.DuckDBPyConnection) -> int:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {SALES} AS
        SELECT sale_key, PARID, DEEDBOOK, DEEDPAGE, sale_date, year(sale_date) AS sale_year,
               record_date, price, sale_code, instrument_type,
               {sale_class_sql()} AS sale_class,
               dq_flags
        FROM {CLEAN}
        ORDER BY sale_key, source_row_number
        """)
    return con.execute(f"SELECT count(*) FROM {SALES}").fetchone()[0]


def _create_output_view(con: duckdb.DuckDBPyConnection) -> None:
    """Complete years only: those before the year the clean data was downloaded."""
    con.execute(
        f"""
        CREATE OR REPLACE VIEW {OUTPUT} AS
        WITH y AS (
            SELECT sale_year,
                   count(*) AS all_transfers,
                   count(*) FILTER (WHERE sale_class = 'family') AS family_transfers,
                   count(*) FILTER (WHERE sale_class = 'estate_family') AS estate_family_transfers,
                   count(*) FILTER (WHERE sale_code = '{NOT_ANALYZED_CODE}') AS not_analyzed_transfers
            FROM {SALES}
            WHERE sale_year < (SELECT year(download_date) FROM {BUILD_INFO})
            GROUP BY sale_year
        )
        SELECT sale_year, all_transfers,
               family_transfers, round(family_transfers / all_transfers, 4) AS family_share,
               estate_family_transfers,
               round(estate_family_transfers / all_transfers, 4) AS estate_family_share,
               not_analyzed_transfers
        FROM y ORDER BY sale_year
        """)


def build_analytics(con: duckdb.DuckDBPyConnection) -> dict:
    schema, table = CLEAN.split(".")
    exists = con.execute("SELECT count(*) FROM information_schema.tables "
                         "WHERE table_schema = ? AND table_name = ?", [schema, table]).fetchone()[0]
    if not exists:
        raise AnalyticsError(f"{CLEAN} does not exist; run python -m wprdc.transform first")
    con.begin()
    try:
        _build_build_info(con)
        _build_municipality(con)
        _build_school_district(con)
        _build_parcel(con)
        _build_sale_type(con)
        _build_instrument_type(con)
        n_sales = _build_sales(con)
        _create_output_view(con)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return {"n_sales": n_sales}


def format_output(rows: list[tuple]) -> str:
    """The output view as a plain aligned table."""
    header = ("year", "all", "family", "share", "estate_family", "share", "not_analyzed")
    lines = [f"{header[0]:<6}{header[1]:>9}{header[2]:>9}{header[3]:>8}"
             f"{header[4]:>15}{header[5]:>8}{header[6]:>14}"]
    for year, n, fam, fam_share, est, est_share, na in rows:
        lines.append(f"{year:<6}{n:>9,}{fam:>9,}{fam_share:>8.1%}{est:>15,}{est_share:>8.1%}{na:>14,}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    con = connect()
    try:
        with run_context(con, "analytics"):
            result = build_analytics(con)
        print(f"{result['n_sales']:,} sales in {SALES}\n")
        print(f"{OUTPUT}:")
        print(format_output(con.execute(f"SELECT * FROM {OUTPUT}").fetchall()))
    finally:
        con.close()


if __name__ == "__main__":
    main()
