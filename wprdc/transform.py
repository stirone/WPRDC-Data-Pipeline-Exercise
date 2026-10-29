"""Build the cleaned layer from the latest raw snapshot and run the data quality checks.

    python -m wprdc.transform

clean.property_sales_typed holds every row of the snapshot, typed and trimmed,
keyed by (PARID, DEEDBOOK, DEEDPAGE, SALEDATE) as delivered. The checks in
wprdc/checks.py run against it; clean.property_sales is the typed rows minus
quarantined ones, with dq_flags listing the flag checks each row fails.

Everything happens in one transaction: if any step fails, the run is recorded
as failed and the previous clean table stays. Alerts are sent after commit.
Re-running on an already-evaluated snapshot changes nothing.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from typing import NamedTuple

import duckdb

from wprdc.checks import CHECKS, Check
from wprdc.db import connect, run_context

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
        """SELECT snapshot_id, last_fetched_at, CAST(first_fetched_at AS DATE), row_count, pruned_at
           FROM raw.v_snapshots
           ORDER BY last_fetched_at DESC LIMIT 1""").fetchone()
    if latest is None:
        raise TransformError("no raw snapshot loaded; run python -m wprdc.load_raw first")
    if latest[4] is not None:
        # The source went back to a snapshot whose rows were pruned. Never fall
        # back to an older snapshot: the clean table would silently be stale.
        raise TransformError(f"latest snapshot {latest[0][:12]} has been pruned; "
                             "run python -m wprdc.load_raw --rebuild")
    point = EvalPoint(*latest[:4])
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


def build_clean(con: duckdb.DuckDBPyConnection, eval_id: int, checks: list[Check]) -> int:
    """(Re)build clean.property_sales from the typed table and eval_id's results.

    Quarantine is per row (source_row_number), not per key, so a key collision
    never drops a row that passes. Actions come from `checks` as defined now.
    """
    from wprdc.dq import current_results  # dq imports this module

    con.execute(
        f"""
        CREATE OR REPLACE TABLE {CLEAN} AS
        WITH cur AS ({current_results(eval_id, checks)}),
        flags AS (
            SELECT source_row_number, list(check_id ORDER BY check_id) AS dq_flags
            FROM cur WHERE action = 'flag' GROUP BY 1
        )
        SELECT t.*, coalesce(f.dq_flags, []::VARCHAR[]) AS dq_flags
        FROM {TYPED} t
        LEFT JOIN flags f USING (source_row_number)
        WHERE t.source_row_number NOT IN (SELECT source_row_number FROM cur WHERE action = 'quarantine')
        ORDER BY t.source_row_number
        """)
    return con.execute(f"SELECT count(*) FROM {CLEAN}").fetchone()[0]


def transform(con: duckdb.DuckDBPyConnection, run_id: str, checks: list[Check] = CHECKS) -> dict:
    from wprdc.dq import evaluate, mark_notified, notify, pending_alerts

    con.begin()
    try:
        point = build_typed(con)
        eval_id = evaluate(con, run_id, point, checks)
        n_clean = build_clean(con, eval_id, checks)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    alerts = pending_alerts(con)
    for alert in alerts:
        notify(alert)
        mark_notified(con, alert["alert_id"])
    return {"eval_id": eval_id, "snapshot_id": point.snapshot_id, "n_rows": point.n_rows,
            "n_clean": n_clean, "alerts": alerts}


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    con = connect()
    try:
        with run_context(con, "transform") as run_id:
            result = transform(con, run_id)
        counts = dict(con.execute(
            "SELECT check_id || ':' || check_version, n_failing FROM dq.check_run WHERE eval_id = ?",
            [result["eval_id"]]).fetchall())
        print(f"snapshot {result['snapshot_id'][:12]}: {result['n_rows']:,} rows, "
              f"{result['n_clean']:,} in {CLEAN}")
        for check in CHECKS:
            if not check.retired:
                print(f"  {check.id:<22} {check.action:<10} {counts.get(f'{check.id}:{check.version}', 0):>8,}")
        print(f"alerts: {len(result['alerts'])}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
