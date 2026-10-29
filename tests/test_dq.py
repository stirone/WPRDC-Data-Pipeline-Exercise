from tests.dq_fixtures import con, make_snapshot, sale  # noqa: F401  (con is a fixture)
from wprdc.checks import CHECKS
from wprdc.dq import evaluate
from wprdc.transform import build_typed


def run(con, rows, sha="s1", minutes=0, checks=CHECKS):
    make_snapshot(con, sha, rows, minutes)
    return evaluate(con, "r1", build_typed(con), checks)


def failing(con, check_id):
    return [r[0] for r in con.execute(
        "SELECT source_row_number FROM dq.check_result WHERE check_id = ? ORDER BY 1",
        [check_id]).fetchall()]


def test_record_date_rules(con):
    run(con, [sale(),
              sale(DEEDPAGE="2", RECORDDATE="0212-08-01", SALEDATE="2012-08-01"),
              sale(DEEDPAGE="3", RECORDDATE="2026-10-29"),
              sale(DEEDPAGE="4", RECORDDATE=None),
              sale(DEEDPAGE="5", RECORDDATE="2022-06-01")])
    assert failing(con, "record_date_invalid") == [2]
    assert failing(con, "record_date_future") == [3]
    assert failing(con, "record_date_missing") == [4]
    assert failing(con, "record_before_sale") == [5]
    assert failing(con, "sale_date_invalid") == []


def test_blank_record_date_is_missing_not_invalid(con):
    run(con, [sale(RECORDDATE="  ")])
    assert failing(con, "record_date_missing") == [1]
    assert failing(con, "record_date_invalid") == []


def test_unparseable_record_date_is_invalid(con):
    run(con, [sale(RECORDDATE="2022-13-45")])
    assert failing(con, "record_date_invalid") == [1]
    assert failing(con, "record_date_missing") == []


def test_sale_date_before_2012_and_unparseable(con):
    run(con, [sale(SALEDATE="2011-12-31"), sale(DEEDPAGE="2", SALEDATE="not a date"), sale(DEEDPAGE="3")])
    assert failing(con, "sale_date_invalid") == [1, 2]


def test_possible_duplicate_and_key_not_unique(con):
    run(con, [sale(PARID="8000T00220000000", DEEDBOOK="00", DEEDPAGE="00"),
              sale(PARID="8000T00220000000", DEEDBOOK="000", DEEDPAGE="000"),
              sale(DEEDPAGE="9"), sale(DEEDPAGE="9")])
    assert failing(con, "possible_duplicate") == [1, 2]
    assert failing(con, "key_not_unique") == [3, 4]


def test_check_run_rates(con):
    run(con, [sale(), sale(DEEDPAGE="2"), sale(DEEDPAGE="3"), sale(DEEDPAGE="4", RECORDDATE=None)])
    assert con.execute(
        "SELECT n_failing, n_rows, rate, action FROM dq.check_run WHERE check_id = 'record_date_missing'"
    ).fetchone() == (1, 4, 0.25, "quarantine")
    assert con.execute("SELECT count(*) FROM dq.check_run").fetchone() == (len(CHECKS),)


def test_evaluate_twice_writes_nothing_new(con):
    eval_id = run(con, [sale(RECORDDATE=None)])
    counts = [con.execute(f"SELECT count(*) FROM dq.{t}").fetchone() for t in ("evaluation", "check_run", "check_result")]
    assert evaluate(con, "r2", build_typed(con), CHECKS) == eval_id
    assert [con.execute(f"SELECT count(*) FROM dq.{t}").fetchone()
            for t in ("evaluation", "check_run", "check_result")] == counts
