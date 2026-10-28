# AI Usage

AI tools were used extensively on this exercise. This document explains which tools,
how the work was divided, and where the important decisions came from, so a reviewer
can judge both the code and how I worked with the AI.

The full session transcripts are linked below. This page is a guide to them.

## Tools

| Tool | Used for |
|---|---|
| **Claude.ai** | Early exploration: learning about the four candidate datasets and the WPRDC CKAN API, and a first draft of the fetch-logging schema (`fetch_run` / `fetch_attempt` / `fetch_resource`). |
| **Claude Code** (CLI, Claude Opus 5.5) | Building the pipeline: writing the Python and SQL, writing and running tests, calling the live API, querying the data, and making git commits. |

## Division of labor

**Claude Code wrote nearly all of the code, SQL and tests**, and ran the commands.
Every commit carries a `Co-Authored-By: Claude` trailer.

**I set the direction and made the design decisions.** I reviewed the proposals,
asked for explanations where I didn't follow the architecture, and pushed back when
something seemed overstated or more complex than needed. The sections below list the
specific cases. I am prepared to walk through and defend any part of the code live.

## Session log

| Date | Session | Topics | Transcript |
|---|---|---|---|
| 2026-10-?? | Claude.ai | Dataset exploration; first logging-schema idea | _link to shared conversation_ |
| 2026-10-26 | Claude Code #1 | Fetch logging, raw landing, raw snapshots, DuckDB version pin, row hashes | [`ai-sessions/2026-10-26-ingest-and-raw-layer.txt`](ai-sessions/2026-10-26-ingest-and-raw-layer.txt) |
| 2026-10-28 | Claude Code #2 | Status review; second snapshot (+507 / −206 row versions, unchanged rows not rewritten); why a run makes 3 requests; re-run check; DuckDB UI for browsing | [`ai-sessions/2026-10-28-182057-where-are-we-on-this-project.txt`](ai-sessions/2026-10-28-182057-where-are-we-on-this-project.txt) |

## Decisions I made

- **Dataset:** Allegheny County Property Sale Transactions.
- **Fetch logging is part of the production pipeline**, not only a development aid.
  Before starting, I had seen intermittent failures from WPRDC with `ckanapi` (blank
  responses, then HTML where JSON was expected), and I wanted those measured, not
  hidden by retries.
- **Accepted the revisions to the Claude.ai schema:**
  - drop `ckanapi` in favor of a logged `requests` client;
  - classify each response by its content, not its HTTP status;
  - record the request parameters and the CKAN metadata that update detection needs;
  - treat downloaded files as the true "raw".
- **Keep every successfully downloaded CSV**, whether the fetch was forced or not.
  The files are the permanent archive; the DuckDB tables are working storage that can
  be pruned and rebuilt from them.
- **Raw layer = complete snapshots, not a history table.** Claude proposed a
  content-hash history table (one row per row version, open/closed by snapshot). I
  rejected it because rebuilding the data as of a particular day becomes more complex
  and harder to trust. Full snapshots make any day a single `WHERE snapshot_id = ...`.
  I kept the useful part of the idea, row hashes, for comparing snapshots.
- **Pinned DuckDB to 1.5.6.** DuckDB 2.0 wrote a storage format the newest available
  CLI couldn't open. I chose matching versions (with the existing data migrated)
  over a compatibility setting I would have to explain.
- **Fixed failed fetches leaving files with no record,** and backfilled the one
  orphaned CSV from the logs, so every file in the archive traces back to a run.

## Where I questioned or corrected the AI

- **"Did you re-download the data under 1.5.6?"** After the DuckDB migration, Claude
  reported things as "verified on 1.5.6", but the ingest had skipped the download
  because the source hadn't changed. Claude confirmed nothing had been re-downloaded
  and that it should have said so explicitly.
- **"Adds no rows to what?"** I asked for the architecture to be explained before
  going further. That produced the files → `ops.*` → `raw.*` description that now
  informs the README.
- **"Why is #1 a problem?"** Claude had listed duplicate CSV files from identical
  downloads as a gap. Asked to justify it, Claude agreed it was a minor storage
  issue, not a correctness problem, and dropped the planned code change.
- **Incremental loading.** My idea of hashing every column except `_id` led to the
  key clarification: hashing makes the *load* incremental, not the *download*. A
  full download is still needed to detect deletions.

## AI mistakes caught during the work

- **A wrong duplicate count.** A first hashing query reported only 313,721 distinct
  rows out of 506,425. Claude noticed this contradicted another query, traced it to
  its own SQL bug (DuckDB `COLUMNS()` expanding into 25 hash columns), and corrected
  it before anything was built on it. The data has no duplicates.
- **A path fix that silently did nothing.** A shell-escaping problem meant the first
  attempt to normalize stored file paths changed nothing. It was caught by checking
  the output and redone.
- **A missing dependency.** DuckDB's Python client needs `pytz` to return timestamps
  with time zones. It surfaced during an ad-hoc query and is now pinned.

## Findings from the investigation

These came out of running the pipeline and querying the results. They will feed the
README's "what surprised me" section and the data quality checks.

- **WPRDC's 502 errors come from its own nginx server, not the CDN.** They return in
  about 80 ms with nginx's error page, which means the CKAN application behind it
  refused the connection. Captured in `ops.fetch_attempt`.
- **The CSV dump mixes line endings:** a CRLF header with LF data rows. DuckDB's
  strict CSV parser rejects the file, so a separate structural check was added.
- **`_id` is regenerated on each refresh** (values start near 17 million), so it
  can't be used as a key.
- **`RECORDDATE` contains `0212-08-01`** (presumably a typo for 2012) **and dates in
  the future.**

## How the AI's work was verified

- Automated tests (`python -m pytest`) cover response classification, retries, raw
  loading, re-runs, schema drift, rollback, pruning and rebuilding. The tests include
  a replay of the real blank → HTML → JSON failure sequence.
- Each stage was run against the live API and real data, with the results checked by
  queries. For example: the row count matched CKAN's total of 506,425, re-runs skipped
  as expected, and the raw layer was rebuilt from the CSV archive.
