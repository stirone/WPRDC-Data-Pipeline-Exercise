"""Load landed dump files into raw.property_sales as immutable snapshots.

    python -m wprdc.load_raw                  # load any fetched-but-unloaded dumps
    python -m wprdc.load_raw --prune-keep 30  # drop rows of all but the 30 most recent snapshots
    python -m wprdc.load_raw --rebuild        # drop the raw layer and reload it from the CSV archive

Each distinct file (by sha256) becomes one snapshot, loaded in a single
transaction, so a re-run is a no-op and a half-loaded snapshot cannot exist.
The CSVs under data/raw are never modified or deleted by this module.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import duckdb

from wprdc import config
from wprdc.db import apply_schema, connect, utcnow

RAW_TABLE = "raw.property_sales"
SOURCE_COLUMNS = [
    "_id", "PARID", "FULL_ADDRESS", "PROPERTYHOUSENUM", "PROPERTYFRACTION",
    "PROPERTYADDRESSDIR", "PROPERTYADDRESSSTREET", "PROPERTYADDRESSSUF",
    "PROPERTYADDRESSUNITDESC", "PROPERTYUNITNO", "PROPERTYCITY", "PROPERTYSTATE",
    "PROPERTYZIP", "SCHOOLCODE", "SCHOOLDESC", "MUNICODE", "MUNIDESC",
    "RECORDDATE", "SALEDATE", "PRICE", "DEEDBOOK", "DEEDPAGE", "SALECODE",
    "SALEDESC", "INSTRTYP", "INSTRTYPDESC",
]
# _id is excluded: WPRDC reassigns it every time the datastore is rebuilt.
# NULL is encoded as chr(0) so it hashes differently from an empty string, and
# values are joined with chr(31) (unit separator), which never occurs in the data.
ROW_HASH_SQL = "md5(concat_ws(chr(31), {}))".format(
    ", ".join(f'coalesce("{c}", chr(0))' for c in SOURCE_COLUMNS if c != "_id"))


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
            "INSERT INTO raw.property_sales_snapshot "
            "(snapshot_id, run_id, resource_id, source_path, ckan_last_modified, row_count, loaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [sha, run_id, resource_id, raw_path, ckan_last_modified, expected_rows, utcnow()],
        )
        # strict_mode=false because the header line ends CRLF and data rows LF;
        # ingest's csv-module check has already verified every row's width.
        con.execute(
            f"""
            INSERT INTO {RAW_TABLE} (snapshot_id, source_row_number, row_hash, {cols})
            SELECT ?, row_number() OVER (), {ROW_HASH_SQL}, {cols}
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


def prune(con: duckdb.DuckDBPyConnection, keep: int) -> list[str]:
    """Delete rows of all but the `keep` most recently fetched snapshots.

    The snapshot catalog row stays (marked pruned_at) and the CSV stays on
    disk, so --rebuild brings everything back.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1; the latest snapshot is never pruned")
    victims = [r[0] for r in con.execute(
        """
        SELECT snapshot_id FROM raw.v_snapshots
        WHERE pruned_at IS NULL
        QUALIFY row_number() OVER (ORDER BY last_fetched_at DESC) > ?
        """, [keep]).fetchall()]
    for snapshot_id in victims:
        con.begin()
        try:
            con.execute(f"DELETE FROM {RAW_TABLE} WHERE snapshot_id = ?", [snapshot_id])
            con.execute("UPDATE raw.property_sales_snapshot SET pruned_at = ? WHERE snapshot_id = ?",
                        [utcnow(), snapshot_id])
            con.commit()
        except BaseException:
            con.rollback()
            raise
        print(f"pruned snapshot {snapshot_id[:12]} (CSV kept)")
    if not victims:
        print("nothing to prune")
    return victims


def rebuild(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """Drop the raw layer and reload every fetched dump from the CSV archive.

    Not one transaction (each snapshot loads in its own); if interrupted,
    simply run --rebuild again.
    """
    missing = [p for (p,) in con.execute(
        "SELECT DISTINCT raw_path FROM ops.fetch_resource "
        "WHERE strategy = 'dump' AND status = 'fetched'").fetchall()
        if not (config.PROJECT_ROOT / p).exists()]
    if missing:
        raise FileNotFoundError(f"archive files missing, refusing to rebuild: {missing}")
    con.execute("DROP SCHEMA IF EXISTS raw CASCADE")
    apply_schema(con)
    con.execute("UPDATE ops.fetch_resource SET raw_table = NULL "
                "WHERE strategy = 'dump' AND status = 'fetched'")
    return load_pending(con)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--prune-keep", type=int, metavar="N",
                       help="keep only the N most recently fetched snapshots in the DB")
    group.add_argument("--rebuild", action="store_true",
                       help="drop raw.* and reload every fetched dump from data/raw")
    args = ap.parse_args(argv)

    con = connect()
    try:
        if args.rebuild:
            rebuild(con)
        elif args.prune_keep is not None:
            prune(con, args.prune_keep)
        else:
            load_pending(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
