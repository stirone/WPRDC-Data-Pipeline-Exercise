# Analytics Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `analytics` star schema from `clean.property_sales` and the output view `analytics.family_transfers_by_year`, via `python -m wprdc.analytics`.

**Architecture:** A hand-maintained mapping module (`wprdc/sale_classes.py`) produces the SQL that classifies each sale. A new module `wprdc/analytics.py` rebuilds every analytics table with `CREATE OR REPLACE` inside one transaction, then (re)creates the output view, mirroring `wprdc/transform.py`. Two new flag checks in `wprdc/checks.py` guard the mapping and the parcel dimension.

**Tech Stack:** Python 3, DuckDB 1.5.6, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-30-analytics-layer-design.md`

## Global Constraints

- No new dependencies (`requirements.txt` unchanged).
- Only `clean.property_sales` is read; no other dataset.
- Classes are exactly: `estate_family`, `estate_sale`, `family`, `market`, `distressed`, `other`, `unclassified`.
- Estate deeds: `FD, EX, AD, TD, SV, AF, TO, WB`. "For nothing" = sale code `3` or `price <= 10`; null price is not nominal.
- Rule order: estate deed and nominal → `estate_family`; estate deed or code `37` → `estate_sale`; else mapped class; unmapped or null code → `unclassified`.
- Natural keys only (PARID, codes); no surrogate keys.
- Rebuilds must be deterministic: same clean data → identical tables, independent of run date and row order.
- New checks are `action="flag"`, `alert="any"`; nothing in this plan fails a run except the analytics build itself erroring.
- Run logging: `ops.fetch_run` with `command = 'analytics'`.

## Review Focus

1. `python -m wprdc.analytics` before `transform` has ever run (no `clean.property_sales`): expect a clear `AnalyticsError` naming the transform command, the run recorded as failed, no partial tables. → Task 3 test `test_build_without_clean_raises`.
2. An empty `clean.property_sales`: build succeeds, view returns zero rows. → Task 3 test `test_empty_clean_builds`.
3. Null `municipality_code` / `school_code` / `instrument_type` / `sale_code` on a row: the fact row is kept; dimensions contain no null-key rows; the sale is `unclassified` (null code) or not estate (null instrument). → Task 3 test `test_null_codes_kept_in_fact`.
4. A mapped code absent from the data: still a `sale_type` row, `sale_desc` null. → Task 3 test `test_sale_type_includes_mapped_codes_missing_from_data`.
5. Running the build twice: identical table contents. → Task 3 test `test_rebuild_is_identical`.

---

### Task 1: Sale-class mapping and classification SQL

**Files:**
- Create: `wprdc/sale_classes.py`
- Test: `tests/test_sale_classes.py`

**Interfaces:**
- Produces:
  - `SALE_CLASSES: dict[str, str]` — sale code → class, all 47 codes from the spec table (`3`→`family`, `37`→`estate_sale`, `0`,`PV`→`market`, `1`,`2`,`32`,`33`,`34`,`BK`→`distressed`, the 37 listed others → `other`).
  - `ESTATE_DEEDS: frozenset[str]` = `{"FD","EX","AD","TD","SV","AF","TO","WB"}`.
  - `NOMINAL_PRICE = 10`, `NOT_ANALYZED_CODE = "AA"`.
  - `sale_class_sql(sale_code: str = "sale_code", instrument_type: str = "instrument_type", price: str = "price") -> str` — a SQL `CASE` expression over those column expressions.
  - `mapped_codes_sql() -> str` — the mapped codes as a sorted, comma-separated list of SQL string literals (`'0', '1', …`), for the check in Task 2.

Module docstring in the style of `wprdc/checks.py`: what the mapping is, that it is reviewed in git, and how to add a code (add an entry; the `sale_code_unmapped` check's version changes, giving a `definition_changed` notice, by design).

- [ ] **Step 1: Write the failing tests**

```python
import duckdb
import pytest
from wprdc.sale_classes import ESTATE_DEEDS, SALE_CLASSES, mapped_codes_sql, sale_class_sql

