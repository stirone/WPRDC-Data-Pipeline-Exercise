"""DuckDB connection and run bookkeeping."""
from __future__ import annotations

import platform
import socket
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import duckdb
import requests

from wprdc import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def connect(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open the database and apply every sql/*_schema.sql file (all idempotent).

    DuckDB allows a single writer process: close any CLI/notebook session
    holding this file before running the pipeline, or open those read-only.
    """
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute("SET TimeZone = 'UTC'")
    for schema_file in sorted(config.SQL_DIR.glob("*_schema.sql")):
        con.execute(schema_file.read_text(encoding="utf-8"))
    return con


def _git_state() -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=config.PROJECT_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=config.PROJECT_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


@contextmanager
def run_context(
    con: duckdb.DuckDBPyConnection, command: str, notes: str | None = None
) -> Iterator[str]:
    """Record one ops.fetch_run row around a block; yields the run_id.

    The row is written as 'running' up front so a hard crash still leaves
    evidence; it is finalized as succeeded/failed on exit.
    """
    run_id = str(uuid.uuid4())
    commit, dirty = _git_state()
    con.execute(
        """
        INSERT INTO ops.fetch_run (run_id, command, started_at, status, argv,
            git_commit, git_dirty, hostname, python_version, requests_version, notes)
        VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
        """,
        [run_id, command, utcnow(), " ".join(sys.argv), commit, dirty,
         socket.gethostname(), platform.python_version(), requests.__version__, notes],
    )
    try:
        yield run_id
    except BaseException as exc:
        con.execute(
            "UPDATE ops.fetch_run SET finished_at = ?, status = 'failed', error_message = ? "
            "WHERE run_id = ?",
            [utcnow(), f"{type(exc).__name__}: {exc}"[:2000], run_id],
        )
        raise
    con.execute(
        "UPDATE ops.fetch_run SET finished_at = ?, status = 'succeeded' WHERE run_id = ?",
        [utcnow(), run_id],
    )
