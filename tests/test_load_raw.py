from datetime import timedelta

import pytest

from wprdc.db import connect, utcnow
from wprdc.load_raw import SOURCE_COLUMNS, SchemaDriftError, load_pending, prune, rebuild

ROW = ['16966258', '1010G00075000000', '"0 HILL ST, SOUTH PARK, PA 15129"', '0', ' ', '',
       'HILL', 'ST', '', '', 'SOUTH PARK', 'PA', '15129', '37', 'South Park', '945',
       'South Park  ', '2022-07-06', '2022-06-21', '1.0', '18965', '534', 'H',
       'MULTI-PARCEL SALE', 'DE', 'DEED']
DEEDPAGE = SOURCE_COLUMNS.index("DEEDPAGE")
T0 = utcnow()


def write_dump(path, deedpages, header=SOURCE_COLUMNS, id_start=16966258):
    """One row per deedpage value, so each row has distinct content.

    Mirrors WPRDC's quirk: CRLF after the header, LF after data rows.
    """
    lines = [",".join(header) + "\r\n"]
    for i, page in enumerate(deedpages):
        row = list(ROW)
        row[0], row[DEEDPAGE] = str(id_start + i), str(page)
        lines.append(",".join(row) + "\n")
    path.write_bytes("".join(lines).encode())


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "test.duckdb")
    yield c
    c.close()


def add_fetch(con, path, sha, rows, run_id="run-1", minutes=0):
    con.execute(
        """INSERT INTO ops.fetch_resource (run_id, resource_id, strategy, package_id,
               status, raw_path, raw_sha256, raw_rows, started_at, finished_at)
           VALUES (?, 'res', 'dump', 'pkg', 'fetched', ?, ?, ?, ?, ?)""",
        [run_id, str(path), sha, rows, T0 + timedelta(minutes=minutes),
         T0 + timedelta(minutes=minutes, seconds=30)])


def fetch_series(con, tmp_path, contents):
    """contents: list of (name, deedpages); identical deedpages => identical bytes => same sha."""
    for i, (name, pages) in enumerate(contents):
        f = tmp_path / f"{name}_{i}.csv"
        write_dump(f, pages)
        add_fetch(con, f, f"sha-{name}", len(pages), run_id=f"run-{i}", minutes=i)
    return load_pending(con)


def test_load_is_idempotent_and_keeps_values_verbatim(con, tmp_path):
    f = tmp_path / "a.csv"
    write_dump(f, [1, 2, 3])
    add_fetch(con, f, "sha-a", 3)
    assert load_pending(con) == [(str(f), "loaded")]
    assert load_pending(con) == []  # nothing pending on re-run

    # Same bytes fetched again in a later run: recorded, not duplicated.
    add_fetch(con, f, "sha-a", 3, run_id="run-2", minutes=1)
    assert load_pending(con) == [(str(f), "already_loaded")]

    assert con.execute("SELECT count(*) FROM raw.property_sales").fetchone() == (3,)
    assert con.execute("SELECT count(*) FROM raw.property_sales_snapshot").fetchone() == (1,)
    assert con.execute(
        "SELECT MUNIDESC, PROPERTYFRACTION, FULL_ADDRESS FROM raw.property_sales LIMIT 1"
    ).fetchone() == ("South Park  ", " ", "0 HILL ST, SOUTH PARK, PA 15129")


