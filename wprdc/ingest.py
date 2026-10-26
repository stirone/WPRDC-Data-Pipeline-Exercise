"""Fetch the property sales resource from CKAN and land it, unchanged, under data/raw.

    python -m wprdc.ingest                      # dump strategy, skip if unchanged
    python -m wprdc.ingest --strategy datastore # page through datastore_search
    python -m wprdc.ingest --force              # fetch even if CKAN says unchanged

Re-runnable: each fetch lands in a new run-stamped path and is only made
visible once complete (atomic rename). If CKAN's last_modified for the
resource matches the last successful fetch, nothing is downloaded.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
from datetime import datetime
from pathlib import Path

import duckdb

from wprdc import config
from wprdc.ckan import Ckan
from wprdc.db import connect, run_context, utcnow
from wprdc.http_client import LoggedHttp


def _ckan_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _previous_fetch(con: duckdb.DuckDBPyConnection, resource_id: str, strategy: str):
    return con.execute(
        """
        SELECT ckan_last_modified, raw_sha256, raw_path
        FROM ops.fetch_resource
        WHERE resource_id = ? AND strategy = ? AND status = 'fetched'
        ORDER BY finished_at DESC LIMIT 1
        """,
        [resource_id, strategy],
    ).fetchone()


def _count_csv_rows(path: Path) -> int:
    """Count data rows, failing if any row's width differs from the header's.

    WPRDC's dump ends the header line with CRLF but data rows with LF, which
    defeats DuckDB's strict CSV sniffer. Loading therefore needs
    strict_mode=false, so this independent structural check runs first.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh, strict=True)
        width = len(next(reader))
        n = 0
        for n, row in enumerate(reader, start=1):
            if len(row) != width:
                raise ValueError(f"{path}: row {n} has {len(row)} fields, header has {width}")
    return n


def _recorded_path(path: Path) -> str:
    # Project-relative with POSIX separators so the recorded path works on any OS.
    return (path.relative_to(config.PROJECT_ROOT) if path.is_relative_to(config.PROJECT_ROOT)
            else path).as_posix()


def fetch_datastore(ckan: Ckan, resource_id: str, dest_base: Path, total: int,
                    page_size: int) -> tuple[Path, str, int, int]:
    """Land every datastore_search page body byte-for-byte, then publish the directory."""
    final_dir = dest_base
    part_dir = dest_base.with_name(dest_base.name + ".part")
    part_dir.mkdir(parents=True, exist_ok=False)
    combined = hashlib.sha256()
    n_bytes = n_rows = 0
    offset = 0
    while offset < total:
        page = ckan.datastore_search(resource_id, limit=page_size, offset=offset)
        (part_dir / f"page_{offset:09d}.json").write_bytes(page.body)
        combined.update(page.sha256.encode())
        n_bytes += len(page.body)
        records = page.payload["result"]["records"]
        n_rows += len(records)
        if not records:
            break
        offset += page_size
    os.replace(part_dir, final_dir)
    return final_dir, combined.hexdigest(), n_bytes, n_rows


def ingest(con, ckan: Ckan, run_id: str, *, resource_id: str, strategy: str,
           force: bool, page_size: int) -> str:
    started = utcnow()
    pkg = ckan.package_show(config.PACKAGE_ID)
    res = next((r for r in pkg["resources"] if r["id"] == resource_id), None)
    if res is None:
        raise RuntimeError(f"resource {resource_id} no longer in package {config.PACKAGE_ID}")
    last_modified = _ckan_ts(res.get("last_modified") or res.get("metadata_modified"))
    pkg_modified = _ckan_ts(pkg.get("metadata_modified"))
    prev = _previous_fetch(con, resource_id, strategy)

    def record(status: str, **cols) -> None:
        row = dict(
            run_id=run_id, resource_id=resource_id, strategy=strategy,
            package_id=config.PACKAGE_ID, resource_name=res.get("name"),
            ckan_last_modified=last_modified, package_metadata_modified=pkg_modified,
            status=status, started_at=started, finished_at=utcnow(), **cols,
        )
        con.execute(
            f"INSERT INTO ops.fetch_resource ({', '.join(row)}) "
            f"VALUES ({', '.join('?' * len(row))})",
            list(row.values()),
        )

    if prev and prev[0] == last_modified and not force:
        record("skipped_unchanged", raw_path=prev[2], raw_sha256=prev[1], content_changed=False)
        print(f"unchanged since last fetch (last_modified {last_modified}); skipping")
        return "skipped_unchanged"

    total = ckan.datastore_total(resource_id)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    dest_base = config.RAW_DIR / config.PACKAGE_ID / resource_id / strategy / f"{stamp}_{run_id[:8]}"
    # Filled in as soon as bytes are on disk, so a failure in a later check
    # still records which archived file it was about.
    landed: dict = {}
    try:
        if strategy == "dump":
            result = ckan.dump(resource_id, dest_base.with_suffix(".csv"))
            path, sha, n_bytes = result.path, result.sha256, result.bytes
            landed = dict(raw_path=_recorded_path(path), raw_sha256=sha, raw_bytes=n_bytes)
            n_rows = _count_csv_rows(path)
        else:
            path, sha, n_bytes, n_rows = fetch_datastore(ckan, resource_id, dest_base, total, page_size)
    except Exception as exc:
        record("failed", ckan_reported_total=total,
               error_message=f"{type(exc).__name__}: {exc}"[:2000], **landed)
        raise

    record(
        "fetched", ckan_reported_total=total,
        raw_path=_recorded_path(path), raw_sha256=sha, raw_bytes=n_bytes, raw_rows=n_rows,
        content_changed=(prev is None or prev[1] != sha),
    )
    print(f"landed {n_rows:,} rows / {n_bytes:,} bytes -> {path}")
    if n_rows != total:
        print(f"WARNING: landed {n_rows:,} rows but CKAN reported {total:,}")
    return "fetched"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strategy", choices=["dump", "datastore"], default="dump")
    ap.add_argument("--resource-id", default=config.MAIN_RESOURCE_ID)
    ap.add_argument("--page-size", type=int, default=32000, help="datastore strategy only")
    ap.add_argument("--force", action="store_true", help="fetch even if unchanged")
    ap.add_argument("--notes")
    args = ap.parse_args(argv)

    con = connect()
    try:
        with run_context(con, "ingest", args.notes) as run_id:
            ckan = Ckan(LoggedHttp(con, run_id))
            ingest(con, ckan, run_id, resource_id=args.resource_id,
                   strategy=args.strategy, force=args.force, page_size=args.page_size)
    finally:
        con.close()


if __name__ == "__main__":
    main()
