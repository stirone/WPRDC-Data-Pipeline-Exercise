from dataclasses import replace

from tests.dq_fixtures import con, make_snapshot, sale  # noqa: F401  (con is a fixture)
from wprdc.checks import CHECKS
from wprdc.dq import evaluate, mark_notified, notify, pending_alerts
from wprdc.transform import build_typed


def rows(n_missing, n=20):
    return [sale(DEEDPAGE=str(i), RECORDDATE=None if i < n_missing else "2022-07-06") for i in range(n)]


def snap(con, sha, data, minutes, checks=CHECKS):
    make_snapshot(con, sha, data, minutes)
    return evaluate(con, "r", build_typed(con), checks)


def alerts(con):
    return con.execute("SELECT kind, check_id, previous_n, new_n, subject FROM dq.alert ORDER BY alert_id").fetchall()


def test_rate_jump_alerts(con):
    snap(con, "s1", rows(1), 0)
    snap(con, "s2", rows(12), 1)
    assert alerts(con) == [("rate_jump", "record_date_missing", 1, 12,
                            "[WPRDC DQ ALERT] record_date_missing: 5.00% -> 60.00% (snapshot s2, 2026-10-28)")]


def test_small_increase_does_not_alert(con):
    snap(con, "s1", rows(1), 0)
    snap(con, "s2", rows(5), 1)       # more than double, but only 4 more
    assert alerts(con) == []


def test_first_evaluation_no_rate_alert(con):
    snap(con, "s1", rows(12), 0)
    assert alerts(con) == []


def test_key_collision_alerts_on_first_failure(con):
    snap(con, "s1", [sale(), sale(DEEDPAGE="2")], 0)
    snap(con, "s2", [sale(), sale()], 1)
    assert alerts(con) == [("key_collision", "key_not_unique", 0, 2,
                            "[WPRDC DQ ALERT] key_not_unique: 2 records share a key (snapshot s2, 2026-10-28)")]


def test_key_collision_on_first_evaluation(con):
    snap(con, "s1", [sale(), sale()], 0)
    assert [a[0] for a in alerts(con)] == ["key_collision"]


def test_definition_changed_is_notice_not_alert(con):
    snap(con, "s1", rows(1), 0)
    changed = [replace(c, where="record_date_text IS NULL OR false") if c.id == "record_date_missing" else c
               for c in CHECKS]
    snap(con, "s2", rows(12), 1, checks=changed)
    assert alerts(con) == [("definition_changed", "record_date_missing", 1, 12,
                            "[WPRDC DQ NOTICE] record_date_missing definition changed (snapshot s2, 2026-10-28)")]


def test_alerts_not_duplicated_on_rerun(con):
    snap(con, "s1", rows(1), 0)
    snap(con, "s2", rows(12), 1)
    evaluate(con, "r", build_typed(con), CHECKS)
    assert len(alerts(con)) == 1


def test_notify_prints_and_marks(con, capsys):
    snap(con, "s1", rows(1), 0)
    snap(con, "s2", rows(12), 1)
    [alert] = pending_alerts(con)
    notify(alert)
    mark_notified(con, alert["alert_id"])
    out = capsys.readouterr().out
    assert alert["subject"] in out and "dq.check_result" in out
    assert pending_alerts(con) == []


def test_key_collision_body_lists_keys(con):
    snap(con, "s1", [sale(), sale()], 0)
    body = con.execute("SELECT body FROM dq.alert").fetchone()[0]
    assert "1010G00075000000 / 18965 / 534 / 2022-06-21" in body


def test_previous_run_prefers_current_version_on_same_evaluation(con):
    changed = [replace(c, where="record_date_text IS NULL OR false") if c.id == "record_date_missing" else c
               for c in CHECKS]
    snap(con, "s1", rows(1), 0, checks=changed)     # evaluated at the edited version first
    evaluate(con, "r", build_typed(con), CHECKS)    # then at the current version, same snapshot
    snap(con, "s2", rows(12), 1)
    kinds = [a[0] for a in alerts(con) if a[1] == "record_date_missing"]
    assert kinds == ["definition_changed", "rate_jump"]
