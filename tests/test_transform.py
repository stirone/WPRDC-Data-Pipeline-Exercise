from dataclasses import replace
from datetime import date

import duckdb
import pytest

from tests.dq_fixtures import T0, con, make_snapshot, sale  # noqa: F401  (con is a fixture)
from wprdc import config
from wprdc import transform as transform_module
from wprdc.checks import CHECKS
from wprdc.transform import CLEAN, TYPED, EvalPoint, TransformError, build_typed, transform


def test_typed_parses_price_and_dates(con):
    make_snapshot(con, "s1", [sale(PRICE="1658.0"), sale(PRICE=None, DEEDPAGE="535")])
    point = build_typed(con)
    assert isinstance(point, EvalPoint)
    assert point.snapshot_id == "s1"
    assert point.as_of == T0
    assert point.download_date == date(2026, 10, 28)
    assert point.n_rows == 2
    assert con.execute(f"SELECT price FROM {TYPED} ORDER BY source_row_number").fetchall() == [(1658,), (None,)]
    assert con.execute(f"SELECT sale_date, record_date FROM {TYPED} LIMIT 1").fetchone() == (
        date(2022, 6, 21), date(2022, 7, 6))


def test_key_columns_unchanged_and_real_deed(con):
    make_snapshot(con, "s1", [sale(DEEDBOOK="00", DEEDPAGE="000"), sale(DEEDBOOK=" 18965 ")])
    build_typed(con)
    rows = con.execute(f"SELECT DEEDBOOK, has_real_deed FROM {TYPED} ORDER BY source_row_number").fetchall()
    assert rows == [("00", False), (" 18965 ", True)]


def test_text_trimmed_and_blank_is_null(con):
    make_snapshot(con, "s1", [sale(RECORDDATE="  ")])
    build_typed(con)
    assert con.execute(f"SELECT municipality, record_date_text FROM {TYPED}").fetchone() == ("South Park", None)


def test_build_typed_without_snapshot_raises(con):
    with pytest.raises(TransformError):
        build_typed(con)


def clean_rows(con):
    return con.execute(f"SELECT source_row_number, dq_flags FROM {CLEAN} ORDER BY 1").fetchall()


def test_clean_excludes_quarantined_and_flags(con):
    make_snapshot(con, "s1", [sale(), sale(DEEDPAGE="2", RECORDDATE="0212-08-01"),
                              sale(DEEDPAGE="3", RECORDDATE="2022-06-01")])
    result = transform(con, "r1")
    assert clean_rows(con) == [(1, []), (3, ["record_before_sale"])]
    assert (result["n_rows"], result["n_clean"]) == (3, 2)


def test_quarantine_is_by_row_not_key(con):
    make_snapshot(con, "s1", [sale(), sale(RECORDDATE="0212-08-01")])   # same key, only row 2 quarantined
    transform(con, "r1")
    assert clean_rows(con) == [(1, ["key_not_unique"])]


def test_action_change_releases_records(con):
    make_snapshot(con, "s1", [sale(RECORDDATE="0212-08-01")])
    transform(con, "r1")
    assert clean_rows(con) == []
    flagged = [replace(c, action="flag") if c.id == "record_date_invalid" else c for c in CHECKS]
    transform(con, "r2", flagged)
    assert clean_rows(con) == [(1, ["record_date_invalid"])]


def test_transform_rerun_is_noop(con):
    make_snapshot(con, "s1", [sale(), sale()])          # key collision -> one alert
    first = transform(con, "r1")
    tables = ("evaluation", "check_run", "check_result", "issue", "alert")
    counts = [con.execute(f"SELECT count(*) FROM dq.{t}").fetchone() for t in tables]
    second = transform(con, "r2")
    assert [con.execute(f"SELECT count(*) FROM dq.{t}").fetchone() for t in tables] == counts
    assert len(first["alerts"]) == 1 and second["alerts"] == []
    assert clean_rows(con) == [(1, ["key_not_unique"]), (2, ["key_not_unique"])]


def test_structural_failure_keeps_previous_clean(con, monkeypatch):
    make_snapshot(con, "s1", [sale()])
    transform(con, "r1")
    make_snapshot(con, "s2", [sale(), sale(DEEDPAGE="2")], minutes=1)

    def boom(*args, **kwargs):
        raise TransformError("cannot build")
    monkeypatch.setattr(transform_module, "build_clean", boom)
    with pytest.raises(TransformError):
        transform(con, "r2")
    assert con.execute(f"SELECT count(*), any_value(snapshot_id) FROM {CLEAN}").fetchone() == (1, "s1")
    assert con.execute("SELECT count(*) FROM dq.evaluation").fetchone() == (1,)


def test_main_logs_a_transform_run(tmp_path, monkeypatch, capsys):
    db = tmp_path / "main.duckdb"
    c = transform_module.connect(db)
    make_snapshot(c, "s1", [sale()])
    c.close()
    monkeypatch.setattr(config, "DB_PATH", db)
    transform_module.main([])
    assert "record_date_missing" in capsys.readouterr().out
    c = duckdb.connect(str(db))
    assert c.execute("SELECT command, status FROM ops.fetch_run").fetchall() == [("transform", "succeeded")]
    c.close()


def test_pruned_latest_snapshot_fails_instead_of_falling_back(con):
    from tests.dq_fixtures import refetch
    from wprdc.load_raw import prune
    make_snapshot(con, "A", [sale()], 0)
    make_snapshot(con, "B", [sale(DEEDPAGE="2")], 1)
    transform(con, "r1")
    prune(con, keep=1)                 # A's rows leave the DB; its CSV stays
    refetch(con, "A", 2)               # the source goes back to A
    with pytest.raises(TransformError, match="pruned"):
        transform(con, "r2")
    assert con.execute(f"SELECT any_value(snapshot_id) FROM {CLEAN}").fetchone() == ("B",)


def test_unmapped_sale_code_flagged(con):
    make_snapshot(con, "s1", [sale(), sale(DEEDPAGE="2", SALECODE="ZZ")])
    result = transform(con, "r1")
    assert clean_rows(con) == [(1, []), (2, ["sale_code_unmapped"])]
    assert len(result["alerts"]) == 1


def test_parcel_location_varies_flagged(con):
    make_snapshot(con, "s1", [sale(), sale(DEEDPAGE="2", MUNICODE="946"),
                              sale(PARID="0001A00001000000", DEEDPAGE="3")])
    transform(con, "r1")
    assert clean_rows(con) == [(1, ["parcel_location_varies"]), (2, ["parcel_location_varies"]), (3, [])]
