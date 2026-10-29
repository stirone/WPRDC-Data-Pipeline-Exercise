# Data quality checks — design

Date: 2026-10-28. Status: draft for approval.

## Intent

Property sales records have bad values: a record date in the year 212, record
dates after the download, record dates before the sale. The checks should catch
these on every refresh and route them to the two people who care:

- **The data steward** works through problem records at their own pace. They
  browse a table of issues; nobody emails them. Fixes happen in the county's
  source system, and the pipeline notices when a problem goes away.
- **The data engineer** (Stephen) is told immediately, by email, when a check's
  failure rate jumps, because that usually means something changed upstream.

The exercise also requires checks that actually fail on bad data, at least one
written because of something found in this dataset. Every check below came from
profiling this dataset.

## Goals

- Clean sales table built from the latest raw snapshot, with impossible records
  held out (quarantined) and doubtful ones kept but flagged.
- One steward-facing list of issues that persists across runs, and records when
  and how each issue was resolved.
- Per-run history of every check's failure count and rate, with an alert when
  a rate jumps.
- Adding, changing or retiring a check is a small, reviewed code change.
- Re-running on the same snapshot changes nothing (no duplicate issues, rates
  or alerts).

## Non-goals

- **Sending email.** Designed for, not built. Alerts are recorded and printed at
  the point where the email would be sent.
- **Who fixed a record.** Dropped; we record that and when it was resolved.
- **Steward-editable rules.** Only the engineer changes checks, in code. The
  steward and engineer are expected to talk regularly about the rules.
- **A steward UI.** The steward browses the issue views, e.g. in the DuckDB UI.
- **Backfilling** a new or changed check over old snapshots.
- **The parcel / sales split and the analytical output table.** Decided
  separately, once the analytical question is chosen.

## Anti-goals

- A resolution that hides a non-fix: a deletion or a rule change must never be
  reported as "fixed".
- Rebuilds that give different answers. Every check result depends only on the
  snapshot and the check definition, never on the date the pipeline ran.
- An alert caused by the engineer's own rule change.

## Where it sits

```
raw.property_sales (latest snapshot)
   └─ clean.property_sales_typed   typed + trimmed, every row, with its key
        ├─ checks (wprdc/checks.py) ──> dq.check_result, dq.check_run
        └─ clean.property_sales        typed rows minus quarantined ones,
                                        with a flags column
dq.v_issues / dq.v_open_issues         steward's list, derived from dq.check_result
dq.alert                               rate jumps, for the engineer
```

`python -m wprdc.transform` builds the typed and clean tables and runs the
checks, as one logged run (`ops.fetch_run`, command `transform`). The planned
single `python -m wprdc.pipeline` command will call ingest, load_raw and
transform in turn.

## Record identity

`(PARID, DEEDBOOK, DEEDPAGE, SALEDATE)`, using the **source values unchanged**
(no trimming, placeholders not converted to null). It is unique in both
snapshots so far. It comes from the data, not from the county, so a check
watches it (`key_not_unique`, below) without depending on it.

Why not fewer columns: `PARID + DEEDBOOK + DEEDPAGE` is unique for the 503,905
records with a real deed reference, but not for the 2,821 records without one
(mostly mobile home sales), which need `SALEDATE`. Those are only distinguished
by how their placeholder zeros are spelled (`0`, `00`, `000`…), which the
"possible duplicate" check below covers.

A **deed reference is real** when neither DEEDBOOK nor DEEDPAGE is null, blank,
or all zeros.

## Typing

`clean.property_sales_typed` has one row per row of the latest snapshot:

- the four key columns, unchanged, plus `sale_key` (a hash of them, for joins)
- `sale_date`, `record_date`: `TRY_CAST` to DATE (null if unparseable)
- `price`: `TRY_CAST` to integer (the source has values like `1658.0`)
- text columns trimmed, empty strings as null
- `has_real_deed` (boolean, as defined above)
- `download_date`: the UTC date of the snapshot's first fetch, from
  `raw.v_snapshots.first_fetched_at`

Builder's call: exact column names and which text columns to keep.

## Checks

Each check is one entry in `wprdc/checks.py`:

```python
Check(
    id="record_date_missing",
    description="RECORDDATE is null",
    action="quarantine",          # or "flag"
    where="record_date IS NULL",  # SQL over clean.property_sales_typed
    retired=False,
)
```

