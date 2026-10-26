"""Lightweight reliability probe: a few cheap CKAN calls, repeated, all logged.

    python -m wprdc.probe                       # one round
    python -m wprdc.probe --rounds 12 --interval 300

Schedule it (Task Scheduler / cron) to populate ops.v_reliability_by_hour
without downloading the full dataset each time.
"""
from __future__ import annotations

import argparse
import time

from wprdc import config
from wprdc.ckan import Ckan
from wprdc.db import connect, run_context
from wprdc.http_client import FetchError, LoggedHttp


def probe_round(ckan: Ckan, resource_id: str) -> list[str]:
    failures = []
    calls = [
        ("package_show", lambda: ckan.package_show(config.PACKAGE_ID)),
        ("datastore_search head", lambda: ckan.datastore_search(resource_id, limit=1)),
        # Deep offsets make the database scan; a common place for CKAN to time out.
        ("datastore_search deep", lambda: ckan.datastore_search(resource_id, limit=1, offset=500_000)),
    ]
    for name, call in calls:
        try:
            call()
        except FetchError as exc:
            failures.append(f"{name}: {exc}")
    return failures


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between rounds")
    ap.add_argument("--resource-id", default=config.MAIN_RESOURCE_ID)
    ap.add_argument("--notes")
    args = ap.parse_args(argv)

    con = connect()
    try:
        with run_context(con, "probe", args.notes) as run_id:
            # Fewer retries than ingest: the point is to observe failures, not paper over them.
            ckan = Ckan(LoggedHttp(con, run_id, max_attempts=3))
            for i in range(args.rounds):
                if i:
                    time.sleep(args.interval)
                failures = probe_round(ckan, args.resource_id)
                print(f"round {i + 1}/{args.rounds}: {len(failures)} failed request(s)")
                for f in failures:
                    print("   ", f)
    finally:
        con.close()


if __name__ == "__main__":
    main()
