"""Build small raw snapshots directly in the database, for transform/dq tests."""
from datetime import datetime, timedelta, timezone

import pytest

from wprdc.db import connect
from wprdc.load_raw import ROW_HASH_SQL, SOURCE_COLUMNS

T0 = datetime(2026, 10, 28, 21, 52, tzinfo=timezone.utc)

# Passes every check: recorded 15 days after the sale, real deed reference.
BASE = {
    "_id": "16966258", "PARID": "1010G00075000000",
    "FULL_ADDRESS": "0 HILL ST, SOUTH PARK, PA 15129", "PROPERTYHOUSENUM": "0",
    "PROPERTYFRACTION": " ", "PROPERTYADDRESSDIR": "", "PROPERTYADDRESSSTREET": "HILL",
    "PROPERTYADDRESSSUF": "ST", "PROPERTYADDRESSUNITDESC": "", "PROPERTYUNITNO": "",
    "PROPERTYCITY": "SOUTH PARK", "PROPERTYSTATE": "PA", "PROPERTYZIP": "15129",
    "SCHOOLCODE": "37", "SCHOOLDESC": "South Park", "MUNICODE": "945",
    "MUNIDESC": "South Park  ", "RECORDDATE": "2022-07-06", "SALEDATE": "2022-06-21",
    "PRICE": "1.0", "DEEDBOOK": "18965", "DEEDPAGE": "534", "SALECODE": "H",
    "SALEDESC": "MULTI-PARCEL SALE", "INSTRTYP": "DE", "INSTRTYPDESC": "DEED",
}


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "test.duckdb")
    yield c
    c.close()


def sale(**overrides) -> dict:
    row = dict(BASE)
    row.update(overrides)
    return row


def _fetch(con, sha: str, minutes: int) -> None:
    at = T0 + timedelta(minutes=minutes)
    con.execute(
        """INSERT INTO ops.fetch_resource (run_id, resource_id, strategy, package_id,
               status, raw_path, raw_sha256, raw_rows, raw_table, started_at, finished_at)
           VALUES (?, 'res', 'dump', 'pkg', 'fetched', ?, ?, 0, 'raw.property_sales', ?, ?)""",
        [f"run-{sha}-{minutes}", f"{sha}.csv", sha, at - timedelta(seconds=30), at])


def make_snapshot(con, sha: str, rows: list[dict], minutes: int = 0) -> str:
    """A loaded snapshot whose (first) fetch finished at T0 + minutes."""
    _fetch(con, sha, minutes)
    con.execute(
        """INSERT INTO raw.property_sales_snapshot
               (snapshot_id, run_id, resource_id, source_path, row_count, loaded_at)
           VALUES (?, ?, 'res', ?, ?, ?)""",
        [sha, f"run-{sha}-{minutes}", f"{sha}.csv", len(rows), T0 + timedelta(minutes=minutes)])
    cols = ", ".join(f'"{c}"' for c in SOURCE_COLUMNS)
    marks = ", ".join("?" * len(SOURCE_COLUMNS))
    for i, row in enumerate(rows, start=1):
        con.execute(
            f"INSERT INTO raw.property_sales (snapshot_id, source_row_number, row_hash, {cols}) "
            f"VALUES (?, ?, '', {marks})",
            [sha, i, *(row[c] for c in SOURCE_COLUMNS)])
    con.execute(f"UPDATE raw.property_sales SET row_hash = {ROW_HASH_SQL} WHERE snapshot_id = ?", [sha])
    return sha


def refetch(con, sha: str, minutes: int) -> None:
    """The same bytes fetched again later (the source went back to an earlier state)."""
    _fetch(con, sha, minutes)
