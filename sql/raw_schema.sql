-- Raw layer: every distinct fetch of the source, kept as a complete, immutable
-- snapshot. Source columns are VARCHAR exactly as delivered (e.g. 'South Park  '
-- keeps its trailing spaces); typing and cleaning happen downstream.
--
-- The landed CSV files are the permanent archive. These tables are working
-- storage: old snapshots can be pruned here and rebuilt from their CSVs
-- (python -m wprdc.load_raw --prune-keep N / --rebuild).

CREATE SCHEMA IF NOT EXISTS raw;

-- One row per distinct landed file. snapshot_id is the file's sha256, so the
-- same bytes fetched twice are never loaded twice.
CREATE TABLE IF NOT EXISTS raw.property_sales_snapshot (
    snapshot_id         VARCHAR PRIMARY KEY,
    run_id              VARCHAR NOT NULL,     -- ops.fetch_run that first fetched it
    resource_id         VARCHAR NOT NULL,
    source_path         VARCHAR NOT NULL,
    ckan_last_modified  TIMESTAMP,            -- naive UTC, from CKAN
    row_count           BIGINT NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL,
    pruned_at           TIMESTAMPTZ           -- rows removed from this DB; CSV still on disk
);

CREATE TABLE IF NOT EXISTS raw.property_sales (
    snapshot_id              VARCHAR NOT NULL,
    source_row_number        BIGINT NOT NULL,  -- 1-based data row within the file
    row_hash                 VARCHAR NOT NULL, -- md5 of every source column except _id
    "_id"                    VARCHAR,
    PARID                    VARCHAR,
    FULL_ADDRESS             VARCHAR,
    PROPERTYHOUSENUM         VARCHAR,
    PROPERTYFRACTION         VARCHAR,
    PROPERTYADDRESSDIR       VARCHAR,
    PROPERTYADDRESSSTREET    VARCHAR,
    PROPERTYADDRESSSUF       VARCHAR,
    PROPERTYADDRESSUNITDESC  VARCHAR,
    PROPERTYUNITNO           VARCHAR,
    PROPERTYCITY             VARCHAR,
    PROPERTYSTATE            VARCHAR,
    PROPERTYZIP              VARCHAR,
    SCHOOLCODE               VARCHAR,
    SCHOOLDESC               VARCHAR,
    MUNICODE                 VARCHAR,
    MUNIDESC                 VARCHAR,
    RECORDDATE               VARCHAR,
    SALEDATE                 VARCHAR,
    PRICE                    VARCHAR,
    DEEDBOOK                 VARCHAR,
    DEEDPAGE                 VARCHAR,
    SALECODE                 VARCHAR,
    SALEDESC                 VARCHAR,
    INSTRTYP                 VARCHAR,
    INSTRTYPDESC             VARCHAR
);

-- Snapshots in fetch order. Recency comes from the fetch log, not load time:
-- if the source goes A -> B -> A, the third fetch is "already loaded" yet A
-- is the current state again.
CREATE OR REPLACE VIEW raw.v_snapshots AS
WITH fetches AS (
    SELECT raw_sha256        AS snapshot_id,
           min(finished_at)  AS first_fetched_at,
           max(finished_at)  AS last_fetched_at,
           count(*)          AS times_fetched
    FROM ops.fetch_resource
    WHERE strategy = 'dump' AND status = 'fetched'
    GROUP BY 1
)
SELECT
    row_number() OVER (ORDER BY f.first_fetched_at) AS snapshot_seq,
    s.snapshot_id,
    s.ckan_last_modified,
    s.row_count,
    f.first_fetched_at,
    f.last_fetched_at,
    f.times_fetched,
    s.pruned_at,
    s.source_path
FROM raw.property_sales_snapshot s
JOIN fetches f USING (snapshot_id);

-- The current state of the source: the snapshot from the most recent fetch.
CREATE OR REPLACE VIEW raw.property_sales_latest AS
SELECT *
FROM raw.property_sales
WHERE snapshot_id = (
    SELECT snapshot_id FROM raw.v_snapshots ORDER BY last_fetched_at DESC LIMIT 1
);

-- Row-level differences between two snapshots, by content hash. A changed
-- record appears once as 'removed' (old version) and once as 'added'.
--   SELECT * FROM raw.snapshot_diff('<old snapshot_id>', '<new snapshot_id>');
CREATE OR REPLACE MACRO raw.snapshot_diff(old_snapshot, new_snapshot) AS TABLE
SELECT 'added' AS change, n.*
FROM (SELECT * FROM raw.property_sales WHERE snapshot_id = new_snapshot) n
ANTI JOIN (SELECT row_hash FROM raw.property_sales WHERE snapshot_id = old_snapshot) o
    USING (row_hash)
UNION ALL
SELECT 'removed' AS change, o.*
FROM (SELECT * FROM raw.property_sales WHERE snapshot_id = old_snapshot) o
ANTI JOIN (SELECT row_hash FROM raw.property_sales WHERE snapshot_id = new_snapshot) n
    USING (row_hash);

-- How much each snapshot differs from the one before it (NULL if either is pruned).
CREATE OR REPLACE VIEW raw.v_snapshot_changes AS
WITH seq AS (
    SELECT snapshot_seq, snapshot_id, row_count, pruned_at,
           lag(snapshot_id) OVER (ORDER BY snapshot_seq) AS prev_snapshot_id,
           lag(row_count)   OVER (ORDER BY snapshot_seq) AS prev_row_count,
           lag(pruned_at)   OVER (ORDER BY snapshot_seq) AS prev_pruned_at,
           first_fetched_at
    FROM raw.v_snapshots
),
common AS (
    SELECT seq.snapshot_id, count(*) AS n_common
    FROM seq
    JOIN raw.property_sales cur ON cur.snapshot_id = seq.snapshot_id
    JOIN raw.property_sales prv ON prv.snapshot_id = seq.prev_snapshot_id
                               AND prv.row_hash = cur.row_hash
    GROUP BY 1
)
SELECT
    seq.snapshot_seq,
    seq.snapshot_id,
    seq.first_fetched_at,
    seq.prev_snapshot_id,
    seq.row_count,
    seq.row_count - seq.prev_row_count AS net_change,
    CASE WHEN seq.pruned_at IS NULL AND seq.prev_pruned_at IS NULL
         THEN seq.row_count - coalesce(c.n_common, 0) END AS rows_added,
    CASE WHEN seq.pruned_at IS NULL AND seq.prev_pruned_at IS NULL
         THEN seq.prev_row_count - coalesce(c.n_common, 0) END AS rows_removed
FROM seq
LEFT JOIN common c USING (snapshot_id)
WHERE seq.prev_snapshot_id IS NOT NULL;
