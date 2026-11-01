# Introduction 
Thank you so much for this very interesting exercise!  This is an entirely AI-assisted result using Claude Code in Visual Studio Code.  One of my main reasons for completing this assignment and submitting it (besides to remain eligible for consideration for the job) was to find out if you think this is acceptable and appropriate.  I have evolved to using the Genie AI assistant in Databricks in my current job on a daily basis to write Python and SQL, and it is so much more efficient and clear than me writing code directly -- I cannot imagine going back.  However, I am concerned about maintaining AI-assisted code, and I am wondering how that will shake out in real life.  That is why I left the implementation gap (row count failure only prints a warning, doesn't fail the pipeline, as designed but not implemented by Claude) in this code.  You may want to have someone else try to make that fix, using AI or otherwise.  

In addition to the AI_USAGE document here for my AI disclosure statement, I've also included a transcript of every session I had with Claude Code in the folder docs\ai-sessions (linked clearly in [`docs/AI_USAGE.md`](docs/AI_USAGE.md)), in case you want to see exactly how I interacted with Claude.  


# WPRDC Property Sales Pipeline

A small, reproducible pipeline for the
[Allegheny County Property Sale Transactions](https://data.wprdc.org/dataset/real-estate-sales)
dataset on the Western Pennsylvania Regional Data Center (WPRDC). It downloads the data
through the CKAN API (actually, a wrapper -- see [design decision 2](#2-every-http-request-is-logged-and-classified-as-part-of-the-pipeline-not-as-a-debugging-aid)), archives each download as delivered, loads it into DuckDB as an
immutable snapshot, runs data quality checks, and builds a cleaned table and a small
star schema. The deliverable is one output table that answers the question below.

- **Stack:** Python 3.13, DuckDB 1.5.6, `requests`, `pytest`. No other services.
- **Question:** Are family transfers of property in Allegheny County rising as the
  population ages?
- **Output table:** the view `analytics.family_transfers_by_year`.

## Contents

- [Running it](#running-it)
- [How it works](#how-it-works)
- [Re-runs and dataset updates](#re-runs-and-dataset-updates)
- [Data quality checks](#data-quality-checks)
- [The question and the output table](#the-question-and-the-output-table)
- [Three most important design decisions](#three-most-important-design-decisions)
- [What surprised me in the data](#what-surprised-me-in-the-data)
- [At 100x scale, or with daily refreshes](#at-100x-scale-or-with-daily-refreshes)
- [What I didn't do, and why](#what-i-didnt-do-and-why)
- [AI usage](#ai-usage)

## Running it

```sh
python -m venv .venv
.venv/Scripts/activate          # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python -m wprdc.pipeline        # ingest, load_raw, transform, analytics
```

The first run downloads about 105 MB and builds `data/wprdc.duckdb` (about 300 MB with
a few snapshots). Everything under `data/` is created by the pipeline and is not in git.
The run ends by printing the output table.

`python -m wprdc.pipeline --force` downloads even when CKAN reports no change. Each
step can also be run on its own (`python -m wprdc.ingest`, `wprdc.load_raw`,
`wprdc.transform`, `wprdc.analytics`). Maintenance options are on the steps:
`python -m wprdc.load_raw --prune-keep N` drops the rows of all but the N newest
snapshots, and `--rebuild` reloads the raw layer from the CSV archive.

To browse the tables, open the database with the DuckDB 1.5.6 CLI, for example
`duckdb data/wprdc.duckdb -ui`. The versions are pinned because a newer DuckDB writes
a storage format that older CLIs cannot open.

## How it works

```
CKAN API ──> data/raw/…/*.csv            every download, unchanged and kept   (ingest)
               │
               ▼
          raw.property_sales             one immutable snapshot per distinct file (load_raw)
               │
               ▼
          staging.property_sales_typed   typed and trimmed, every row          (transform)
               ├── checks ──> dq.*       results, steward's issue list, alerts
               ▼
          clean.property_sales           minus quarantined rows, with dq_flags
               │
               ▼
          analytics.sales + dimensions   star schema                           (analytics)
               │
               ▼
          analytics.family_transfers_by_year   the output table (a view)

ops.*  logs every run, every HTTP request and every downloaded file
```

| Schema | What it holds |
|---|---|
| `ops` | `fetch_run` (one row per command run), `fetch_attempt` (every HTTP request: timing, status, response classification, headers), `fetch_resource` (every file downloaded, with its SHA-256, row count and CKAN `last_modified`) |
| `raw` | `property_sales`: all source columns as text, plus `snapshot_id` and a row hash. `v_snapshots` lists snapshots; `v_snapshot_changes` counts rows added and removed between consecutive snapshots |
| `staging` | `property_sales_typed`: the latest snapshot with dates and prices typed, text trimmed, and a record key |
| `dq` | Check results per snapshot, failure rates, the issue list with resolutions, and alerts |
| `clean` | `property_sales`: the typed rows minus quarantined ones, with a `dq_flags` list |
| `analytics` | Fact `sales`; dimensions `parcel`, `municipality` (with Pittsburgh, Clairton, Duquesne and McKeesport wards rolled up), `school_district`, `sale_type`, `instrument_type`; the output view |

How the analytics tables join:

```
analytics.municipality    analytics.school_district
           └──────────────────┬───────┘
                      analytics.parcel
                              │
analytics.sale_type ── analytics.sales ── analytics.instrument_type
                              │
                              ▼
             analytics.family_transfers_by_year
```

**Ingest** asks CKAN for the resource's metadata and skips the download if
`last_modified` matches the last successful fetch. Otherwise it downloads the full CSV
dump to a temporary name, renames it into a run-stamped path once complete, and checks
that every row has as many fields as the header. It also compares the row count with
CKAN's reported total and prints a warning if they differ.

**load_raw** checks the header against the expected columns (an added, missing or
reordered column fails the run) and loads each new file as one snapshot, in one
transaction. If DuckDB loads a different number of rows than ingest counted in the
file, the load fails and is rolled back. The snapshot ID is the file's SHA-256.

**transform** types the latest snapshot, evaluates the checks, and rebuilds the clean
table. **analytics** rebuilds the star schema from the clean table and prints the
output. Each step is one logged run in one transaction: if it fails, the run is
recorded as failed and the previous tables stay in place.

## Re-runs and dataset updates

Running the pipeline twice changes nothing:

- **Ingest** downloads nothing if CKAN's `last_modified` is unchanged. A forced
  download of an identical file produces the same SHA-256, so it is recognized as an
  existing snapshot and not loaded again.
- **load_raw** loads a snapshot in a single transaction, so a half-loaded snapshot
  cannot exist, and a snapshot that is already loaded is skipped.
- **Check results and alerts** are written once per (snapshot, check version).
  Re-evaluating the same snapshot writes nothing.
- **Clean and analytics tables** are rebuilt in full from the latest snapshot. Every
  rule depends only on the data and the snapshot's download date, never on the date
  the pipeline runs, so the same snapshot always gives the same tables.

WPRDC republishes the whole file, usually early each morning. When `last_modified`
changes, the pipeline downloads the new file and adds it as a new snapshot; older
snapshots and every downloaded CSV are kept. Because each snapshot is complete,
deletions in the source are visible, and comparing row hashes between snapshots shows
exactly what changed. In the three snapshots taken so far (Oct 26, 28 and 30), each
refresh added about 500 row versions and removed about 200–240, out of about 507,000
rows. The steward's issue list notices when a flagged record is fixed, replaced by a
new version, or removed.

The CSV archive is the permanent record. The DuckDB raw tables are working storage:
old snapshots can be pruned, and the raw layer can be rebuilt from the archive.

## Data quality checks

The checks are defined in [`wprdc/checks.py`](wprdc/checks.py). Each one is a SQL
condition that selects failing records, plus an action. Every check below came from
profiling this dataset. Counts are from the Oct 30 snapshot (506,991 rows).

| Check | Rule | Action | Failing |
|---|---|---|---|
| `sale_date_invalid` | Sale date unparseable or before 2012 (the source's apparent start) | quarantine | 0 |
| `record_date_invalid` | Record date unparseable or before 2012, e.g. year `0212` | quarantine | 4 |
| `record_date_future` | Record date after the day the file was downloaded | quarantine | 0 (1 on Oct 28) |
| `record_date_missing` | No record date | quarantine | 1,248 |
| `record_before_sale` | Recorded before the sale date | flag | 726 |
| `possible_duplicate` | Same parcel and sale date, no real deed reference, differing only in how the placeholder zeros are spelled | flag | 43 |
| `key_not_unique` | Two rows share the record key | flag + alert | 0 |
| `sale_code_unmapped` | A sale code the classification doesn't know | flag + alert | 0 |
| `parcel_location_varies` | A parcel appears in more than one municipality or school district | flag + alert | 0 |

What each outcome means:

- **Quarantine:** the record is impossible and is held out of `clean.property_sales`.
- **Flag:** the record is doubtful but may be legitimate. It stays in, with the check
  in its `dq_flags`. For example, a corrective deed can be recorded before its sale
  date.
- **Fail the run:** reserved for structural problems, where carrying on would be
  wrong: a changed header, a row with the wrong number of fields, DuckDB loading a
  different number of rows than the file contains, or a table that can't be built. The
  run is marked failed and the previous tables stay. (A row count that differs from
  CKAN's reported total only prints a warning for now; see below.)

The checks serve two people:

- **The data steward** works through `dq.v_open_issues` at their own pace. Fixes
  happen in the county's source system. On the next refresh the pipeline records each
  resolved issue as `fixed`, `replaced` (linked to the new record) or `removed`, so a
  deletion is never reported as a fix. Changing a check's rule records `rule_changed`.
- **The data engineer** gets an alert in `dq.alert` when a check's failure rate more
  than doubles, by at least 10 records, between snapshots. A check whose normal count
  is zero (the last three above) alerts on its first failure. Alerts are recorded and
  printed where an email would be sent; sending the email is not built.

### Changing or adding a check

Checks are changed in code by the engineer and reviewed through git. After any change,
run `python -m wprdc.pipeline`. The new definitions apply to the current snapshot even
if WPRDC hasn't published a new file.

**To change a check from quarantine to flag** (for example, `record_date_missing`),
change its `action="quarantine"` to `action="flag"` in
[`wprdc/checks.py`](wprdc/checks.py). On the next run, its records move into
`clean.property_sales` with the check's name in `dq_flags`, and its open issues stay
open with their action updated. The tests don't depend on any check's action, so
`python -m pytest` still passes.

**To change what a check catches**, edit its `where` condition. This gives the check a
new version. Its open issues that no longer fail close as `rule_changed`, and the
engineer gets a `definition_changed` notice instead of a rate alert.

**To add a check**, append a `Check` to `CHECKS` with:

- an `id`
- a `description`
- an `action` (`quarantine` or `flag`)
- a `where` condition: SQL over `staging.property_sales_typed` selecting the failing
  records. A subquery refers to that table as `typed`.

Set `alert="any"` if the check should normally find nothing and should alert on its
first failure. Add a test for the condition in [`tests/test_dq.py`](tests/test_dq.py).

**To remove a check**, set `retired=True`. Don't delete it, so its history stays
readable.

## The question and the output table

**Are family transfers of property in Allegheny County rising as the population ages?**

The hypothesis is that as owners age, more property passes to family members, either
directly, as a "love and affection" transfer (sale code 3, typically for $1), or through
an estate after a death. The view `analytics.family_transfers_by_year` counts both,
by year, against all recorded transfers. Current output (Oct 30 snapshot):

| sale_year | all_transfers | family_transfers | family_share | estate_family_transfers | estate_family_share | not_analyzed_transfers |
|---:|---:|---:|---:|---:|---:|---:|
| 2012 | 33,459 | 7,138 | 0.2133 | 224 | 0.0067 | 9,903 |
| 2013 | 33,761 | 6,519 | 0.1931 | 240 | 0.0071 | 882 |
| 2014 | 33,307 | 6,719 | 0.2017 | 206 | 0.0062 | 120 |
| 2015 | 34,416 | 6,752 | 0.1962 | 185 | 0.0054 | 108 |
| 2016 | 35,856 | 6,708 | 0.1871 | 213 | 0.0059 | 40 |
| 2017 | 35,263 | 6,490 | 0.1840 | 181 | 0.0051 | 473 |
| 2018 | 36,472 | 6,498 | 0.1782 | 200 | 0.0055 | 1,999 |
| 2019 | 35,666 | 6,605 | 0.1852 | 219 | 0.0061 | 1,665 |
| 2020 | 35,342 | 7,027 | 0.1988 | 178 | 0.0050 | 6 |
| 2021 | 40,225 | 8,061 | 0.2004 | 280 | 0.0070 | 5 |
| 2022 | 34,826 | 6,963 | 0.1999 | 275 | 0.0079 | 3 |
| 2023 | 30,859 | 6,747 | 0.2186 | 301 | 0.0098 | 2 |
| 2024 | 31,111 | 6,835 | 0.2197 | 293 | 0.0094 | 2 |
| 2025 | 31,555 | 7,191 | 0.2279 | 330 | 0.0105 | 0 |

| Column | Meaning |
|---|---|
| `all_transfers` | Every recorded deed transfer that year, of any kind, including $1 transfers. A "sale" in this dataset is any recorded transfer, not only a market sale. |
| `family_transfers` | Love and affection transfers (code 3), not on an estate deed |
| `estate_family_transfers` | Estate deeds (executor's, fiduciary, administrator's, trustee's, survivorship, transfer on death…) passing property for nothing: coded love and affection, or a price of $10 or less |
| `*_share` | The count divided by `all_transfers` |
| `not_analyzed_transfers` | Sales the assessor never coded (code AA). These can't be coded love and affection, so family counts are understated in years with many of them: 2012 (30% of the year), 2018 and 2019. |

Only complete years are shown: years before the year of the snapshot's download
date. 2026 is left out without hard-coding a year.

**A first reading:** the number of family transfers is roughly flat, around 6,500 to
7,200 a year, apart from 2021. Their share has risen since 2018 mostly because other
transfers fell after 2021. Estate transfers to family have nearly doubled since 2020
(178 to 330) but remain about 1% of transfers. That is why counts sit next to shares
in the table: a share can rise because the denominator shrank. A per-parcel rate would
be a better measure, but it needs the assessments dataset, and the exercise allows only
one dataset.

**How sales are classified.** Every sale gets exactly one `sale_class` in
`analytics.sales`, from rules in [`wprdc/sale_classes.py`](wprdc/sale_classes.py):
an estate deed for nothing is `estate_family`; any other estate deed, or code 37
"estate sale", is `estate_sale`; otherwise the class comes from the sale code
(`family`, `market`, `distressed` or `other`). The mapping is maintained by hand and
reviewed in git, and `sale_code_unmapped` catches a new county code instead of letting
it be misclassified silently. The other classes aren't needed for this question; they
are there so the fact table is useful for others.

## Three most important design decisions

### 1. The raw layer is complete, immutable snapshots, backed by an archive of every file

Every download is kept as delivered, and each distinct file becomes one complete
snapshot in `raw.property_sales`. Recreating the data as of any day is a single
`WHERE snapshot_id = …`. A row hash on every row makes comparing two snapshots cheap.

**Rejected:** a history table with one row per row version, opened and closed by
snapshot (slowly changing dimension type 2). It stores far less, but rebuilding the
data as of a given day becomes a more complex query and harder to trust. At this size
(about 507,000 rows, 105 MB per snapshot), full snapshots are affordable, and old ones
can be pruned because the CSV archive can rebuild them. At 100x scale I'd revisit
this (below).

### 2. Every HTTP request is logged and classified, as part of the pipeline, not as a debugging aid

Before starting, I had seen intermittent failures from WPRDC with the `ckanapi`
library: blank responses, then HTML where JSON was expected. So I replaced it with a
small `requests` client that logs every attempt to `ops.fetch_attempt`: timing, HTTP
status, headers, a snippet of the body, and a classification of the response by its
content rather than its status code (a 200 response with an HTML page is a failure).
Retries happen, but they are recorded, not hidden. This is how I found that WPRDC's
502 errors come from its own server rather than the CDN (below).

**Rejected:** `ckanapi` with retry logic around it. It would have worked most of the
time, but failures would have been invisible, and I would have no evidence to bring
to WPRDC about how reliable the API is.

### 3. Data quality: flag by default, quarantine only the impossible, fail the run only on structural problems

Doubtful records stay in the clean table, flagged, and go on the data steward's list.
Only records that can't be true (a record date in the year 212 or after the download
date, for example) are held out. A run fails only when the pipeline can't sensibly
carry on, such as a changed header.

**Rejected:** failing the run whenever a check finds bad data, including when the
record key is not unique. The key (`PARID`, `DEEDBOOK`, `DEEDPAGE`, `SALEDATE`) is my
inference from the data, not something the county guarantees. If it turned out wrong,
a strict pipeline would stop delivering data entirely. Instead, a collision flags the
records and alerts the engineer immediately. **Also rejected:** quarantining every
doubtful record. "Recorded before the sale" looks impossible, but corrective deeds,
and the county's own `RECORD YR <> SALE YR` sale code, make some of those legitimate.

## What surprised me in the data
_Editor's note:_ the only thing that truly surprised me here was the 1,248 records that have no record date.  That seems to be an important field!  In fact, I coded this as a quarantine data quality check, with misgivings, since the current data is what it is, and I didn't want to come in and question someone else's previous decisions.  However, I left it as a quarantine check for two reasons: 1) to prevent any future data coming in from having no record date; and 2) so that the reviewer of this assignment could see how easy (or not) it would be to make the change from quarantine to flag on that check.  

I'm leaving the list of "surprises" that Claude Code came up with below, with my comments on each of them.  

- **The download file mixes line endings.** The header ends in CRLF and the data rows
  in LF. DuckDB's strict CSV reader rejects the file, so it is loaded in relaxed mode,
  with a separate structural check that every row has the header's width. 
  - Didn't even notice this, and not surprised by it, nor bothered by it. In fact, I am glad someone didn't waste time cleaning up the header to match the data rows, as that is easily handled downstream.
- **`_id` is regenerated on every refresh.** The values start near 17 million and
  change each time, so they can't be used as a key. 
  - Not surprised or bothered by this.  It would be nice to have a simple provided id like this we could trust, but that doesn't always happen.
- **WPRDC changes very little each refresh.** About 500 row versions are added and
  200–240 removed a day, out of about 507,000. The other rows hash identically even
  though their `_id` values changed. 
  - Not surprised by this, and I don't understand why Claude expected more.
- **The obvious natural key is only unique by accident.** For the 2,821 records with no
  real deed reference (mostly mobile home sales), placeholder deed numbers are spelled
  `0`, `00`, `000`, and those spellings are all that separates otherwise identical
  records. That became the `possible_duplicate` check.  
  - Not surprised, but I am still unsure what the natural key is in this data, and bothered that I can't find a definitive one.
- **Impossible dates:** a record date of `0212-08-01` (presumably 2012), record dates
  before the data's 2012 start, and a record date in the future. 
  - Not surprised this exists in the data, as it is only one and the world is imperfect.  This is coded as a quarantine check going forward, however, to prevent this from occurring from now on.
- **1,248 records have no record date.** Most are mobile home sales, certificates of
  transfer, treasurer deeds and lease memoranda, which may never be recorded. So "missing" may mean "not
  applicable". I quarantined them for now, but I'd want the data steward's view; making
  it a flag is a one-word change (see [Changing or adding a check](#changing-or-adding-a-check)).  
  - As described above, this is the only thing that really surprised me in the data so far.
- **Most transfers are not market sales.** Of about 506,000 sales, 98,813 carry the
  assessor's "valid sale" codes. Over half are coded as multi-parcel sales, quit claims,
  invalid for time on market, corrective deeds and similar.  
  - This didn't surprise me, as I didn't even know what is meant by "market sales" before I started this project.
- **Sale code 37, "estate sale", is applied unevenly:** 0 uses in 2012, 451 in 2015, 33
  in 2022. That is why it never feeds the family counts.  
  - This doesn't surprise me, it reflects changing business processes and probably staffing changes over the years.  Automation, if possible, would help bring consistency to the data going forward.
- **WPRDC's 502 errors come from its own nginx server, not the CDN.** They return in
  about 80 ms with nginx's error page, meaning the CKAN application behind it refused
  the connection. 
  - Entirely Claude's surprise, as I had no understanding of how the connections worked before Claude educated me. I still would not consider myself knowledgeable in this area at all. I'm really only including it here to pass on this information to you, in case anyone else has been complaining about flaky connections. Perhaps this insight might help, if it isn't known already.


## At 100x scale, or with daily refreshes

About 50 million rows and 10 GB per download, every day:

- **Raw storage.** Full daily snapshots would be about 3.6 TB a year. I'd switch the
  raw layer to the row-version history I rejected above (decision 1), or to columnar
  snapshots (Parquet) with a short retention period. The row hash is already
  computed, so the load becomes "insert the rows whose hash is new, close the rows whose
  hash disappeared".
- **Downloads.** A full download is still the only way to see deletions in this
  source. I'd ask WPRDC whether it can publish a change feed or a modified timestamp
  per row. With one, the CKAN datastore API could fetch only the changed rows (the
  pipeline can already page through it: `--strategy datastore`).
- **Transforms.** Rebuilding clean and analytics in full each run would become an
  incremental merge on the record key, touching only changed rows. Most checks are
  per-row and could also run only on changed rows. The grouped checks (duplicates,
  key uniqueness, parcel location) would need the affected keys.
- **Engine.** DuckDB on one machine handles 50 million rows comfortably, but a single
  database file allows one writer. With several pipelines or concurrent users, I'd move
  to Parquet files queried by DuckDB, or to a warehouse.
- **Daily operation.** Schedule it with an orchestrator for retries and history, and
  actually send the alert emails. Add a freshness alert for when `last_modified` stops
  changing. Retune the alert thresholds: with about 500 changes a day, rate jumps are
  noisy.

## What I didn't do, and why

- **Send alert emails.** Alerts are recorded in `dq.alert` and printed where the email
  would be sent. Configuring email didn't seem worth it for a take-home.
- **Scheduling.** The pipeline is one command that's safe to re-run, so cron or Task
  Scheduler is enough to run it daily.
- **Other datasets.** Not allowed by the exercise. A per-parcel rate would need the
  assessments dataset; I rejected estimating parcel counts from the sales data, because
  it misses exactly the long-held homes the question is about.
- **Charts, or a by-municipality breakdown.** The deliverable is one table. The star
  schema makes a by-municipality breakdown a short query.
- **Backfill checks over old snapshots.** Only the latest snapshot is evaluated. A new
  or changed check starts its history from the next run.
- **Track who fixed a record.** The source data doesn't say. The pipeline records only
  that, when and how an issue was resolved.
- **A steward interface.** The steward browses `dq.v_open_issues` in the DuckDB UI.
- **Decide on missing record dates.** Quarantined for now, pending the steward (above).
- **Fail on a row count that differs from CKAN's total.** Ingest only warns. The
  counts have matched on every download so far, and this should become a run failure.
- **Packaging, CI or containers.** The setup is a virtual environment and
  `requirements.txt` with exact versions, which seemed in keeping with "small".
- **Three edge cases** found in the final code review. None of them occurs in the
  current data: a null description could win "most frequent" for a code; a parcel's
  location can come from its latest sale even when that location is null; no test
  covers a failed analytics run being recorded as failed.

## AI usage

Note:  [`docs/AI_USAGE.md`](docs/AI_USAGE.md) is a useful write-up of my AI usage in this project.  Below is just a quick summary.

I used Claude.ai for early exploration of the datasets and the CKAN API, and Claude
Code (Claude Opus 5.5) to write all of the code, SQL and tests and to run them.
I set the direction and made the design decisions, and I'm prepared to walk through and
defend any part of the code. [`docs/AI_USAGE.md`](docs/AI_USAGE.md) lists which
decisions were mine, where I questioned or corrected the AI, and the AI's mistakes
caught along the way. It links the full session transcripts in
[`docs/ai-sessions/`](docs/ai-sessions/). The design specs and implementation plans
for the data quality and analytics layers are in [`docs/superpowers/`](docs/superpowers/).