def test_row_hash_ignores_id_and_distinguishes_null_from_empty(con, tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    write_dump(a, [1], id_start=100)
    write_dump(b, [1], id_start=999)          # same content, different _id
    add_fetch(con, a, "sha-a", 1)
    add_fetch(con, b, "sha-b", 1, run_id="run-2", minutes=1)
    load_pending(con)
    assert con.execute("SELECT count(DISTINCT row_hash) FROM raw.property_sales").fetchone() == (1,)

    # PROPERTYADDRESSDIR is unquoted-empty in the fixture -> NULL; '' must hash differently.
    null_hash, = con.execute("SELECT md5(concat_ws(chr(31), coalesce(NULL, chr(0)), 'x'))").fetchone()
    empty_hash, = con.execute("SELECT md5(concat_ws(chr(31), coalesce('', chr(0)), 'x'))").fetchone()
    assert null_hash != empty_hash


def test_latest_follows_most_recent_fetch_even_when_content_reverts(con, tmp_path):
    fetch_series(con, tmp_path, [("A", [1, 2]), ("B", [1, 2, 3]), ("A", [1, 2])])
    assert con.execute("SELECT count(*) FROM raw.property_sales_snapshot").fetchone() == (2,)
    assert con.execute(
        "SELECT DISTINCT snapshot_id FROM raw.property_sales_latest").fetchall() == [("sha-A",)]


def test_snapshot_diff_and_changes(con, tmp_path):
    fetch_series(con, tmp_path, [("A", [1, 2, 3]), ("B", [2, 3, 4, 5])])
    diff = con.execute(
        "SELECT change, DEEDPAGE FROM raw.snapshot_diff('sha-A', 'sha-B') ORDER BY 1, 2").fetchall()
    assert diff == [("added", "4"), ("added", "5"), ("removed", "1")]
    assert con.execute(
        "SELECT snapshot_id, prev_snapshot_id, net_change, rows_added, rows_removed "
        "FROM raw.v_snapshot_changes").fetchall() == [("sha-B", "sha-A", 1, 2, 1)]


def test_prune_keeps_latest_and_rebuild_restores_from_csv(con, tmp_path):
    fetch_series(con, tmp_path, [("A", [1]), ("B", [1, 2]), ("C", [1, 2, 3])])
    assert prune(con, keep=1) == ["sha-B", "sha-A"]
    assert con.execute(
        "SELECT snapshot_id, count(*) FROM raw.property_sales GROUP BY 1").fetchall() == [("sha-C", 3)]
    assert con.execute(
        "SELECT count(*) FROM raw.property_sales_snapshot WHERE pruned_at IS NOT NULL").fetchone() == (2,)
    assert prune(con, keep=1) == []  # idempotent

    rebuild(con)
    assert con.execute(
        "SELECT snapshot_id, count(*) FROM raw.property_sales GROUP BY 1 ORDER BY 1").fetchall() == [
        ("sha-A", 1), ("sha-B", 2), ("sha-C", 3)]
    assert con.execute(
        "SELECT count(*) FROM raw.property_sales_snapshot WHERE pruned_at IS NOT NULL").fetchone() == (0,)


def test_rebuild_refuses_if_archive_file_missing(con, tmp_path):
    fetch_series(con, tmp_path, [("A", [1])])
    (tmp_path / "A_0.csv").unlink()
    with pytest.raises(FileNotFoundError):
        rebuild(con)
    assert con.execute("SELECT count(*) FROM raw.property_sales").fetchone() == (1,)


def test_schema_drift_fails_and_loads_nothing(con, tmp_path):
    f = tmp_path / "drift.csv"
    write_dump(f, [1, 2], header=SOURCE_COLUMNS[:-1] + ["NEWCOL"])
    add_fetch(con, f, "sha-d", 2)
    with pytest.raises(SchemaDriftError, match="NEWCOL"):
        load_pending(con)
    assert con.execute("SELECT count(*) FROM raw.property_sales").fetchone() == (0,)


def test_row_count_mismatch_rolls_back(con, tmp_path):
    f = tmp_path / "short.csv"
    write_dump(f, [1, 2])
    add_fetch(con, f, "sha-s", 5)  # landing check claimed 5 rows
    with pytest.raises(RuntimeError, match="loaded 2 rows"):
        load_pending(con)
    assert con.execute("SELECT count(*) FROM raw.property_sales_snapshot").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM raw.property_sales").fetchone() == (0,)
    assert con.execute("SELECT raw_table FROM ops.fetch_resource").fetchone() == (None,)
