"""Evaluate data quality checks, maintain the steward's issue list, raise alerts.

Called by wprdc.transform inside its transaction; see sql/dq_schema.sql for the
tables and wprdc/checks.py for the checks.
"""
from __future__ import annotations

import duckdb

from wprdc.checks import ALERT_MIN_INCREASE, ALERT_RATE_FACTOR, Check
from wprdc.db import utcnow
from wprdc.transform import TYPED, EvalPoint


def evaluate(con: duckdb.DuckDBPyConnection, run_id: str, point: EvalPoint,
             checks: list[Check]) -> int:
    """Evaluate every check not yet evaluated at this point and version; returns eval_id.

    Re-running on the same point writes nothing. The caller owns the transaction.
    """
    row = con.execute("SELECT eval_id FROM dq.evaluation WHERE snapshot_id = ? AND as_of = ?",
                      [point.snapshot_id, point.as_of]).fetchone()
    if row:
        eval_id = row[0]
    else:
        eval_id = con.execute(
            """INSERT INTO dq.evaluation (snapshot_id, as_of, download_date, n_rows, run_id, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?) RETURNING eval_id""",
            [point.snapshot_id, point.as_of, point.download_date, point.n_rows, run_id, utcnow()],
        ).fetchone()[0]

    for check in checks:
        if check.retired:
            retire_issues(con, eval_id, check)
            continue
        done = con.execute(
            "SELECT 1 FROM dq.check_run WHERE eval_id = ? AND check_id = ? AND check_version = ?",
            [eval_id, check.id, check.version]).fetchone()
        if done:
            # Results exist, but the issue list may have moved on (the rule was
            # edited and reverted, the check un-retired, or its action changed);
            # update_issues is idempotent, so bring the list back in line.
            update_issues(con, eval_id, check)
            continue
        con.execute(
            f"""INSERT INTO dq.check_result
                SELECT ?, ?, ?, ?, source_row_number, sale_key, PARID, DEEDBOOK, DEEDPAGE, SALEDATE
                FROM {TYPED} WHERE {check.where}""",
            [eval_id, check.id, check.version, check.action])
        n_failing = con.execute(
            "SELECT count(*) FROM dq.check_result WHERE eval_id = ? AND check_id = ? AND check_version = ?",
            [eval_id, check.id, check.version]).fetchone()[0]
        con.execute(
            "INSERT INTO dq.check_run VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [eval_id, check.id, check.version, check.action, check.description,
             n_failing, point.n_rows, n_failing / point.n_rows if point.n_rows else 0.0])
        update_issues(con, eval_id, check)
        detect_alerts(con, run_id, eval_id, check)
    return eval_id


def current_results(eval_id: int, checks: list[Check]) -> str:
    """SQL selecting (check_id, action, source_row_number) failing at eval_id, at each
    non-retired check's current version, with the action as currently defined."""
    active = [c for c in checks if not c.retired]
    if not active:
        return ("SELECT NULL::VARCHAR AS check_id, NULL::VARCHAR AS action, "
                "NULL::BIGINT AS source_row_number WHERE false")
    defs = ", ".join(f"('{c.id}', '{c.version}', '{c.action}')" for c in active)
    return f"""
        SELECT r.check_id, d.action, r.source_row_number
        FROM dq.check_result r
        JOIN (VALUES {defs}) d(check_id, check_version, action)
          ON d.check_id = r.check_id AND d.check_version = r.check_version
        WHERE r.eval_id = {int(eval_id)}"""


