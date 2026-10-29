"""Build the cleaned layer from the latest raw snapshot and run the data quality checks.

    python -m wprdc.transform

clean.property_sales_typed holds every row of the snapshot, typed and trimmed,
keyed by (PARID, DEEDBOOK, DEEDPAGE, SALEDATE) as delivered.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import NamedTuple

import duckdb

TYPED = "clean.property_sales_typed"
CLEAN = "clean.property_sales"


class TransformError(RuntimeError):
    pass


class EvalPoint(NamedTuple):
    snapshot_id: str
    as_of: datetime        # the snapshot's last_fetched_at
    download_date: date    # UTC date of its first fetch
    n_rows: int


def _key_part(col: str) -> str:
    return f'coalesce("{col}", chr(0))'


SALE_KEY_SQL = "md5(concat_ws(chr(31), {}))".format(
    ", ".join(_key_part(c) for c in ("PARID", "DEEDBOOK", "DEEDPAGE", "SALEDATE")))


def _no_deed(col: str) -> str:
    return f"regexp_full_match(trim(coalesce({col}, '')), '0*')"


def _text(col: str) -> str:
    return f"nullif(trim({col}), '')"


# Source column -> cleaned column name, for the descriptive text columns.
TEXT_COLUMNS = {
    "FULL_ADDRESS": "full_address",
    "PROPERTYHOUSENUM": "house_number",
    "PROPERTYFRACTION": "house_fraction",
    "PROPERTYADDRESSDIR": "street_direction",
    "PROPERTYADDRESSSTREET": "street",
    "PROPERTYADDRESSSUF": "street_suffix",
    "PROPERTYADDRESSUNITDESC": "unit_desc",
    "PROPERTYUNITNO": "unit_no",
    "PROPERTYCITY": "city",
    "PROPERTYSTATE": "state",
    "PROPERTYZIP": "zip",
    "SCHOOLCODE": "school_code",
    "SCHOOLDESC": "school_district",
    "MUNICODE": "municipality_code",
    "MUNIDESC": "municipality",
    "SALECODE": "sale_code",
    "SALEDESC": "sale_desc",
    "INSTRTYP": "instrument_type",
    "INSTRTYPDESC": "instrument_desc",
}


def build_typed(con: duckdb.DuckDBPyConnection) -> EvalPoint:
    """(Re)build clean.property_sales_typed from the latest snapshot."""
    latest = con.execute(
        """SELECT snapshot_id, last_fetched_at, CAST(first_fetched_at AS DATE), row_count
           FROM raw.v_snapshots WHERE pruned_at IS NULL
           ORDER BY last_fetched_at DESC LIMIT 1""").fetchone()
    if latest is None:
        raise TransformError("no raw snapshot loaded; run python -m wprdc.load_raw first")
    point = EvalPoint(*latest)
    text_cols = ",\n            ".join(f"{_text(src)} AS {dst}" for src, dst in TEXT_COLUMNS.items())
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {TYPED} AS
        SELECT
            snapshot_id, source_row_number, row_hash,
            {SALE_KEY_SQL} AS sale_key,
            PARID, DEEDBOOK, DEEDPAGE, SALEDATE,
            {_text("RECORDDATE")} AS record_date_text,
            TRY_CAST(SALEDATE AS DATE) AS sale_date,
            TRY_CAST(RECORDDATE AS DATE) AS record_date,
            TRY_CAST(TRY_CAST(PRICE AS DECIMAL(18, 2)) AS BIGINT) AS price,
            NOT ({_no_deed("DEEDBOOK")} OR {_no_deed("DEEDPAGE")}) AS has_real_deed,
            CAST(? AS DATE) AS download_date,
            {text_cols}
        FROM raw.property_sales
        WHERE snapshot_id = ?
        """,
        [point.download_date, point.snapshot_id])
    n = con.execute(f"SELECT count(*) FROM {TYPED}").fetchone()[0]
    if n != point.n_rows:
        raise TransformError(f"snapshot {point.snapshot_id[:12]}: typed {n} rows, catalog says {point.n_rows}")
    return point
