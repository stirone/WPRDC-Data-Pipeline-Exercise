# Analytics layer — design

Date: 2026-10-30. Status: draft for approval.

## Intent

The exercise asks for "one output table that could be used to answer a
question you chose (state the question)". The question:

> **Are family transfers of property in Allegheny County rising as the
> population ages?**

Stephen's hypothesis: as owners age, more property passes to family members
or trusts, so "love and affection" transfers (the assessor's sale code 3,
median price $1) should rise over the years. Estate transfers to family
(an executor or similar deeding property to an heir for nothing, after a
death) are the other route the same trend would take, so they are counted
alongside, as their own class. Estate deeds that sell property at a price are
a separate class: they follow a death but are not property passing to family.

This is a data engineering exercise, not an analytics one. The analysis stays
deliberately small: one output table of yearly counts. The engineering is in
the layer underneath: a star schema built from the clean layer, a reviewed,
hand-maintained classification of sale codes, and rebuilds that always give
the same answer for the same data.

The "one output table" is the result of a query, not another physical table:
a view, `analytics.family_transfers_by_year`, defined in SQL in the repo and
computed from the star schema.

## Goals

- A star schema in a new `analytics` schema: fact table `analytics.sales` and
  dimensions for parcel, municipality, school district, sale type and
  instrument type.
- Every sale classified into exactly one `sale_class`, by rules kept in code
  and reviewed through git.
- The output view `analytics.family_transfers_by_year`, which the pipeline
  prints at the end of a run and the README shows.
- Re-runnable: the analytics layer is rebuilt in full from
  `clean.property_sales` each run; the same clean data gives the same tables.
- New sale codes, and parcels whose location changes, are caught by data
  quality checks rather than silently misclassified.

## Non-goals

- **Charts.** The output is a table.
- **Other datasets.** Only the property sales dataset is used. Per-parcel
  rates (per 1,000 parcels) would need the WPRDC assessments dataset, so the
  only denominator is all recorded transfers. An estimate of parcel counts
  from the sales data was considered and rejected: only ~304,000 parcels
  appear in 2012–2025 (roughly half the county), the count is still growing
  ~14,000 a year, and it misses exactly the long-held homes the question is
  about.
- **A by-municipality output.** The dimensions make it a short query, but it
  is not part of the deliverable.
- **2026 and any other incomplete year** in the output (see the view, below).
- **Incremental loads.** The layer is small (~506,000 sales); a full rebuild
  takes seconds. Incremental builds are a "100x scale" topic for the README.