def update_issues(con: duckdb.DuckDBPyConnection, eval_id: int, check: Check) -> None:
    """Open, carry forward and resolve this check's issues for one evaluation.

    An issue that stops failing is resolved with the outcome decided now, while
    this snapshot is in clean.property_sales_typed:
      rule_changed  record still present, but the check's version changed
      fixed         record still present and passes
      replaced      record gone, a record with the same real deed is present
                    (replaced_by_sale_key named only if exactly one)
      removed       record gone, no successor
    """
    p = {"eval_id": eval_id, "check_id": check.id, "version": check.version, "action": check.action}
    failing = """(SELECT DISTINCT sale_key FROM dq.check_result
                  WHERE eval_id = $eval_id AND check_id = $check_id AND check_version = $version)"""
    con.execute(
        f"""
        UPDATE dq.issue SET resolved_eval_id = $eval_id, outcome = s.outcome,
                            replaced_by_sale_key = s.replaced_by
        FROM (
            WITH gone AS (
                SELECT * FROM dq.issue
                WHERE check_id = $check_id AND resolved_eval_id IS NULL
                  AND sale_key NOT IN {failing}
            ), successors AS (
                SELECT g.issue_id, count(DISTINCT t.sale_key) AS n, min(t.sale_key) AS sale_key
                FROM gone g
                JOIN {TYPED} t ON t.PARID = g.PARID AND t.DEEDBOOK = g.DEEDBOOK AND t.DEEDPAGE = g.DEEDPAGE
                WHERE g.has_real_deed
                GROUP BY 1
            )
            SELECT g.issue_id,
                   CASE WHEN g.sale_key IN (SELECT sale_key FROM {TYPED}) AND g.check_version <> $version
                             THEN 'rule_changed'
                        WHEN g.sale_key IN (SELECT sale_key FROM {TYPED}) THEN 'fixed'
                        WHEN s.n > 0 THEN 'replaced'
                        ELSE 'removed' END AS outcome,
                   CASE WHEN g.sale_key NOT IN (SELECT sale_key FROM {TYPED}) AND s.n = 1
                        THEN s.sale_key END AS replaced_by
            FROM gone g LEFT JOIN successors s USING (issue_id)
        ) s
        WHERE dq.issue.issue_id = s.issue_id
        """, {k: v for k, v in p.items() if k != "action"})
    con.execute(
        f"""UPDATE dq.issue SET last_seen_eval_id = $eval_id, check_version = $version, action = $action
            WHERE check_id = $check_id AND resolved_eval_id IS NULL AND sale_key IN {failing}""", p)
    con.execute(
        f"""
        INSERT INTO dq.issue (check_id, check_version, action, sale_key, PARID, DEEDBOOK, DEEDPAGE,
                              SALEDATE, has_real_deed, first_seen_eval_id, last_seen_eval_id)
        SELECT $check_id, $version, $action, t.sale_key, any_value(t.PARID), any_value(t.DEEDBOOK),
               any_value(t.DEEDPAGE), any_value(t.SALEDATE), bool_or(t.has_real_deed), $eval_id, $eval_id
        FROM {TYPED} t
        WHERE t.sale_key IN {failing}
          AND t.sale_key NOT IN (SELECT sale_key FROM dq.issue
                                 WHERE check_id = $check_id AND resolved_eval_id IS NULL)
        GROUP BY t.sale_key
        """, p)


def retire_issues(con: duckdb.DuckDBPyConnection, eval_id: int, check: Check) -> None:
    """A retired check's open issues close as rule_changed: nobody fixed them."""
    con.execute(
        """UPDATE dq.issue SET resolved_eval_id = ?, outcome = 'rule_changed'
           WHERE check_id = ? AND resolved_eval_id IS NULL""", [eval_id, check.id])


