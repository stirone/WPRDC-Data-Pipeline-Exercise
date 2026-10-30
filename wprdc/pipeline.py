"""Run the whole pipeline: ingest, load_raw, transform, analytics.

    python -m wprdc.pipeline            # normal run; skips the download if CKAN says unchanged
    python -m wprdc.pipeline --force    # download even if unchanged

Each step is the same as running its own command, and logs its own run in
ops.fetch_run. The first step that fails stops the pipeline (its error is
raised, so the exit code is non-zero); later steps do not run, so the
previous clean and analytics tables stay. Every step is a no-op when there
is nothing new, so running the pipeline twice changes nothing.

Special modes stay on the step commands: python -m wprdc.ingest --strategy
datastore, python -m wprdc.load_raw --prune-keep N / --rebuild.
"""
from __future__ import annotations

import argparse

from wprdc import analytics, ingest, load_raw, transform

STEPS = (("ingest", ingest), ("load_raw", load_raw), ("transform", transform), ("analytics", analytics))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="ingest: download even if unchanged")
    args = ap.parse_args(argv)
    for name, step in STEPS:
        print(f"== {name}")
        step.main(["--force"] if name == "ingest" and args.force else [])


if __name__ == "__main__":
    main()
