from datetime import date

import pytest

from tests.dq_fixtures import T0, con, make_snapshot, sale  # noqa: F401  (con is a fixture)
from wprdc.transform import TYPED, EvalPoint, TransformError, build_typed


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
