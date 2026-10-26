import pytest

from wprdc.db import connect, utcnow
from wprdc.load_raw import SOURCE_COLUMNS, SchemaDriftError, load_pending

ROW = ['16966258', '1010G00075000000', '"0 HILL ST, SOUTH PARK, PA 15129"', '0', ' ', '',
       'HILL', 'ST', '', '', 'SOUTH PARK', 'PA', '15129', '37', 'South Park', '945',
       'South Park  ', '2022-07-06', '2022-06-21', '1.0', '18965', '534', 'H',
       'MULTI-PARCEL SALE', 'DE', 'DEED']


def write_dump(path, n_rows, header=SOURCE_COLUMNS):
    # Mirrors WPRDC's quirk: CRLF after the header, LF after data rows.
    lines = [",".join(header) + "\r\n"]
    for i in range(n_rows):
        lines.append(",".join([str(16966258 + i)] + ROW[1:]) + "\n")
    path.write_bytes("".join(lines).encode())


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "test.duckdb")
    yield c
    c.close()


def add_fetch(con, path, sha, rows, run_id="run-1"):
    con.execute(
        """INSERT INTO ops.fetch_resource (run_id, resource_id, strategy, package_id,
               status, raw_path, raw_sha256, raw_rows, started_at)
           VALUES (?, 'res', 'dump', 'pkg', 'fetched', ?, ?, ?, ?)""",
        [run_id, str(path), sha, rows, utcnow()])


def test_load_is_idempotent_and_keeps_values_verbatim(con, tmp_path):
    f = tmp_path / "a.csv"
    write_dump(f, 3)
    add_fetch(con, f, "sha-a", 3)
    assert load_pending(con) == [(str(f), "loaded")]
    assert load_pending(con) == []  # nothing pending on re-run

    # Same bytes fetched again in a later run: recorded, not duplicated.
    add_fetch(con, f, "sha-a", 3, run_id="run-2")
    assert load_pending(con) == [(str(f), "already_loaded")]

    assert con.execute("SELECT count(*) FROM raw.property_sales").fetchone() == (3,)
    assert con.execute("SELECT count(*) FROM raw.property_sales_snapshot").fetchone() == (1,)
    assert con.execute(
        "SELECT MUNIDESC, PROPERTYFRACTION, FULL_ADDRESS FROM raw.property_sales LIMIT 1"
    ).fetchone() == ("South Park  ", " ", "0 HILL ST, SOUTH PARK, PA 15129")


def test_new_content_becomes_new_snapshot(con, tmp_path):
    for name, rows in [("a.csv", 3), ("b.csv", 4)]:
        write_dump(tmp_path / name, rows)
        add_fetch(con, tmp_path / name, f"sha-{name}", rows, run_id=name)
    load_pending(con)
    assert con.execute(
        "SELECT snapshot_id, count(*) FROM raw.property_sales GROUP BY 1 ORDER BY 1"
    ).fetchall() == [("sha-a.csv", 3), ("sha-b.csv", 4)]


def test_schema_drift_fails_and_loads_nothing(con, tmp_path):
    f = tmp_path / "drift.csv"
    write_dump(f, 2, header=SOURCE_COLUMNS[:-1] + ["NEWCOL"])
    add_fetch(con, f, "sha-d", 2)
    with pytest.raises(SchemaDriftError, match="NEWCOL"):
        load_pending(con)
    assert con.execute("SELECT count(*) FROM raw.property_sales").fetchone() == (0,)


def test_row_count_mismatch_rolls_back(con, tmp_path):
    f = tmp_path / "short.csv"
    write_dump(f, 2)
    add_fetch(con, f, "sha-s", 5)  # landing check claimed 5 rows
    with pytest.raises(RuntimeError, match="loaded 2 rows"):
        load_pending(con)
    assert con.execute("SELECT count(*) FROM raw.property_sales_snapshot").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM raw.property_sales").fetchone() == (0,)
    assert con.execute("SELECT raw_table FROM ops.fetch_resource").fetchone() == (None,)