CLASSES = {"estate_family", "estate_sale", "family", "market", "distressed", "other"}

def classify(code, instr, price):
    return duckdb.sql(
        f"SELECT {sale_class_sql('c', 'i', 'p')} FROM (SELECT ?::VARCHAR c, ?::VARCHAR i, ?::BIGINT p)",
        params=[code, instr, price]).fetchone()[0]
```
(If `duckdb.sql` does not accept `params` in 1.5.6, use `duckdb.connect().execute(...)`.)

- `test_mapping_covers_all_codes`: `len(SALE_CLASSES) == 47`; `set(SALE_CLASSES.values()) <= CLASSES`; `SALE_CLASSES["3"] == "family"`, `["37"] == "estate_sale"`, `["0"] == ["PV"] == "market"`, `{c for c, k in SALE_CLASSES.items() if k == "distressed"} == {"1","2","32","33","34","BK"}`.
- `test_estate_deeds`: `ESTATE_DEEDS == {"FD","EX","AD","TD","SV","AF","TO","WB"}`.
- `test_classification` — parametrized `(code, instr, price, expected)`:
  - `("3","FD",1,"estate_family")`, `("0","EX",10,"estate_family")`, `("0","FD",150000,"estate_sale")`, `("0","SV",None,"estate_sale")`, `("3","FD",None,"estate_family")`, `("37","DE",1,"estate_sale")`, `("3","DE",1,"family")`, `("3",None,1,"family")`, `("0","DE",200000,"market")`, `("1","SD",5000,"distressed")`, `("H","DE",1,"other")`, `("ZZ","DE",1,"unclassified")`, `(None,"DE",1,"unclassified")`.
- `test_mapped_codes_sql`: `duckdb.sql(f"SELECT count(*) FROM (VALUES {...})")` is awkward; instead assert `mapped_codes_sql().count("'") == 94` and `"'3'" in mapped_codes_sql()`, and that it is sorted (stable version hash): `mapped_codes_sql() == ", ".join(f"'{c}'" for c in sorted(SALE_CLASSES))`.

- [ ] **Step 2: Run to verify failure**
Run: `python -m pytest tests/test_sale_classes.py -q` — Expected: FAIL, `ModuleNotFoundError: wprdc.sale_classes`.

- [ ] **Step 3: Implement `wprdc/sale_classes.py`**
`sale_class_sql` builds the CASE from the rule order in Global Constraints; the mapped-code branch is one `WHEN {sale_code} = '<code>' THEN '<class>'` per entry, sorted by code, then `ELSE 'unclassified'`. Nominal test: `({sale_code} = '3' OR {price} <= 10)` (null price makes the comparison null, i.e. not nominal).

- [ ] **Step 4: Run to verify pass**
Run: `python -m pytest tests/test_sale_classes.py -q` — Expected: all pass.

- [ ] **Step 5: Commit**
`git add wprdc/sale_classes.py tests/test_sale_classes.py && git commit -m "Add sale-class mapping and classification SQL"`

---

### Task 2: Two new data quality checks

**Files:**
- Modify: `wprdc/checks.py` (append to `CHECKS`)
- Modify: `tests/test_checks.py` (count 7 → 9)
- Test: `tests/test_transform.py` (add tests)

**Interfaces:**
- Consumes: `mapped_codes_sql()` from Task 1.
- Produces: check ids `sale_code_unmapped`, `parcel_location_varies` (both `action="flag"`, `alert="any"`).

Definitions:
- `sale_code_unmapped`: description "SALECODE missing or not in wprdc/sale_classes.py"; `where=f"sale_code IS NULL OR sale_code NOT IN ({mapped_codes_sql()})"`.
- `parcel_location_varies`: description "PARID has more than one municipality or school district in the snapshot"; `where="""PARID IN (SELECT PARID FROM typed GROUP BY 1 HAVING count(DISTINCT municipality_code) > 1 OR count(DISTINCT school_code) > 1)"""`.

- [ ] **Step 1: Write the failing tests**
- In `tests/test_checks.py::test_check_ids_unique`: `== 9`. Add to `test_initial_actions`: `by_id["sale_code_unmapped"].alert == by_id["parcel_location_varies"].alert == "any"` and both `action == "flag"`.
- In `tests/test_transform.py`:
  - `test_unmapped_sale_code_flagged`: snapshot `[sale(), sale(DEEDPAGE="2", SALECODE="ZZ")]`; `result = transform(con, "r1")`; `clean_rows(con) == [(1, []), (2, ["sale_code_unmapped"])]`; `len(result["alerts"]) == 1`.
  - `test_parcel_location_varies_flagged`: snapshot `[sale(), sale(DEEDPAGE="2", MUNICODE="946")]`; both rows flagged `["parcel_location_varies"]`; a third parcel with a different PARID and the base location is not flagged.

- [ ] **Step 2: Run to verify failure**
Run: `python -m pytest tests/test_checks.py tests/test_transform.py -q` — Expected: the new/changed tests FAIL.

- [ ] **Step 3: Append the two `Check` entries in `wprdc/checks.py`** (import `mapped_codes_sql`; short comment above each saying why it exists, as for the existing checks).

- [ ] **Step 4: Run the whole suite**
Run: `python -m pytest -q` — Expected: all pass (existing fixtures use mapped code `H` and one location per PARID, so existing alert counts are unchanged).

- [ ] **Step 5: Commit**
`git commit -am "Add sale_code_unmapped and parcel_location_varies checks"` (include the test files).

---

### Task 3: Build the analytics layer

**Files:**
- Create: `sql/analytics_schema.sql` (only `CREATE SCHEMA IF NOT EXISTS analytics;` plus a header comment listing the tables and the view, in the style of `sql/clean_schema.sql`; it must not create the view: DuckDB refuses a view over a table that does not exist yet)
- Create: `wprdc/analytics.py`
- Modify: `sql/ops_schema.sql` comment on `command` (add `analytics`)
- Test: `tests/test_analytics.py`

**Interfaces:**
- Consumes: `sale_class_sql`, `ESTATE_DEEDS`, `SALE_CLASSES`, `NOT_ANALYZED_CODE` (Task 1); `CLEAN` from `wprdc.transform`; `connect`, `run_context` from `wprdc.db`; test fixtures `con`, `make_snapshot`, `sale` from `tests/dq_fixtures.py` and `transform` to produce clean rows.
- Produces:
  - Constants `SALES = "analytics.sales"`, `OUTPUT = "analytics.family_transfers_by_year"`, `TABLES = ("analytics.municipality", "analytics.school_district", "analytics.parcel", "analytics.sale_type", "analytics.instrument_type", SALES)`.
  - `class AnalyticsError(RuntimeError)`.
  - `build_analytics(con: duckdb.DuckDBPyConnection) -> dict` — `{"n_sales": int}`; one transaction (`con.begin()` … `commit()`, `rollback()` and re-raise on any exception, as in `transform.transform`); raises `AnalyticsError("... run python -m wprdc.transform first")` if `clean.property_sales` does not exist (check `information_schema.tables`).
  - One private builder per table (`_build_municipality(con)`, etc.) plus `_create_output_view(con)`; names are the builder's choice.
  - `main(argv: list[str] | None = None) -> None`.

Table contents (exact columns, from the spec):
- `analytics.municipality(municipality_code, municipality, is_ward, municipality_rollup)`: distinct non-null codes. `municipality` = description with `trim` and internal whitespace runs collapsed to one space. Ward if the normalized name matches `^\d+(st|nd|rd|th) Ward\s*-\s*(.+)$`; rollup = capture group 2 (trimmed) for wards, else `municipality`.
- `analytics.school_district(school_code, school_district)`: distinct non-null codes.
- `analytics.parcel(PARID, full_address, house_number, house_fraction, street_direction, street, street_suffix, unit_desc, unit_no, city, state, zip, municipality_code, school_code)`: one row per PARID, attributes from the row ranked first by `sale_date DESC NULLS LAST, record_date DESC NULLS LAST, sale_key DESC`.
- `analytics.sale_type(sale_code, sale_desc, sale_class)`: union of `SALE_CLASSES` keys and non-null codes in the data; `sale_class` from the mapping, `'unclassified'` if absent; `sale_desc` from the data, null if absent.
- `analytics.instrument_type(instrument_type, instrument_desc, is_estate_deed)`: distinct non-null types in the data.
- Descriptions for every dimension: the most frequent description per code, ties broken by alphabetical first (`arg_max` over `(count, -rank)` or a window; builder's choice).
- `analytics.sales(sale_key, PARID, DEEDBOOK, DEEDPAGE, sale_date, sale_year, record_date, price, sale_code, instrument_type, sale_class, dq_flags)`: one row per clean row, `sale_year = year(sale_date)`, `sale_class = sale_class_sql()`, ordered by `sale_key`.
- View `analytics.family_transfers_by_year(sale_year, all_transfers, family_transfers, family_share, estate_family_transfers, estate_family_share, not_analyzed_transfers)` over `analytics.sales`, `WHERE sale_year < (SELECT year(max(download_date)) FROM clean.property_sales)`, shares `round(x / all_transfers, 4)`, `ORDER BY sale_year`. Created with `CREATE OR REPLACE VIEW` at the end of the transaction.

`main`: `connect()`; `with run_context(con, "analytics"): build_analytics(con)`; then print one line `f"{n_sales:,} sales in {SALES}"` and the view as a plain aligned table (header + one line per year; integers with thousands separators, shares as percentages with one decimal). Close the connection in `finally`.

Module docstring as in `transform.py`: the command, what it builds, one transaction, re-running gives the same result.

- [ ] **Step 1: Write the failing tests** in `tests/test_analytics.py`. Helper: `def build(con, rows): make_snapshot(con, "s1", rows); transform(con, "r1"); return build_analytics(con)`. Fixture download date is 2026-10-28, so 2026 sales are the incomplete year.
  - `test_build_without_clean_raises`: on a fresh `con`, `pytest.raises(AnalyticsError, match="transform")`; then `SELECT count(*) FROM information_schema.tables WHERE table_schema = 'analytics'` is 0.
  - `test_empty_clean_builds`: `make_snapshot(con, "s1", [])`, `transform`, `build_analytics(con)["n_sales"] == 0`, `SELECT count(*) FROM analytics.family_transfers_by_year` is 0.
  - `test_sales_classified`: rows `sale(SALECODE="3", SALEDESC="LOVE AND AFFECTION SALE")`, `sale(DEEDPAGE="2", SALECODE="3", INSTRTYP="FD", INSTRTYPDESC="FIDUCIARY DEED")`, `sale(DEEDPAGE="3", SALECODE="0", INSTRTYP="FD", PRICE="150000")` → `sale_class` by `DEEDPAGE`: `family`, `estate_family`, `estate_sale`; `n_sales == 3`.
  - `test_ward_rollup`: rows with `(MUNICODE, MUNIDESC)` `("109", "9th Ward  -  McKEESPORT")`, `("101", "1st Ward  - PITTSBURGH")`, `("911", "Forward")`, `("945", "South Park  ")` (distinct PARIDs) → `SELECT municipality_code, municipality, is_ward, municipality_rollup ... ORDER BY 1` equals `[("101","1st Ward - PITTSBURGH",True,"PITTSBURGH"), ("109","9th Ward - McKEESPORT",True,"McKEESPORT"), ("911","Forward",False,"Forward"), ("945","South Park",False,"South Park")]`.
  - `test_parcel_attributes_from_latest_sale`: same PARID, sale 2020-01-01 with `PROPERTYZIP="15129"` and sale 2022-06-21 with `PROPERTYZIP="15102"` (both valid record dates after their sale dates) → parcel zip `"15102"`, one parcel row.
  - `test_sale_type_includes_mapped_codes_missing_from_data`: one `H` row → `SELECT count(*) FROM analytics.sale_type` is 47; code `'3'` row has `sale_desc` null and class `family`; `'H'` has `"MULTI-PARCEL SALE"`.
  - `test_null_codes_kept_in_fact`: one row with `MUNICODE=""`, `SCHOOLCODE=""`, `INSTRTYP=""`, `SALECODE=""` (blank → null in typed) plus one normal row → `analytics.sales` has 2 rows, the blank one `unclassified`; `analytics.municipality`, `school_district`, `instrument_type` have no null keys.
  - `test_output_view`: 2025 rows: two family (`SALECODE="3"`), one estate_family (`SALECODE="3", INSTRTYP="FD"`), one `AA`, one `H`; one 2026 row (sale 2026-03-01, record 2026-03-10) → view returns exactly `[(2025, 5, 2, 0.4, 1, 0.2, 1)]`. Use `SALEDATE`/`RECORDDATE` in 2025 and distinct `DEEDPAGE`s.
  - `test_rebuild_is_identical`: build, snapshot every table in `TABLES` and the view with `SELECT * ... ORDER BY ALL`, `build_analytics(con)` again, compare equal.
  - `test_failure_keeps_previous_tables`: build once with 1 row; add a second snapshot with 2 rows (`make_snapshot(con, "s2", ..., minutes=1)`), `transform(con, "r2")`; monkeypatch the sales builder (or `_create_output_view`) to raise; `pytest.raises`; `SELECT count(*) FROM analytics.sales` still 1.
  - `test_main_logs_an_analytics_run`: as `test_main_logs_a_transform_run` in `tests/test_transform.py`: create db, snapshot, run `transform_module.main([])`, then `analytics.main([])`; output contains `"family"`; `ops.fetch_run` has `("analytics", "succeeded")`.

- [ ] **Step 2: Run to verify failure**
Run: `python -m pytest tests/test_analytics.py -q` — Expected: FAIL, `ModuleNotFoundError: wprdc.analytics`.

- [ ] **Step 3: Implement `sql/analytics_schema.sql`, `wprdc/analytics.py`, the ops comment**

- [ ] **Step 4: Run the whole suite**
Run: `python -m pytest -q` — Expected: all pass.

- [ ] **Step 5: Commit**
`git add sql/analytics_schema.sql sql/ops_schema.sql wprdc/analytics.py tests/test_analytics.py && git commit -m "Add analytics star schema and family transfers output view"`

---

### Task 4: Run against the real data and verify

**Files:** none changed unless a check fails.

- [ ] **Step 1: Run transform then analytics on the real database**
Run: `python -m wprdc.transform` then `python -m wprdc.analytics` (close any DuckDB UI/CLI holding `data/wprdc.duckdb` first).
Expected: transform prints `sale_code_unmapped 0` and `parcel_location_varies 0`, and no new alerts; analytics prints ~506,000 sales and 14 rows, 2012–2025.

- [ ] **Step 2: Check the output against the spec's profiled values**
Expected: `family_transfers` 7,138 (2012), 6,490 (2017), 8,061 (2021), 7,191 (2025); `estate_family_transfers` 224 (2012), 178 (2020), 330 (2025); `not_analyzed_transfers` 9,903 (2012). Also `SELECT count(*) FROM analytics.municipality` = 175, `school_district` = 46, `instrument_type` = 30, `sale_type` = 47, and `SELECT count(*) FROM analytics.sales WHERE sale_class = 'unclassified'` = 0. If a value differs, find out why before changing anything (the data may have been refreshed since profiling: compare with a direct query on `clean.property_sales`).

- [ ] **Step 3: Commit only if fixes were needed**, with a message naming the fix.
