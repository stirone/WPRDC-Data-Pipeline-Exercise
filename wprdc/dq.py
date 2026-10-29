"""Evaluate data quality checks, maintain the steward's issue list, raise alerts.

Called by wprdc.transform inside its transaction; see sql/dq_schema.sql for the
tables and wprdc/checks.py for the checks.
"""
from __future__ import annotations

import duckdb

from wprdc.checks import Check
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
    pass


def retire_issues(con: duckdb.DuckDBPyConnection, eval_id: int, check: Check) -> None:
    pass


def detect_alerts(con: duckdb.DuckDBPyConnection, run_id: str, eval_id: int,
                  check: Check) -> list[dict]:
    return []