- `where` selects the failing records. Its **version** is a hash of the
  normalized `where` text. Changing `action` or `description` does not change
  the version.
- **Add** a check: add an entry. **Change** one: edit it. **Retire** one: set
  `retired=True`; never delete it, so its history stays readable.

### Initial checks (counts from the 2026-10-28 snapshot)

| id | Rule | Action | Today |
|---|---|---|---|
| `sale_date_invalid` | `sale_date` null (unparseable) or before 2012-01-01 | quarantine | 0 |
| `record_date_invalid` | `record_date` not null and before 2012-01-01, or `RECORDDATE` present but unparseable | quarantine | 4 (years 212, 2006, 2008, 2011) |
| `record_date_future` | `record_date > download_date` | quarantine | 1 (Oct 29) |
| `record_date_missing` | `record_date` null and `RECORDDATE` null | quarantine | 1,248 |
| `record_before_sale` | both dates valid and `record_date < sale_date` | flag | 725 |
| `possible_duplicate` | no real deed, and another no-deed record has the same `PARID` and `SALEDATE` | flag | 17 groups (pairs) |

Notes:

- **`record_date_missing` is quarantined with misgivings.** Only 7 of the 1,248
  are ordinary deeds; most are mobile home sales (827), certificates of transfer,
  treasurer deeds and lease memoranda, mostly from 2012–2016. These may be
  instruments that are never recorded, so "missing" may mean "not applicable".
  To raise with the steward; likely to become a flag. That is a one-word change.
- `sale_date_invalid` catches nothing today. It guards the source's apparent
  2012 cutoff.
- A record failing several checks gets one result per check.

### Key collisions (flag and alert, never fail the run)

`key_not_unique` (flag): the record shares its key with another row of the
snapshot. 0 today.

The key is an assumption drawn from two snapshots two days apart, not a rule
the county guarantees, so a collision must not stop the pipeline. Instead:

- The colliding records stay in `clean.property_sales`, flagged, and appear in
  the steward's list.
- The engineer is alerted on the **first** collision (kind `key_collision`),
  not by the rate-jump rule: the baseline is zero, so any collision means the
  key assumption has just been shown wrong. The alert lists the colliding keys.
- Downstream keeps working, less precisely for those records: an issue is per
  (check, key), so it stays open while any record with that key fails and
  resolves when none does. If more than one successor matches a deed, the
  outcome is `replaced` with `replaced_by` left null.
- The engineer revises the key when ready, as a normal code change.

### What fails a run

Only structural problems, where the pipeline cannot sensibly carry on:

- the CSV's columns don't match the expected header (exists: `load_raw`)
- the downloaded row count doesn't match CKAN's reported total (exists: ingest)
- the typed table cannot be built at all

On failure the run is recorded as failed and the previous clean table is left
in place. Surprising data never fails a run; impossible values are quarantined
and doubtful ones flagged.

## Results and the steward's list

`dq.check_result`: one row per (snapshot, check, check version, failing record):
`snapshot_id, check_id, check_version, sale_key, PARID, DEEDBOOK, DEEDPAGE,
SALEDATE, action`. Written once per (snapshot, check version); a re-run on an
already-evaluated snapshot writes nothing. Results are kept even after a raw
snapshot is pruned (they are small).

`dq.check_run`: one row per (snapshot, check, check version): `n_failing,
n_rows, rate, evaluated_at, run_id`. This is the rate history.

Only the latest snapshot is evaluated. Snapshots are evaluated in fetch order,
so the first evaluation of a snapshot older than the last evaluated one is
skipped (cannot happen in normal use).

`dq.issue_resolution`: written when a snapshot is evaluated, for each issue
open before it that no longer fails in it: `check_id, sale_key,
resolved_snapshot_id, outcome, replaced_by_sale_key`. Deciding the outcome needs
that snapshot's raw rows, so it is decided then and stored, which keeps the
issue list readable after raw snapshots are pruned. It depends only on the
snapshot and the check definitions, so it is the same on a rebuild.

`dq.v_issues` is a view over `dq.check_result` and `dq.issue_resolution`. One
row per (check, record key) occurrence:

