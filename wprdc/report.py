"""Print the ops reliability views.  python -m wprdc.report"""
import sys

from wprdc.db import connect

QUERIES = {
    "Recent runs": """
        SELECT command, status, started_at, round(epoch(finished_at - started_at)) AS secs,
               git_commit[:8] AS commit, notes, error_message
        FROM ops.fetch_run ORDER BY started_at DESC LIMIT 15""",
    "Requests needing retries or failing": """
        SELECT endpoint, attempts, final_outcome, first_attempt_at, total_elapsed_ms, request_key
        FROM ops.v_request_outcomes WHERE attempts > 1 OR final_outcome <> 'ok'
        ORDER BY first_attempt_at DESC LIMIT 20""",
    "Failure breakdown": "SELECT * EXCLUDE (sample_snippet) FROM ops.v_failure_breakdown",
    "Reliability by hour (ET)": "SELECT * FROM ops.v_reliability_by_hour",
    "Resource fetches": """
        SELECT strategy, status, ckan_last_modified, ckan_reported_total, raw_rows,
               raw_bytes, content_changed, raw_path
        FROM ops.fetch_resource ORDER BY started_at DESC LIMIT 15""",
}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    con = connect()
    try:
        for title, sql in QUERIES.items():
            print(f"\n== {title}")
            con.sql(sql).show(max_width=200)
    finally:
        con.close()


if __name__ == "__main__":
    main()