- **A date dimension** (DuckDB's date functions cover year and month) and a
  **buyer/seller dimension** (the data has no names).
- **`python -m wprdc.pipeline`.** The single command that runs every step is
  a separate task.
- **Writing the README.** Separate task; this spec only says what the README
  must show from this layer.

## Anti-goals

- A sale silently counted in the wrong class because the county added a code.
- Rebuilds that give different answers for the same clean data (e.g. depending
  on the date the pipeline ran, or on row order).
- An output table that hides its denominator: shares always appear next to
  the counts they come from.

## Where it sits

```
raw ─> staging.property_sales_typed ─> clean.property_sales     (existing)
                                              │
                                              ▼
                          analytics.municipality   analytics.school_district
                                     │                  │
                                     └── analytics.parcel ──┐
                                                            │
             analytics.sale_type ── analytics.sales (fact) ─┘
                                            │
                               analytics.instrument_type

analytics.family_transfers_by_year   (view: the output table)
```

`python -m wprdc.analytics` builds the layer as one logged run
(`ops.fetch_run`, command `analytics`), in one transaction: on failure the
run is recorded as failed and the previous analytics tables stay. It ends by
printing the output view. The planned `python -m wprdc.pipeline` calls ingest,
load_raw, transform and analytics in turn.

`sql/analytics_schema.sql` creates the schema and the view (the view can be
created up front because DuckDB binds it at query time; if that turns out
not to hold, create it at the end of the build). Tables are rebuilt by the
module, as for `clean`.

## Classification

### Sale classes

Every sale gets exactly one `sale_class`:

| sale_class | Meaning |
|---|---|
| `estate_family` | An estate deed passing property for nothing: coded love and affection, or price $10 or less |
| `estate_sale` | Any other estate deed (an estate selling at a price), or sale code 37 ESTATE SALE |
| `family` | Love and affection transfer (code 3), not on an estate deed |
| `market` | The assessor's valid, arm's-length sale codes |
| `distressed` | Foreclosure, sheriff, treasurer, bank, court-ordered, short sale |
| `other` | Every other known code |
| `unclassified` | A sale code not in the mapping (should never happen; a check catches it) |

**Rules, in order (first match wins):**

1. Estate deed **and** (sale code 3 **or** price ≤ $10) → `estate_family`.
   A null price counts as not nominal.
2. Estate deed, or sale code 37 → `estate_sale`.
3. Otherwise the class mapped from the sale code (code 3 → `family`).

Estate wins over family (decided by Stephen), and estate is split into the
two classes above (decided by Stephen, after the builder check showed that
most estate-deed sales are market-priced: median around $120,000–$140,000).
This settles the 2,983 sales coded love and affection on an estate deed
(2,810 of them fiduciary deeds), such as an executor deeding a parent's house
to a child: they are `estate_family`, not `family`.

Code 37 only ever makes a sale `estate_sale` (Claude's call): the assessor
applies it unevenly (0 uses in 2012, 451 in 2015, 33 in 2022), so it must
not drive the family-related counts.

### Instrument types: estate deeds

`is_estate_deed` is true for: FD fiduciary deed, EX executors deed, AD admin
deed, TD trustee deed, SV survivorship, AF affidavit of survivorship, TO
transfer on death deed, WB will book. All other instrument types (30 in the
current data) are false. Unknown instrument types are false (they can only
affect estate vs. code-based class). Survivorship deeds are usually spouse to
spouse, but they follow a death, so they stay estate deeds (Claude's call).

### Sale codes → class

The code is the key: 47 codes share 27 descriptions, many of them "OTHER".
Starting mapping (Claude's call, except 3 and 37):

| Class | Codes |
|---|---|
| family | 3 LOVE AND AFFECTION SALE |
| estate_sale | 37 ESTATE SALE |
| market | 0 VALID SALE, PV CLEAN AND GREEN VALID PREFERENTIAL |
| distressed | 1 SHERIFF SALE, 2 CITY TREASURER SALE, 32 COURT ORDERED SALE, 33 SALE OF PREVIOUS FORECLOSURE, 34 SHORT SALE, BK BANK/FINANCIAL INSTITUTION |
| other | every other code in the current data (H multi-parcel, 14 time on market, 36 quit claim, 9, 99, AA not analyzed, UR, 35, GV, 16, DT, Z, 27, N, U, 13, 28, ND, PA, 6, 19, 5, X, 29, VB, V, XX, 0X, S, 4, 8, 50, UX, VH, PX, YY, 21) |

Only `family` and `estate_family` feed the output; the other classes are there so
the fact table is useful beyond this one question, and the split costs nothing.
`is_market` from the earlier sketch is dropped: it would equal
`sale_class = 'market'`.

Both mappings live in one Python module, `wprdc/sale_classes.py`, maintained
by hand and reviewed in git like `checks.py`, with a docstring saying how to
add a code. The module is the single source; the dimension tables are built
from it plus the descriptions in the data.

## Tables

All keys are the source's natural codes (PARID, municipality code, …), not
surrogate keys: they are stable, unique in the data, and keep the tables
readable in the DuckDB UI. (Claude's call.)

**`analytics.sales`** (fact): one row per row of `clean.property_sales`, all
years. Columns: `sale_key`, `PARID`, `DEEDBOOK`, `DEEDPAGE`, `sale_date`,
`sale_year`, `record_date`, `price`, `sale_code`, `instrument_type`,
`sale_class`, `dq_flags`. Municipality and school district are reached
through the parcel.

**`analytics.parcel`**: one row per PARID: address columns (`full_address`
and its parts, city, state, zip), `municipality_code`, `school_code`. If a
parcel's attributes differ between its sales, take them from its latest sale
(latest `sale_date`, then latest `record_date`, then highest `sale_key`), so
the choice is deterministic. Today no parcel's municipality or school code
varies; a check watches that (below).

**`analytics.municipality`**: one row per `municipality_code` (175):
`municipality_code`, `municipality` (as in the data, trimmed, internal runs
of spaces collapsed), `is_ward`, `municipality_rollup`.
- The codes include wards: Pittsburgh (101–132), Clairton (201–202), Duquesne
  (301–303), McKeesport (401–412). Their names look like
  `1st Ward  - PITTSBURGH`. For those, `is_ward` is true and
  `municipality_rollup` is the city name (`PITTSBURGH`); otherwise
  `municipality_rollup` is the municipality name and `is_ward` is false.
- Detect a ward by the name pattern `^\d+(st|nd|rd|th) Ward\s*-\s*(.+)$`,
  not by the word "ward": code 911 is **Forward** (a township). A test covers
  it.

**`analytics.school_district`**: one row per `school_code` (46):
`school_code`, `school_district`. Linked from the parcel, not the
municipality, because 6 municipalities span more than one district.

**`analytics.sale_type`**: one row per sale code in the mapping or the data:
`sale_code`, `sale_desc` (from the data; null for a mapped code not in the
data), `sale_class` (`unclassified` for a code in the data but not the
mapping).

**`analytics.instrument_type`**: one row per instrument type in the data:
`instrument_type`, `instrument_desc`, `is_estate_deed`.

Descriptions come from the latest clean data. Each code has exactly one
description today; if one ever had several, take the most frequent (ties:
alphabetical first).

## The output table

`analytics.family_transfers_by_year`, one row per complete year:

| Column | |
|---|---|
| `sale_year` | |
| `all_transfers` | every sale in `analytics.sales` that year |
| `family_transfers` | `sale_class = 'family'` |
| `family_share` | `family_transfers / all_transfers` |
| `estate_family_transfers` | `sale_class = 'estate_family'` |
| `estate_family_share` | `estate_family_transfers / all_transfers` |
| `not_analyzed_transfers` | sale code AA, SALE NOT ANALYZED |

`not_analyzed_transfers` is there because an unanalyzed sale cannot be coded
love and affection, so family counts are understated in years with many of
them: 9,903 in 2012 (30% of the year), about 2,000 and 1,700 in 2018 and
2019, under 10 a year since 2020. Those years stay in the table; the README
explains the effect (Claude's call, after the builder check).

- **Complete years only:** `sale_year` before the year of the clean data's
  download date (`download_date` in the latest snapshot). With today's data
  that is 2012–2025, leaving out 2026 as Stephen decided. (Claude's call on
  the rule: it keeps 2026 out without hard-coding a year. Caveat for the
  README: in early 2027, 2026 enters the table while some of its late sales
  may still be "not analyzed".)
- **Denominator:** all recorded transfers that year, every class, including
  $1 transfers (decided by Stephen). The README must say a "sale" here means
  any recorded deed transfer, and that a share can rise because the other
  transfers fell (e.g. fewer market sales since 2022), which is why counts
  sit next to shares.
- Shares as decimals rounded to 4 places; ordered by `sale_year`.

Expected today (from profiling; the tests should not depend on these):
family ranges 6,490 (2017) to 8,061 (2021), 7,191 in 2025; estate_family
ranges 178 (2020) to 330 (2025), 224 in 2012.

## Data quality checks added

Two `flag` checks in `wprdc/checks.py`, both with `alert="any"` (their
normal count is zero, so the first failure tells the engineer). They follow
the existing check rules; nothing here fails a run.

| id | Rule | Why |
|---|---|---|
| `sale_code_unmapped` | `sale_code` is null or not in the sale-class mapping | A new county code would otherwise land in `unclassified` unnoticed. The `where` text is generated from the mapping, so changing the mapping gives the check a new version (a `definition_changed` notice, by design). |
| `parcel_location_varies` | the record's PARID has more than one distinct `municipality_code` or `school_code` in the snapshot | The parcel dimension assumes one location per parcel. True across all three snapshots so far. |

Both are 0 today.

## Tests

- Classification: estate deed with code 3 → `estate_family`; estate deed at
  $10 with another code → `estate_family`; estate deed at $150,000 →
  `estate_sale`; estate deed with null price and code 0 → `estate_sale`; code
  37 on a plain deed at $1 → `estate_sale`; code 3 on a plain deed →
  `family`; unmapped code → `unclassified`; each class reachable.
- Ward rollup, including `Forward` (not a ward) and names with double spaces.
- Parcel attributes taken from the latest sale, deterministically.
- The output view on fixture rows: counts, shares, the incomplete year left
  out.
- Re-running the build on the same clean data gives identical tables.
- A failure mid-build leaves the previous analytics tables in place and the
  run recorded as failed.
- The two new checks fire on fixture rows and not otherwise.

## Decided by Stephen

- Question: family transfers over time; hypothesis that they rise as the
  population ages.
- Star schema in a schema named `analytics`; fact table `analytics.sales`
  ("sale" in the source's sense: any recorded deed transfer); classification
  column `sale_class`.
- Dimensions: parcel, municipality (with ward roll-up), school district
  (linked from parcel), sale type, instrument type. No date or buyer
  dimension.
- Estate transfers are their own class, and win over family when a sale is
  both.
- Estate is split into `estate_family` (estate deed for nothing) and
  `estate_sale` (estate deed at a price).
- Denominator: share of all transfers. No second dataset.
- 2026 left out of the analysis.
- The output is a table, not a chart; "one output table" is the result of a
  query (a view), not another physical table.

## Claude's calls (check these)

- Estate deeds = FD, EX, AD, TD, SV, AF, TO, WB (survivorship included).
- "For nothing" = code 3 or price ≤ $10; null price is not nominal.
- Code 37 only ever gives `estate_sale`.
- `not_analyzed_transfers` column; years with many unanalyzed sales kept.
- Sale-code mapping for `market`, `distressed`, `other` as above; `is_market`
  dropped.
- Mapping kept in `wprdc/sale_classes.py`, not a CSV seed.
- Natural keys, no surrogate keys.
- Fact table holds all years; only the view drops incomplete years, by the
  "before the download year" rule.
- Parcel attributes from the latest sale.
- Two new flag checks with `alert="any"`.
- `python -m wprdc.analytics` as a separate step, one transaction, logged
  like the others.
