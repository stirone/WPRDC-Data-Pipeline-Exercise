# AI Usage

Claude.ai and Claude Code in VS Code were used extensively on this exercise. This document explains when each tool was used,
how the work was divided, and where the important decisions came from, so a reviewer
can judge both the code and how I worked with the AI assistant.

The full session transcripts are linked below. This page is a guide to them.

## Tools

| Tool | Used for |
|---|---|
| **Claude.ai** | Early exploration: learning about the four candidate datasets and the WPRDC CKAN API. This session was started in the car on my drive back from Buffalo, interacting by voice through Apple CarPlay. I then picked it up later on my laptop and interacted with it while testing out the CKAN API, which returned errors initially, driving my decision to include logging of connection issues and bad responses.|
| **Claude Code** (CLI, Claude Opus 5.5) | Building the pipeline: writing the Python and SQL, writing and running tests, calling the live API, querying the data, and making git commits. |

## Division of labor

**Claude Code wrote all of the code, SQL and tests**, and ran the commands.
Every commit carries a `Co-Authored-By: Claude` trailer.

**I (Stephen Tirone) set the direction and made the design decisions.** I reviewed the proposals,
asked for explanations where I didn't follow the architecture, and pushed back when
something seemed overstated or more complex than needed. The sections below list the
specific cases. 

## Session log

| Date | Session | Topics | Transcript |
|---|---|---|---|
| 2026-10-25 | Claude.ai | Dataset exploration; first logging-schema idea | [`ai-sessions/2026-10-25-wprdc_conversation_transcript.txt`](ai-sessions/2026-10-25-wprdc_conversation_transcript.txt) |
| 2026-10-26 | Claude Code #1 | Fetch logging, raw landing, raw snapshots, DuckDB version pin, row hashes | [`ai-sessions/2026-10-26-ingest-and-raw-layer.txt`](ai-sessions/2026-10-26-ingest-and-raw-layer.txt) |
| 2026-10-28 | Claude Code #2 | Status review; second snapshot (+507 / −206 row versions, unchanged rows not rewritten); why a run makes 3 requests; re-run check; DuckDB UI for browsing; natural key investigation; data quality design (quarantine vs. flag, steward's issue list, rate-jump alerts, key collisions don't fail runs), plan, implementation and review fixes | [`ai-sessions/2026-10-28-220407-where-are-we-on-this-project.txt`](ai-sessions/2026-10-28-220407-where-are-we-on-this-project.txt) |
| 2026-10-30 | Claude Code #3 | Third snapshot; typed table moved from `clean` to a new `staging` schema; choosing the analytical question; naming the layer `analytics` and the fact table `sales` | [`ai-sessions/2026-10-30-130735-lets-do-another-pipeline-run.txt`](ai-sessions/2026-10-30-130735-lets-do-another-pipeline-run.txt) |
| 2026-10-30 | Claude Code #4 | Analytics layer design (estate deeds, denominator, parcel-count estimate rejected, table not chart, output as a view), plan, implementation, final review and fix; `python -m wprdc.pipeline`; push to GitHub | [`ai-sessions/2026-10-30-153811-lets-pick-up-where-left-off.txt`](ai-sessions/2026-10-30-153811-lets-pick-up-where-left-off.txt) |
| 2026-10-30 | Claude Code #5 | What `not_analyzed_transfers` means and how to explain it to others: column comments in DuckDB and a footnote under the printed table | [`ai-sessions/2026-10-30-205055-what-is-this-notanalyzed-column-in-the-analyti.txt`](ai-sessions/2026-10-30-205055-what-is-this-notanalyzed-column-in-the-analyti.txt) |
| 2026-10-31 | Claude Code #6 | Time spent; remaining requirements; README draft, checked against the code and database; found the row-count gap (below) and documented it here; CKAN API vs. the `ckanapi` library, explained up front in the README | [`ai-sessions/2026-10-31-0914720-are-you-able-to-tell-me-how-much-time-we-have-wor.txt`](ai-sessions/2026-10-31-0914720-are-you-able-to-tell-me-how-much-time-we-have-wor.txt) |
| 2026-11-01 | Claude Code #7 | What `python -m pytest` tests (the code, not the data); ER diagram of the analytics tables for the README; tests moved from `record_date_missing` to `record_date_invalid`, so switching that check to a flag doesn't break them (checked: 109 pass either way); spell check; README section on changing or adding a check | [`ai-sessions/2026-11-01-104251-im-sorry-but-i-do-not-understand-what-the-pyth.txt`](ai-sessions/2026-11-01-104251-im-sorry-but-i-do-not-understand-what-the-pyth.txt) |

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
- **Data quality has two audiences.** The data steward works a list of flagged
  records at their own discretion, with no email. I, as the data engineer, get an
  immediate email (designed, not yet built) when a check's failure rate jumps,
  because that usually means something changed upstream.
