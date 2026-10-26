"""Logged CKAN access for interactive exploration, so dev hits land in ops.* too.

    from wprdc.dev import dev_session
    with dev_session("checking deep offsets") as (ckan, con):
        ckan.datastore_search(MAIN_RESOURCE_ID, limit=5, offset=400_000)
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import duckdb

from wprdc.ckan import Ckan
from wprdc.db import connect, run_context
from wprdc.http_client import LoggedHttp


@contextmanager
def dev_session(notes: str | None = None, **http_kwargs) -> Iterator[tuple[Ckan, duckdb.DuckDBPyConnection]]:
    con = connect()
    try:
        with run_context(con, "adhoc", notes) as run_id:
            yield Ckan(LoggedHttp(con, run_id, **http_kwargs)), con
    finally:
        con.close()