def detect_alerts(con: duckdb.DuckDBPyConnection, run_id: str, eval_id: int,
                  check: Check) -> list[dict]:
    """Compare this evaluation with the check's previous one; record any alert.

    rate_jump           same version, rate > ALERT_RATE_FACTOR x previous and
                        at least ALERT_MIN_INCREASE more failures
    key_collision       alert="any" checks: first failure after zero (or ever)
    definition_changed  the version differs from the previous evaluation's;
                        a notice, and the rate baseline restarts
    """
    cur = con.execute(
        """SELECT c.n_failing, c.rate, e.snapshot_id, e.as_of
           FROM dq.check_run c JOIN dq.evaluation e USING (eval_id)
           WHERE c.eval_id = ? AND c.check_id = ? AND c.check_version = ?""",
        [eval_id, check.id, check.version]).fetchone()
    n, rate, snapshot_id, as_of = cur
    # Earlier evaluations, or this one at another version (evaluated before now).
    prev = con.execute(
        """SELECT c.check_version, c.n_failing, c.rate, e.snapshot_id
           FROM dq.check_run c JOIN dq.evaluation e USING (eval_id)
           WHERE c.check_id = ? AND NOT (c.eval_id = ? AND c.check_version = ?)
             AND (e.as_of < ? OR c.eval_id = ?)
           ORDER BY e.as_of DESC, c.eval_id DESC, (c.check_version = ?) DESC LIMIT 1""",
        [check.id, eval_id, check.version, as_of, eval_id, check.version]).fetchone()
    prev_version, prev_n, prev_rate, prev_snapshot = prev or (None, None, None, None)

    where = f"(snapshot {snapshot_id[:8]}, {as_of:%Y-%m-%d})"
    kind = None
    if prev is not None and prev_version != check.version:
        kind = "definition_changed"
        subject = f"[WPRDC DQ NOTICE] {check.id} definition changed {where}"
    elif check.alert == "any":
        if n > 0 and (prev is None or prev_n == 0):
            kind = "key_collision"
            subject = f"[WPRDC DQ ALERT] {check.id}: {n} records share a key {where}"
    elif (prev is not None and rate > ALERT_RATE_FACTOR * prev_rate
          and n - prev_n >= ALERT_MIN_INCREASE):
        kind = "rate_jump"
        subject = f"[WPRDC DQ ALERT] {check.id}: {prev_rate:.2%} -> {rate:.2%} {where}"
    if kind is None:
        return []

    def fmt(count, r):
        return "none" if count is None else f"{count:,} ({r:.2%})"
    lines = [
        f"Check:     {check.id} ({check.description}), action {check.action}, version {check.version}",
        f"Previous:  {fmt(prev_n, prev_rate)} in snapshot {prev_snapshot or '-'}",
        f"Now:       {fmt(n, rate)} in snapshot {snapshot_id}, fetched {as_of:%Y-%m-%d %H:%M} UTC",
        "Failures:",
        f"  SELECT * FROM dq.check_result WHERE eval_id = {eval_id} "
        f"AND check_id = '{check.id}' AND check_version = '{check.version}';",
    ]
    if kind == "key_collision":
        keys = con.execute(
            """SELECT DISTINCT PARID, DEEDBOOK, DEEDPAGE, SALEDATE FROM dq.check_result
               WHERE eval_id = ? AND check_id = ? AND check_version = ? ORDER BY ALL LIMIT 20""",
            [eval_id, check.id, check.version]).fetchall()
        lines += ["Colliding keys (PARID / DEEDBOOK / DEEDPAGE / SALEDATE, first 20):"]
        lines += ["  " + " / ".join(str(v) for v in key) for key in keys]
    body = "\n".join(lines)
    inserted = con.execute(
        """INSERT INTO dq.alert (created_at, run_id, eval_id, snapshot_id, check_id, check_version,
                                 kind, previous_rate, new_rate, previous_n, new_n, subject, body)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT DO NOTHING
           RETURNING *""",
        [utcnow(), run_id, eval_id, snapshot_id, check.id, check.version, kind,
         prev_rate, rate, prev_n if prev is not None else (0 if kind == "key_collision" else None),
         n, subject, body])
    return _dicts(inserted)


def _dicts(cursor) -> list[dict]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def pending_alerts(con: duckdb.DuckDBPyConnection) -> list[dict]:
    return _dicts(con.execute("SELECT * FROM dq.alert WHERE notified_at IS NULL ORDER BY alert_id"))


def notify(alert: dict) -> None:
    """Tell the engineer. Prints for now; an email version (SMTP settings from
    environment variables, subject as given) replaces only this function."""
    print(f"\n{alert['subject']}\n{alert['body']}\n")


def mark_notified(con: duckdb.DuckDBPyConnection, alert_id: int) -> None:
    con.execute("UPDATE dq.alert SET notified_at = ? WHERE alert_id = ?", [utcnow(), alert_id])