- **Flag by default; quarantine only impossible values.** Records are held out of
  the clean table only for dates before 2012 (sale or record) and record dates
  after the download date. "Recorded before the sale" is flagged, not quarantined,
  after Claude pointed out that corrective deeds and the county's own
  `RECORD YR <> SALE YR` code make some of those legitimate.
- **Missing record dates are quarantined, with misgivings.** Most of the 1,248 are
  mobile home sales and other instruments that may never be recorded. I want the
  data steward's view before deciding; switching to a flag is a one-word change.
- **The tests don't depend on `record_date_missing` staying a quarantine check.** I
  left it quarantined so a reviewer could try switching it to a flag. Several tests
  had used it as their example of a quarantine check, so trying the switch would have
  made 9 of them fail. They now use `record_date_invalid`, which should always
  quarantine, and I dropped the test that pinned down which checks quarantine. All
  109 tests pass either way.
- **"In the future" means after the download date,** so rebuilding an old snapshot
  gives the same answer as the original run.
- **Fixes happen in the source system.** The pipeline records that an issue was
  resolved and when, as fixed, replaced, removed or rule changed. I dropped "who
  fixed it" as out of reach for now.
- **Only the engineer changes checks, in code,** reviewed through git. I expect
  the data steward and the data engineer to work on the rules together.
- **Kept the row hash** after asking whether it was needed. It is not required for
  correctness, but it makes snapshot comparison cheap and is the natural answer
  at 100x scale.
- **Moved the typed table out of `clean`.** It holds every row, including the ones
  the checks quarantine, so it belongs in an intermediate `staging` schema, not next
  to the table meant for analysis.
- **The question.** I first suggested property values over time and by area, then
  chose family transfers instead, with the hypothesis that they rise as the
  population ages.
- **Naming:** `analytics` for the layer (Claude suggested it over `gold`, which this
  repo's other names don't follow), and `sales` for the fact table. Claude had
  proposed `transfer`, since most records aren't sales in the everyday sense; I
  chose `sales` because the source calls every record a sale, so the vocabulary stays
  the same from source to output.
- **A star schema, with the "one output table" as a view.** I read "one output table"
  as the result of a query, not another physical table, so the star schema supports
  the question and the view answers it.
- **Estate transfers are their own class, and win over family** when a sale is both.
  After profiling showed most estate deeds sell at market prices, I chose to split
  them into estate transfers for nothing (`estate_family`) and estate sales at a
  price (`estate_sale`).
- **The denominator is all recorded transfers.** I had asked for a rate per 1,000
  parcels, which needs the assessments dataset; the exercise allows one dataset.
  When I asked whether parcels could be estimated from the sales data, Claude showed
  that the sales data sees only about half the county's parcels, and misses exactly
  the long-held homes the question is about.
- **2026 left out**, as an incomplete year.
- **A table, not a chart.** The exercise asks for a table, and this is a data
  engineering exercise, not an analytics one, so the analysis stays small.
- **Built the analytics plan in one session ("Native")** with one independent review
  of the whole branch at the end, rather than a separate builder and reviewer per
  task, to save time before the deadline.
