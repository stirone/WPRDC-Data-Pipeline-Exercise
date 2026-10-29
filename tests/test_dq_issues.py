from dataclasses import replace
from datetime import timedelta

from tests.dq_fixtures import T0, con, make_snapshot, refetch, sale  # noqa: F401  (con is a fixture)
from wprdc.checks import CHECKS
from wprdc.dq import evaluate
from wprdc.transform import TYPED, build_typed

MISSING = "record_date_missing"


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def snap(con, sha, rows, minutes, checks=CHECKS):
    make_snapshot(con, sha, rows, minutes)
    return evaluate(con, "r", build_typed(con), checks)


def with_check(check_id, **changes):
    return [replace(c, **changes) if c.id == check_id else c for c in CHECKS]


def issues(con, check_id):
    return con.execute(
        """SELECT status, outcome, first_seen_at, last_seen_at, resolved_at, action, replaced_by_sale_key
           FROM dq.v_issues WHERE check_id = ? ORDER BY issue_id""", [check_id]).fetchall()


def test_issue_persists_across_snapshots(con):
    snap(con, "s1", [sale(RECORDDATE=None)], 0)
    snap(con, "s2", [sale(RECORDDATE=None), sale(DEEDPAGE="2")], 1)
    assert issues(con, MISSING) == [("open", None, at(0), at(1), None, "quarantine", None)]


def test_fixed(con):
    snap(con, "s1", [sale(RECORDDATE=None)], 0)
    snap(con, "s2", [sale()], 1)
    assert issues(con, MISSING) == [("resolved", "fixed", at(0), at(0), at(1), "quarantine", None)]


def test_replaced_links_successor(con):
    snap(con, "s1", [sale(RECORDDATE="2022-06-01")], 0)                       # recorded before the sale
    snap(con, "s2", [sale(RECORDDATE="2022-06-01", SALEDATE="2022-05-30")], 1)  # sale date corrected
    new_key = con.execute(f"SELECT sale_key FROM {TYPED}").fetchone()[0]
    assert issues(con, "record_before_sale") == [
        ("resolved", "replaced", at(0), at(0), at(1), "flag", new_key)]


def test_replaced_with_two_successors_names_none(con):
    snap(con, "s1", [sale(RECORDDATE="2022-06-01")], 0)
    snap(con, "s2", [sale(SALEDATE="2022-05-30"), sale(SALEDATE="2022-05-31")], 1)
    assert issues(con, "record_before_sale")[0][1:2] == ("replaced",)
    assert issues(con, "record_before_sale")[0][6] is None


def test_removed_and_no_deed_never_replaced(con):
    snap(con, "s1", [sale(DEEDBOOK="0", DEEDPAGE="0", RECORDDATE=None)], 0)
    snap(con, "s2", [sale(DEEDBOOK="0", DEEDPAGE="0", RECORDDATE=None, SALEDATE="2022-06-20")], 1)
    assert [r[:2] for r in issues(con, MISSING)] == [("resolved", "removed"), ("open", None)]


def test_rule_changed_on_version_change(con):
    snap(con, "s1", [sale(RECORDDATE=None)], 0)
    evaluate(con, "r", build_typed(con), with_check(MISSING, where="record_date_text IS NULL AND false"))
    assert issues(con, MISSING) == [("resolved", "rule_changed", at(0), at(0), at(0), "quarantine", None)]


def test_action_change_keeps_issue_open(con):
    snap(con, "s1", [sale(RECORDDATE=None)], 0)
    snap(con, "s2", [sale(RECORDDATE=None)], 1, checks=with_check(MISSING, action="flag"))
    assert issues(con, MISSING) == [("open", None, at(0), at(1), None, "flag", None)]


def test_retired_check_closes_as_rule_changed(con):
    snap(con, "s1", [sale(RECORDDATE=None)], 0)
    snap(con, "s2", [sale(RECORDDATE=None)], 1, checks=with_check(MISSING, retired=True))
    assert issues(con, MISSING) == [("resolved", "rule_changed", at(0), at(0), at(1), "quarantine", None)]


def test_reopen_creates_new_issue(con):
    snap(con, "s1", [sale(RECORDDATE=None)], 0)
    snap(con, "s2", [sale()], 1)
    snap(con, "s3", [sale(RECORDDATE=None)], 2)
    assert [r[:3] for r in issues(con, MISSING)] == [("resolved", "fixed", at(0)), ("open", None, at(2))]


def test_return_to_earlier_snapshot_reevaluates(con):
    snap(con, "A", [sale(RECORDDATE=None)], 0)
    snap(con, "B", [sale()], 1)
    refetch(con, "A", 2)
    evaluate(con, "r", build_typed(con), CHECKS)
    assert [r[:3] for r in issues(con, MISSING)] == [("resolved", "fixed", at(0)), ("open", None, at(2))]
