# Data Quality Checks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `python -m wprdc.transform` builds a typed table and a clean sales table from the latest raw snapshot, runs the data quality checks, maintains the steward's issue list, and records rate-jump alerts.

**Architecture:** Checks are declared in `wprdc/checks.py` as SQL conditions over `clean.property_sales_typed`. `wprdc/dq.py` evaluates them once per evaluation point, writes results and rate history, maintains `dq.issue` (open/resolved with outcome), and writes alerts. `wprdc/transform.py` builds the typed table, calls the evaluation, builds `clean.property_sales`, all in one transaction inside a logged `ops.fetch_run`, then notifies.

**Tech Stack:** Python 3.13, DuckDB 1.5.6, pytest 9.0.2. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-28-data-quality-checks-design.md`

**One deliberate refinement of the spec:** the spec describes `dq.v_issues` as a view over `dq.check_result` plus a `dq.issue_resolution` table. This plan keeps one materialized `dq.issue` table instead (one row per issue occurrence, updated at evaluation time), with `dq.v_issues` / `dq.v_open_issues` as views over it. Same behavior, same survival of raw pruning, much simpler SQL (reopening is just a new row).

## Global Constraints

- `duckdb==1.5.6`; no new packages in `requirements.txt`.
- `wprdc.db.apply_schema` runs `sql/*_schema.sql` in **alphabetical** order (`clean`, `dq`, `ops`, `raw`), so `clean_schema.sql` and `dq_schema.sql` must not reference `ops.*` or `raw.*` objects.
- Connections run with `SET TimeZone = 'UTC'`; timestamps are `TIMESTAMPTZ`.
- Record key: `(PARID, DEEDBOOK, DEEDPAGE, SALEDATE)` on source values unchanged. `sale_key = md5(concat_ws(chr(31), coalesce(PARID, chr(0)), coalesce(DEEDBOOK, chr(0)), coalesce(DEEDPAGE, chr(0)), coalesce(SALEDATE, chr(0))))`.
- Real deed: neither DEEDBOOK nor DEEDPAGE is null, blank, or all zeros (`regexp_full_match(trim(coalesce(x, '')), '0*')` means *not* real).
- `download_date` = UTC date of `raw.v_snapshots.first_fetched_at` for the snapshot.
- Alert constants: `ALERT_RATE_FACTOR = 2.0`, `ALERT_MIN_INCREASE = 10`.
- Surprising data never fails a run. Only structural failure (no snapshot loaded, typed table cannot be built) raises; the transaction rolls back and the previous clean table stays.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Run tests with `python -m pytest`.

## Review Focus

1. **A → B → A.** The source returns to an earlier snapshot. `as_of` (that snapshot's `last_fetched_at`) differs, so it is a new evaluation and issues reflect A again. Test: `test_return_to_earlier_snapshot_reevaluates` (Task 4).
2. **Re-running with no new snapshot.** No new rows in any `dq` table, no alerts, and the clean table is unchanged. Test: `test_transform_rerun_is_noop` (Task 6).
3. **Colliding keys where only one row fails a quarantine check.** Only that row is held out of the clean table (quarantine is by `source_row_number`, not by key). Test: `test_quarantine_is_by_row_not_key` (Task 6).
4. **Blank RECORDDATE.** `''` or whitespace counts as missing, not as "present but unparseable". Test: `test_blank_record_date_is_missing_not_invalid` (Task 3).
5. **PRICE text.** `'1658.0'` and NULL parse without error to `1658` and NULL. Test: `test_typed_parses_price_and_dates` (Task 2).

---

### Task 1: Check definitions

**Files:**
- Create: `wprdc/checks.py`
- Test: `tests/test_checks.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Check:
      id: str
      description: str
      action: Literal["quarantine", "flag"]
      where: str                                  # SQL condition over clean.property_sales_typed
      alert: Literal["rate", "any"] = "rate"      # "any": alert on first failure (baseline 0)
      retired: bool = False
      @property
      def version(self) -> str: ...               # first 12 hex of md5(" ".join(where.split()))
  CHECKS: list[Check]
  ALERT_RATE_FACTOR: float = 2.0
  ALERT_MIN_INCREASE: int = 10
  ```

The seven checks, ids and actions exactly as below. Conditions use typed-table columns from Task 2:

| id | action | alert | where (meaning) |
|---|---|---|---|
| `sale_date_invalid` | quarantine | rate | `sale_date IS NULL OR sale_date < DATE '2012-01-01'` |
| `record_date_invalid` | quarantine | rate | `record_date_text IS NOT NULL AND (record_date IS NULL OR record_date < DATE '2012-01-01')` |
| `record_date_future` | quarantine | rate | `record_date > download_date` |
| `record_date_missing` | quarantine | rate | `record_date_text IS NULL` |
| `record_before_sale` | flag | rate | `record_date >= DATE '2012-01-01' AND sale_date >= DATE '2012-01-01' AND record_date < sale_date` |
| `possible_duplicate` | flag | rate | not `has_real_deed`, and another no-deed row has the same `PARID` and `SALEDATE` (subquery over `clean.property_sales_typed`) |
| `key_not_unique` | flag | any | `sale_key` appears more than once in `clean.property_sales_typed` |

`record_date_missing` gets a comment: quarantined with misgivings, to discuss with the data steward, likely to become a flag. The module docstring explains how to add, change (`action` change keeps the version) and retire (`retired=True`, never delete) a check.

- [ ] **Step 1: Write failing tests**

```python
def test_check_ids_unique():
    ids = [c.id for c in CHECKS]
    assert len(ids) == len(set(ids)) == 7

def test_version_ignores_whitespace_action_and_description():
    a = Check("x", "d1", "flag", "record_date  IS NULL")
    b = Check("x", "d2", "quarantine", "record_date IS\n NULL")
    assert a.version == b.version and len(a.version) == 12

def test_version_changes_with_logic():
    assert Check("x", "d", "flag", "a IS NULL").version != Check("x", "d", "flag", "b IS NULL").version

def test_initial_actions():
    by_id = {c.id: c for c in CHECKS}
    assert {i for i, c in by_id.items() if c.action == "quarantine"} == {
        "sale_date_invalid", "record_date_invalid", "record_date_future", "record_date_missing"}
    assert by_id["key_not_unique"].alert == "any"
```

- [ ] **Step 2: Run** `python -m pytest tests/test_checks.py -v`. Expected: FAIL, `ModuleNotFoundError: wprdc.checks`.
- [ ] **Step 3: Implement `wprdc/checks.py`** as specified.
- [ ] **Step 4: Run** `python -m pytest tests/test_checks.py -v`. Expected: 4 passed.
- [ ] **Step 5: Commit** `git add wprdc/checks.py tests/test_checks.py` then `git commit -m "Add data quality check definitions"`.

---

### Task 2: Schemas, test fixtures, typed table

**Files:**
- Create: `sql/clean_schema.sql` (just `CREATE SCHEMA IF NOT EXISTS clean;` with a header comment)
- Create: `sql/dq_schema.sql`
- Create: `wprdc/transform.py` (typed-table part)
- Create: `tests/dq_fixtures.py`
- Test: `tests/test_transform.py`

**Interfaces:**
- Consumes: `wprdc.load_raw.ROW_HASH_SQL`, `SOURCE_COLUMNS`; `raw.v_snapshots`, `raw.property_sales_latest`.
- Produces:
  ```python
  # wprdc/transform.py
  TYPED = "clean.property_sales_typed"
  CLEAN = "clean.property_sales"
  class TransformError(RuntimeError): ...
  class EvalPoint(NamedTuple):
      snapshot_id: str
      as_of: datetime            # raw.v_snapshots.last_fetched_at of the latest snapshot
      download_date: date        # UTC date of first_fetched_at
      n_rows: int
  def build_typed(con) -> EvalPoint   # CREATE OR REPLACE TABLE clean.property_sales_typed; raises TransformError if no snapshot

  # tests/dq_fixtures.py
  T0: datetime                 # datetime(2026, 10, 28, 21, 52, tzinfo=timezone.utc)
  def sale(**overrides) -> dict      # a valid source row (all SOURCE_COLUMNS as text); overrides by column name
  def make_snapshot(con, sha: str, rows: list[dict], minutes: int = 0) -> str
      # inserts ops.fetch_resource (strategy 'dump', status 'fetched', finished_at = T0 + minutes),
      # raw.property_sales_snapshot and raw.property_sales (row_hash via ROW_HASH_SQL,
      # source_row_number 1..n); returns sha
  ```
  Default `sale()` row: PARID `1010G00075000000`, SALEDATE `2022-06-21`, RECORDDATE `2022-07-06`, PRICE `1.0`, DEEDBOOK `18965`, DEEDPAGE `534`, other columns as in `tests/test_load_raw.py::ROW`. Passes every check.
- Typed table columns: `snapshot_id, source_row_number, row_hash, sale_key, PARID, DEEDBOOK, DEEDPAGE, SALEDATE` (source text, unchanged), `record_date_text` (`nullif(trim(RECORDDATE), '')`), `sale_date DATE`, `record_date DATE` (`TRY_CAST`), `price BIGINT` (`TRY_CAST(TRY_CAST(PRICE AS DECIMAL(18,2)) AS BIGINT)`), `has_real_deed BOOLEAN`, `download_date DATE`, then the remaining text columns trimmed, empty → NULL, snake_case names (builder's choice which to keep; `_id` dropped).
- `dq_schema.sql` (all `IF NOT EXISTS`, with sequences for ids):
  - `dq.evaluation(eval_id PK, snapshot_id, as_of TIMESTAMPTZ, download_date DATE, n_rows BIGINT, run_id, evaluated_at TIMESTAMPTZ, UNIQUE(snapshot_id, as_of))`
  - `dq.check_run(eval_id, check_id, check_version, action, description, n_failing BIGINT, n_rows BIGINT, rate DOUBLE, PRIMARY KEY(eval_id, check_id, check_version))`
  - `dq.check_result(eval_id, check_id, check_version, action, source_row_number, sale_key, PARID, DEEDBOOK, DEEDPAGE, SALEDATE)`
  - `dq.issue(issue_id PK, check_id, check_version, action, sale_key, PARID, DEEDBOOK, DEEDPAGE, SALEDATE, has_real_deed, first_seen_eval_id, last_seen_eval_id, resolved_eval_id, outcome, replaced_by_sale_key)`; `outcome` in `fixed | replaced | removed | rule_changed`
  - `dq.alert(alert_id PK, created_at, run_id, eval_id, snapshot_id, check_id, check_version, kind, previous_rate, new_rate, previous_n, new_n, subject, body, notified_at, UNIQUE(eval_id, check_id, check_version, kind))`; `kind` in `rate_jump | key_collision | definition_changed`
  - `dq.v_issues`: `dq.issue` joined to `dq.evaluation` three times for `first_seen_at`, `last_seen_at`, `resolved_at` (each that evaluation's `as_of`), plus `status` (`open` when `resolved_eval_id IS NULL`, else `resolved`)
  - `dq.v_open_issues`: `dq.v_issues WHERE status = 'open'`

- [ ] **Step 1: Write failing tests**

```python
def test_typed_parses_price_and_dates(con):
    make_snapshot(con, "s1", [sale(PRICE="1658.0"), sale(PRICE=None, DEEDPAGE="535")])
    point = build_typed(con)
    assert point == EvalPoint("s1", ..., date(2026, 10, 28), 2)   # compare fields individually
    assert con.execute(f"SELECT price FROM {TYPED} ORDER BY source_row_number").fetchall() == [(1658,), (None,)]
    assert con.execute(f"SELECT sale_date, record_date FROM {TYPED} LIMIT 1").fetchone() == (date(2022, 6, 21), date(2022, 7, 6))

def test_key_columns_unchanged_and_real_deed(con):
    make_snapshot(con, "s1", [sale(DEEDBOOK="00", DEEDPAGE="000"), sale(DEEDBOOK=" 18965 ")])
    build_typed(con)
    rows = con.execute(f"SELECT DEEDBOOK, has_real_deed FROM {TYPED} ORDER BY source_row_number").fetchall()
    assert rows == [("00", False), (" 18965 ", True)]

def test_build_typed_without_snapshot_raises(con):
    with pytest.raises(TransformError):
        build_typed(con)
```

- [ ] **Step 2: Run** `python -m pytest tests/test_transform.py -v`. Expected: FAIL on import.
- [ ] **Step 3: Implement** the schema files, `tests/dq_fixtures.py`, and `build_typed` in `wprdc/transform.py`.
- [ ] **Step 4: Run** `python -m pytest -v`. Expected: all pass, including the existing suites (the new schema files must not break `connect()`).
- [ ] **Step 5: Commit** `git commit -m "Add dq/clean schemas and typed sales table"`.

---

### Task 3: Evaluate checks (results and rate history)

**Files:**
- Create: `wprdc/dq.py`
- Test: `tests/test_dq.py`

**Interfaces:**
- Consumes: `Check`, `CHECKS` (Task 1); `EvalPoint`, `TYPED`, `build_typed`, fixtures (Task 2).
- Produces:
  ```python
  def evaluate(con, run_id: str, point: EvalPoint, checks: list[Check]) -> int
      # returns eval_id; get-or-create dq.evaluation by (snapshot_id, as_of).
      # For each non-retired check without a dq.check_run row for (eval_id, id, version):
      #   insert dq.check_result rows (SELECT ... FROM TYPED WHERE <check.where>) and one dq.check_run row,
      #   then update_issues (Task 4) and detect_alerts (Task 5).
      # Retired checks: retire_issues (Task 4). Caller owns the transaction.
  def current_results(con, eval_id: int, checks: list[Check]) -> str
      # SQL text selecting (check_id, action, source_row_number) for eval_id at each
      # non-retired check's current version, with action taken from `checks` (not from storage)
  ```

- [ ] **Step 1: Write failing tests** (each builds a snapshot with `make_snapshot`, runs `build_typed`, then `evaluate(con, "r1", point, CHECKS)`, and reads `dq.check_result` by `check_id`):

```python
def failing(con, check_id):  # helper: sorted source_row_numbers failing check_id
    ...

def test_record_date_rules(con):
    rows = [sale(),                                              # 1 passes all
            sale(DEEDPAGE="2", RECORDDATE="0212-08-01", SALEDATE="2012-08-01"),  # 2 invalid (real case)
            sale(DEEDPAGE="3", RECORDDATE="2026-10-29"),         # 3 future vs download 2026-10-28
            sale(DEEDPAGE="4", RECORDDATE=None),                 # 4 missing
            sale(DEEDPAGE="5", RECORDDATE="2022-06-01")]         # 5 before sale (2022-06-21)
    ...
    assert failing(con, "record_date_invalid") == [2]
    assert failing(con, "record_date_future") == [3]
    assert failing(con, "record_date_missing") == [4]
    assert failing(con, "record_before_sale") == [5]
    assert failing(con, "sale_date_invalid") == []

def test_blank_record_date_is_missing_not_invalid(con):
    # RECORDDATE "  " -> record_date_missing only
    ...

def test_sale_date_before_2012_and_unparseable(con):
    # SALEDATE "2011-12-31" and "not a date" -> sale_date_invalid both
    ...

def test_possible_duplicate_and_key_not_unique(con):
    rows = [sale(PARID="8000T00220000000", DEEDBOOK="00", DEEDPAGE="00"),
            sale(PARID="8000T00220000000", DEEDBOOK="000", DEEDPAGE="000"),
            sale(DEEDPAGE="9"), sale(DEEDPAGE="9")]
    ...
    assert failing(con, "possible_duplicate") == [1, 2]
    assert failing(con, "key_not_unique") == [3, 4]

def test_check_run_rates(con):
    # 4 rows, 1 missing record date -> n_failing 1, n_rows 4, rate 0.25
    ...

def test_evaluate_twice_writes_nothing_new(con):
    # second evaluate() returns same eval_id; counts in check_result/check_run unchanged
    ...
```

- [ ] **Step 2: Run** `python -m pytest tests/test_dq.py -v`. Expected: FAIL on import.
- [ ] **Step 3: Implement `evaluate` and `current_results`** in `wprdc/dq.py`, with these no-op stubs, which Tasks 4 and 5 fill in: `update_issues(con, eval_id: int, check: Check) -> None`, `retire_issues(con, eval_id: int, check: Check) -> None`, `detect_alerts(con, run_id: str, eval_id: int, check: Check) -> list[dict]` (returns `[]`).
- [ ] **Step 4: Run** `python -m pytest tests/test_dq.py -v`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Evaluate data quality checks per snapshot"`.

---

### Task 4: Issue lifecycle

**Files:**
- Modify: `wprdc/dq.py`
- Test: `tests/test_dq_issues.py`

**Interfaces:**
- Consumes: `evaluate` (Task 3).
- Produces:
  ```python
  def update_issues(con, eval_id: int, check: Check) -> None
  def retire_issues(con, eval_id: int, check: Check) -> None   # open issues of a retired check -> rule_changed
  ```

Algorithm for `update_issues`, per check (failing = distinct `sale_key` in this evaluation's results for the check at its version; open = `dq.issue` rows for the check with `resolved_eval_id IS NULL`):

- failing ∩ open: set `last_seen_eval_id`, `check_version`, `action`.
- failing − open: insert a new issue (`first_seen_eval_id = last_seen_eval_id = eval_id`; key columns and `has_real_deed` from the typed table).
- open − failing: set `resolved_eval_id = eval_id` and `outcome`:
  1. key present in typed table and the issue's `check_version` ≠ the check's version → `rule_changed`
  2. key present → `fixed`
  3. issue `has_real_deed` and typed rows with the same `PARID, DEEDBOOK, DEEDPAGE` exist → `replaced`, with `replaced_by_sale_key` set only when exactly one distinct `sale_key` matches
  4. otherwise → `removed`

- [ ] **Step 1: Write failing tests** (two or three snapshots via `make_snapshot(..., minutes=i)`, each followed by `build_typed` + `evaluate`; assert on `dq.v_issues`):

```python
def test_issue_persists_across_snapshots(con):
    # missing RECORDDATE in s1 and s2 -> one issue, first_seen_at = s1 as_of, last_seen_at = s2 as_of, open

def test_fixed(con):
    # s1 RECORDDATE None; s2 same key, RECORDDATE "2022-07-06" -> outcome fixed, resolved_at = s2 as_of

def test_replaced_links_successor(con):
    # s1 RECORDDATE "2022-06-01" (before sale) ; s2 same deed, SALEDATE "2022-05-30" -> key changed
    # -> outcome replaced, replaced_by_sale_key = s2 row's sale_key

def test_removed_and_no_deed_never_replaced(con):
    # s1 no-deed row (DEEDBOOK "0") failing record_date_missing; s2 same PARID/deed, different SALEDATE
    # -> outcome removed

def test_rule_changed_on_version_change(con):
    # evaluate s1 with CHECKS; then evaluate the same point with record_date_missing.where altered
    # so the row no longer fails -> outcome rule_changed

def test_action_change_keeps_issue_open(con):
    # evaluate s1 with CHECKS, then s2 (same data) with record_date_missing action="flag"
    # -> still one open issue, action 'flag'

def test_retired_check_closes_as_rule_changed(con): ...

def test_reopen_creates_new_issue(con):
    # fail in s1, pass in s2, fail in s3 -> two issue rows, first resolved fixed, second open

def test_return_to_earlier_snapshot_reevaluates(con):
    # A (missing date), B (fixed), A again (make_snapshot reuses sha "A" with a new fetch row)
    # -> issue resolved at B, new open issue at the second A
```

For the A → B → A case, the fixture adds a second `ops.fetch_resource` row with the same sha and a later `finished_at`, without reinserting raw rows. Add `refetch(con, sha, minutes)` to `tests/dq_fixtures.py`.

- [ ] **Step 2: Run** `python -m pytest tests/test_dq_issues.py -v`. Expected: FAIL (stubs).
- [ ] **Step 3: Implement** `update_issues` and `retire_issues`.
- [ ] **Step 4: Run** `python -m pytest -v`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Track data quality issues and their resolution"`.

---

### Task 5: Alerts and notification

**Files:**
- Modify: `wprdc/dq.py`
- Test: `tests/test_dq_alerts.py`

**Interfaces:**
- Consumes: `dq.check_run` history (Task 3), `ALERT_RATE_FACTOR`, `ALERT_MIN_INCREASE`.
- Produces:
  ```python
  def detect_alerts(con, run_id: str, eval_id: int, check: Check) -> list[dict]
      # inserts into dq.alert (ON CONFLICT DO NOTHING on the unique key) and returns the new alerts
  def notify(alert: dict) -> None
      # prints subject and body; the only function an email version replaces
  def pending_alerts(con) -> list[dict]       # dq.alert rows with notified_at IS NULL
  def mark_notified(con, alert_id: int) -> None
  ```

Rules, comparing with the check's most recent earlier `dq.check_run` (by `dq.evaluation.as_of`, then `eval_id`):

- No earlier run → no alert, except `alert="any"` with `n_failing > 0` → `key_collision`.
- Earlier run at a different version → `definition_changed` notice (no rate alert).
- Same version, `alert="rate"`: `rate > ALERT_RATE_FACTOR * previous_rate` and `n_failing - previous_n >= ALERT_MIN_INCREASE` → `rate_jump`.
- Same version, `alert="any"`: `n_failing > 0` and `previous_n == 0` → `key_collision`.

Subjects, exactly:
- `[WPRDC DQ ALERT] {check_id}: {previous_rate:.2%} -> {new_rate:.2%} (snapshot {snapshot_id[:8]}, {as_of:%Y-%m-%d})`
- `[WPRDC DQ ALERT] key_not_unique: {new_n} records share a key (snapshot {snapshot_id[:8]}, {as_of:%Y-%m-%d})`
- `[WPRDC DQ NOTICE] {check_id} definition changed (snapshot {snapshot_id[:8]}, {as_of:%Y-%m-%d})`

Body: previous and new counts and rates, snapshot id and `as_of`, previous snapshot id, and a SQL query listing this evaluation's failures for the check.

- [ ] **Step 1: Write failing tests**

```python
def test_rate_jump_alerts(con):
    # s1: 20 rows, 1 missing date; s2: 20 rows, 12 missing -> one rate_jump, subject starts "[WPRDC DQ ALERT] record_date_missing: 5.00% -> 60.00%"

def test_small_increase_does_not_alert(con):
    # 1 -> 5 failing (more than double, but +4 < 10) -> no alert

def test_first_evaluation_no_rate_alert(con): ...

def test_key_collision_alerts_on_first_failure(con):
    # s1 clean; s2 two rows with the same key -> key_collision with new_n 2

def test_definition_changed_is_notice_not_alert(con): ...

def test_alerts_not_duplicated_on_rerun(con): ...
```

- [ ] **Step 2: Run** `python -m pytest tests/test_dq_alerts.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `detect_alerts`, `notify`, `pending_alerts`, `mark_notified`.
- [ ] **Step 4: Run** `python -m pytest -v`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Record and print data quality alerts"`.

---

### Task 6: Clean table and the transform command

**Files:**
- Modify: `wprdc/transform.py`
- Test: `tests/test_transform.py`

**Interfaces:**
- Consumes: `build_typed` (Task 2), `evaluate`, `current_results` (Task 3), `pending_alerts`, `notify`, `mark_notified` (Task 5), `wprdc.db.connect`, `run_context`.
- Produces:
  ```python
  def build_clean(con, eval_id: int, checks: list[Check]) -> int    # returns row count
      # CREATE OR REPLACE TABLE clean.property_sales: typed rows whose source_row_number has no
      # current quarantine result, plus dq_flags VARCHAR[] (sorted ids of current flag results, [] if none)
  def transform(con, run_id: str, checks: list[Check] = CHECKS) -> dict
      # one transaction: build_typed, evaluate, build_clean; commit; then notify + mark_notified
      # for pending alerts. Returns {"eval_id", "snapshot_id", "n_rows", "n_clean", "alerts"}.
      # Any exception before commit rolls back (previous clean table remains) and re-raises.
  def main(argv: list[str] | None = None) -> None
      # connect(); with run_context(con, "transform"): transform(); print a per-check summary
  ```

- [ ] **Step 1: Write failing tests**

```python
def test_clean_excludes_quarantined_and_flags(con):
    # rows: ok, missing date (quarantine), before sale (flag) -> clean has 2 rows; flagged row dq_flags == ["record_before_sale"]

def test_quarantine_is_by_row_not_key(con):
    # two rows with the same key, one with RECORDDATE None -> clean keeps the other, flagged ["key_not_unique"]

def test_action_change_releases_records(con):
    # transform with CHECKS, then with record_date_missing action="flag" -> row now in clean with dq_flags ["record_date_missing"]

def test_transform_rerun_is_noop(con):
    # transform twice: counts of dq.evaluation/check_run/check_result/issue/alert unchanged; second result["alerts"] == []

def test_structural_failure_keeps_previous_clean(con, monkeypatch):
    # transform once; then monkeypatch build_clean to raise; transform raises; clean.property_sales row count unchanged
```

- [ ] **Step 2: Run** `python -m pytest tests/test_transform.py -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** `build_clean`, `transform`, `main` (module docstring with usage, matching `load_raw.py`'s style).
- [ ] **Step 4: Run** `python -m pytest -v`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Add transform command: clean table, checks, alerts"`.

---

### Task 7: Run on the real data

**Files:** none changed unless a check misbehaves.

- [ ] **Step 1:** Close any DuckDB UI/CLI session holding `data/wprdc.duckdb`, then run `python -m wprdc.transform`. Expected: run succeeds. Latest snapshot `dd6c1944…`: 506,726 typed rows and 505,473 clean rows. Failing counts: `sale_date_invalid` 0, `record_date_invalid` 4, `record_date_future` 1, `record_date_missing` 1,248, `record_before_sale` 725, `possible_duplicate` 43, `key_not_unique` 0. No alerts (first evaluation).
- [ ] **Step 2:** Run it again. Expected: same summary, no new rows in `dq.*`, no alerts.
- [ ] **Step 3:** `SELECT check_id, count(*) FROM dq.v_open_issues GROUP BY 1` matches step 1's counts (as distinct keys).
- [ ] **Step 4:** If any count differs, stop and report rather than adjusting a check to fit.