- **Left the row-count gap in place** (see [Known gap](#known-gap-between-the-design-and-the-code)),
  so a reviewer can see what it takes to fix.

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

- **"Are we really using that natural key?"** Claude had been calling
  `PARID + DEEDBOOK + DEEDPAGE + SALEDATE` the key. Nothing used it yet, and on a
  closer look it was only unique because placeholder deed numbers are spelled
  differently (`0`, `00`, `000`). That led to the `possible_duplicate` check.
- **"How do we know this was resolved, if the record disappears?"** Claude's
  first answer treated a vanished flagged record as fixed. A deletion looks the
  same, so resolutions now distinguish fixed, replaced (linked to the new record)
  and removed.
- **Key uniqueness should not stop the pipeline.** The design had a duplicate key
  fail the run. I objected that the key is a guess from two snapshots two days
  apart, and a wrong guess would leave us dead in the water. A collision now
  flags the records and alerts me; only structural problems fail a run.
- **The checks serve the data steward, not me.** I corrected Claude's assumption
  that the flagged-record list was for the engineer.
- **"What was the plan here?"** Seeing `clean.property_sales_typed` next to
  `clean.property_sales`, I asked what each was for. That led to moving the typed
  table to `staging`, and to the analytics layer discussion.
- **"We are creating a data mart specific to this question, correct?"** The answer
  shaped the design: a general star schema for all property transfers, with only a
  thin view specific to the question.
- **"Someone else will have that question as well, so how will we communicate it?"**
  After asking what `not_analyzed_transfers` meant, I asked how a reader would find
  out. The explanation now travels with the data, as column comments in DuckDB and a
  footnote under the printed table, as well as in the README.

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
- **An output that could change without a rebuild.** The final review of the
  analytics layer found that the output view read its year cutoff from the clean
  table every time it was queried. If a new snapshot reached the clean layer and the
  analytics step then failed, the view would mix new and old data. The mistake was
  in Claude's plan, not the build. The cutoff is now fixed when the analytics layer
  is built, and a test covers it.
- **A wrong description of the data.** Claude's first draft of the column comments
  described a family transfer as one "between people with the same surname". The
  data has no names, and no such rule exists. Claude caught it by checking the
  wording against `sale_classes.py`, before committing.
- **A check described as existing that never did.** See the next section.

## Known gap between the design and the code

The data quality design says a run fails when "the downloaded row count doesn't
match CKAN's reported total (exists: ingest)". It doesn't. Since the first commit,
[`wprdc/ingest.py`](../wprdc/ingest.py) has only printed a warning when the counts
differ, and the file is still loaded. (A separate check does fail the run: load_raw
fails if DuckDB loads a different number of rows than ingest counted in the file.)

I've left this unfixed on purpose. It shows what maintaining this code is like:
someone other than its author has to find out what the code really does, rather
than what the documents say it does, and then change it safely.

**Why it went unnoticed until Oct 31:**

1. **The first summary overstated the code.** At the end of the Oct 26 session,
   Claude described the check as enforced: "the row count must equal CKAN's total
   (506,425 of 506,425). Only after that passes does lax parsing happen." The code it
   had just written only printed a warning.
2. **The design relied on that summary.** On Oct 28, Claude told me "Ingest already
   validates the file", without re-reading the code, and the spec recorded it as
   "(exists: ingest)".
3. **So nothing tested it.** Because the spec said the check existed, the
   implementation plan had no task for it and no test was written. The reviews
   compared new code against the plan, so a claim about older code was outside what
   any review looked at.
4. **The data never triggered it.** CKAN's total matched the downloaded row count on
   all four successful downloads, so the warning never printed and no run behaved
   unexpectedly.
5. **It was found by checking the README against the code.** While drafting the
   README on Oct 31, Claude checked each claim against the code before writing it,
   and this one didn't hold.

**What fixing it involves:** in `ingest.py`, raise an error instead of printing the
warning, recording the fetch as failed with the downloaded file's details (the
way other ingest failures are recorded), so the file is kept but not loaded.
Then add a test that fakes a CKAN total different from the file's row count, and
update the README, which currently lists this under "what I didn't do".

**The lesson:** a design document's claims about existing behavior should point to
the code or a test that shows it. "(exists: ingest)" pointed to nothing.

## Findings from the investigation

These came out of running the pipeline and querying the results. They fed the data
quality checks and the README's "what surprised me" section, which has the full list.

- **WPRDC's 502 errors come from its own nginx server, not the CDN.** They return in
  about 80 ms with nginx's error page, which means the CKAN application behind it
  refused the connection. Captured in `ops.fetch_attempt`.
- **The CSV dump mixes line endings:** a CRLF header with LF data rows. DuckDB's
  strict CSV parser rejects the file, so a separate structural check was added.
- **`_id` is regenerated on each refresh** (values start near 17 million), so it
  can't be used as a key.
- **`RECORDDATE` contains `0212-08-01`** (presumably a typo for 2012) **and dates in
  the future.**
- **WPRDC's refresh leaves unchanged rows alone.** Between the Oct 26 and Oct 28
  snapshots (506,425 → 506,726 rows), only 507 row versions were added and 206
  removed. The other ~506,000 rows hash identically despite the new `_id` values,
  so comparing snapshots by row hash finds only the rows that really changed. The
  Oct 30 refresh was similar: 503 added, 238 removed.

## How the AI's work was verified

- **Automated tests:** 109, all passing (`python -m pytest`). They cover response
  classification, retries, raw loading, re-runs, schema drift, rollback, pruning and
  rebuilding (including a replay of the real blank → HTML → JSON failure sequence);
  each data quality check, issue resolution and alerts; and the sale
  classification, the dimensions, the output view and identical rebuilds.
- **Real data:** each stage was run against the live API and real data, with the
  results checked by queries. For example: the row count matched CKAN's total of
  506,425, re-runs skipped as expected, and the raw layer was rebuilt from the CSV
  archive.
- **Independent review:** the data quality and analytics work each ended with a
  review of the whole branch by a separate Claude agent that hadn't written the
  code. Its important findings were fixed before merging. Three minor ones that
  can't occur in the current data are listed in the README under "what I didn't do".
- **The limit:** tests and reviews were written from the design documents, so they
  check what the documents ask for. A wrong statement in a document about code that
  already existed was outside what they could catch (see the known gap above).