- `check_id`, record key columns, current `action`
- `first_seen_at`: download time of the first snapshot where it failed
- `last_seen_at`: download time of the most recent snapshot where it failed
- `status`: `open` or `resolved`
- `resolved_at`: download time of the first later evaluated snapshot where it
  no longer failed
- `outcome` (when resolved), decided for that snapshot:

| Outcome | Rule |
|---|---|
| `fixed` | The record (same key) is still in the snapshot and passes the check, at the same check version |
| `replaced` | The key is gone, the record had a real deed, and a record with the same `PARID, DEEDBOOK, DEEDPAGE` is present. `replaced_by` holds the new record's key |
| `removed` | The key is gone and there is no successor (always the case for records without a real deed) |
| `rule_changed` | The record is still present but the check version changed, or the check was retired |

- `dq.v_open_issues`: open rows only, the steward's working list.
- An issue that fails again after being resolved reopens as a new row with a
  new `first_seen_at`.
- Changing a check's `action` (quarantine to flag) releases those records into
  `clean.property_sales` but leaves their issues **open**: the problem still
  exists.

Alerts and resolutions are written at most once per (snapshot, check, check
version), so re-running the transform on the same snapshot adds nothing.

## Clean table

`clean.property_sales` = `clean.property_sales_typed` minus records failing
any check whose action in `checks.py` is currently `quarantine`, plus `dq_flags` (list of failing `flag` check
ids, empty if none). Rebuilt in full each run from the latest snapshot, inside
one transaction.

## Monitoring and alerts

After each evaluation, for each non-retired check, compare its new rate with
its previous rate **at the same check version**:

- **Alert** if `rate > 2 × previous rate` and `n_failing − previous n_failing
  ≥ 10`. Both values are constants in `checks.py`, to tune later (starting
  values, not derived from data).
- **No alert** for a check's first evaluation (no baseline).
- **Definition changed**: when the version differs from the previous
  evaluation, record a notice instead of an alert. The rate baseline restarts.

`dq.alert`: `alert_id, created_at, run_id, snapshot_id, check_id,
check_version, kind ('rate_jump' | 'key_collision' | 'definition_changed'), previous_rate,
new_rate, previous_n, new_n, subject, body, notified_at`.

Notification goes through one function, `notify(alert)`. For now it prints the
subject and body and sets `notified_at`. The email version (SMTP settings from
environment variables) replaces only that function. Subject line, e.g.:

```
[WPRDC DQ ALERT] record_before_sale: 0.14% -> 0.61% (snapshot dd6c1944, 2026-10-29)
```

The body gives the old and new counts and rates, the snapshot and its download
time, the previous snapshot, and a query to list the new failures.

## Tests

- Each check, on small fixture rows that should and shouldn't fail it
  (including the real `0212-08-01` row's values).
- `key_not_unique` flagging both records, alerting on the first collision,
  and the run still succeeding.
- A structural failure (e.g. typed table cannot be built) failing the run and
  leaving the previous clean table in place.
- Re-running on the same snapshot: no new results, runs or alerts.
- Each resolution outcome (`fixed`, `replaced`, `removed`, `rule_changed`),
  plus reopening.
- Action change keeps issues open and moves records into the clean table.
- Alert rule: jump, no jump, first evaluation, definition changed.

## Decided by Stephen

- Steward works the issue list at their discretion; no email to them.
- Engineer gets an immediate email on a rate jump, notable subject line; email
  not implemented now.
- Flag, don't quarantine, except: dates before 2012 (sale or record), record
  date after the download date, and (with misgivings) missing record date.
- "In the future" means after the download date.
- Record before sale is flagged, not quarantined.
- Fixes happen in the source system; record that and when an issue was
  resolved, not who.
- Resolution outcomes fixed / replaced / removed, plus rule changed.
- Only the engineer changes checks, in code.
- A key collision flags and alerts; it never fails the run, because the key is
  a guess. Only structural problems fail a run.

## Claude's calls (check these)

- Record key on unchanged source values; `possible_duplicate` flag.
- Rate-jump rule: more than double and at least 10 more failures.
- Check version = hash of the `where` text only.
- Steward list as a view over per-snapshot results plus resolutions stored
  at evaluation time (so it survives raw pruning).
- Only the latest snapshot is evaluated; no backfill.
- Unparseable dates count as invalid (none today).
- `download_date` is the UTC date of the snapshot's first fetch.
