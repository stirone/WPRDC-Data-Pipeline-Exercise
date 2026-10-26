"""Load landed dump files into raw.property_sales as immutable snapshots.

    python -m wprdc.load_raw

Picks up every fetched-but-unloaded dump in ops.fetch_resource. Each distinct
file (by sha256) becomes one snapshot, loaded in a single transaction, so a
re-run is a no-op and a half-loaded snapshot cannot exist.
"""
from __future__ import annotations

import csv
from pathlib import Path

import duckdb

from wprdc import config
from wprdc.db import connect, utcnow

RAW_TABLE = "raw.property_sales"
SOURCE_COLUMNS = [
    "_id", "PARID", "FULL_ADDRESS", "PROPERTYHOUSENUM", "PROPERTYFRACTION",
    "PROPERTYADDRESSDIR", "PROPERTYADDRESSSTREET", "PROPERTYADDRESSSUF",
    "PROPERTYADDRESSUNITDESC", "PROPERTYUNITNO", "PROPERTYCITY", "PROPERTYSTATE",
    "PROPERTYZIP", "SCHOOLCODE", "SCHOOLDESC", "MUNICODE", "MUNIDESC",
    "RECORDDATE", "SALEDATE", "PRICE", "DEEDBOOK", "DEEDPAGE", "SALECODE",
    "SALEDESC", "INSTRTYP", "INSTRTYPDESC",
]


class SchemaDriftError(RuntimeError):
    pass


def check_header(path: Path) -> None:
    """Fail loudly if WPRDC adds, drops, renames or reorders columns."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        header = next(csv.reader(fh))
    if header != SOURCE_COLUMNS:
        added = [c for c in header if c not in SOURCE_COLUMNS]
        missing = [c for c in SOURCE_COLUMNS if c not in header]
        raise SchemaDriftError(
            f"{path.name}: header changed (added={added}, missing={missing}, "
            f"order_changed={not added and not missing})")


def _pending(con: duckdb.DuckDBPyConnection):
    return con.execute(
        """
        SELECT run_id, resource_id, raw_path, raw_sha256, raw_rows, ckan_last_modified
        FROM ops.fetch_resource
        WHERE status = 'fetched' AND strategy = 'dump' AND raw_table IS NULL
        ORDER BY started_at
        """
    ).fetchall()


def load_snapshot(con: duckdb.DuckDBPyConnection, run_id: str, resource_id: str,
                  raw_path: str, sha: str, expected_rows: int, ckan_last_modified) -> str:
    """Load one landed file; returns 'loaded' or 'already_loaded'."""
    mark_loaded = (
        "UPDATE ops.fetch_resource SET raw_table = ? "
        "WHERE run_id = ? AND resource_id = ? AND strategy = 'dump'"
    )
    if con.execute("SELECT 1 FROM raw.property_sales_snapshot WHERE snapshot_id = ?",
                   [sha]).fetchone():
        con.execute(mark_loaded, [RAW_TABLE, run_id, resource_id])
        return "already_loaded"

    path = config.PROJECT_ROOT / raw_path
    check_header(path)
    cols = ", ".join(f'"{c}"' for c in SOURCE_COLUMNS)
    con.begin()
    try:
        con.execute(
            "INSERT INTO raw.property_sales_snapshot VALUES (?, ?, ?, ?, ?, ?, ?)",
            [sha, run_id, resource_id, raw_path, ckan_last_modified, expected_rows, utcnow()],
        )
        # strict_mode=false because the header line ends CRLF and data rows LF;
        # ingest's csv-module check has already verified every row's width.
        con.execute(
            f"""
            INSERT INTO {RAW_TABLE} (snapshot_id, source_row_number, {cols})
            SELECT ?, row_number() OVER (), {cols}
            FROM read_csv(?, header = true, all_varchar = true, strict_mode = false,
                          delim = ',', quote = '"', escape = '"')
            """,
            [sha, str(path)],
        )
        loaded = con.execute(f"SELECT count(*) FROM {RAW_TABLE} WHERE snapshot_id = ?",
                             [sha]).fetchone()[0]
        if loaded != expected_rows:
            raise RuntimeError(f"{raw_path}: loaded {loaded} rows, landing check counted {expected_rows}")
        con.execute(mark_loaded, [RAW_TABLE, run_id, resource_id])
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return "loaded"


def load_pending(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    results = []
    for run_id, resource_id, raw_path, sha, rows, last_mod in _pending(con):
        status = load_snapshot(con, run_id, resource_id, raw_path, sha, rows, last_mod)
        print(f"{status}: {raw_path} ({rows:,} rows, snapshot {sha[:12]})")
        results.append((raw_path, status))
    if not results:
        print("nothing to load")
    return results


def main() -> None:
    con = connect()
    try:
        load_pending(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
