-- Operational metadata: every run, every HTTP attempt, every resource fetched.
-- Applied idempotently on every connection (see wprdc/db.py).
-- All timestamps we generate are TIMESTAMPTZ in UTC. CKAN's own timestamps
-- (metadata_modified, last_modified) are naive UTC and stored as TIMESTAMP.

CREATE SCHEMA IF NOT EXISTS ops;

-- One row per script execution (ingest, probe, or ad-hoc dev session).
CREATE TABLE IF NOT EXISTS ops.fetch_run (
    run_id            VARCHAR PRIMARY KEY,
    command           VARCHAR NOT NULL,      -- ingest | probe | adhoc | transform | analytics
    started_at        TIMESTAMPTZ NOT NULL,
    finished_at       TIMESTAMPTZ,
    status            VARCHAR NOT NULL,      -- running | succeeded | failed
    error_message     VARCHAR,
    argv              VARCHAR,
    git_commit        VARCHAR,
    git_dirty         BOOLEAN,
    hostname          VARCHAR,
    python_version    VARCHAR,
    requests_version  VARCHAR,
    notes             VARCHAR
);

-- One row per HTTP request actually sent, including each retry.
CREATE SEQUENCE IF NOT EXISTS ops.fetch_attempt_seq;
CREATE TABLE IF NOT EXISTS ops.fetch_attempt (
    attempt_id        BIGINT PRIMARY KEY DEFAULT nextval('ops.fetch_attempt_seq'),
    run_id            VARCHAR NOT NULL,
    request_key       VARCHAR NOT NULL,      -- logical request; identical across its retries
    endpoint          VARCHAR NOT NULL,      -- package_show | datastore_search | datastore_dump | ...
    attempt_no        INTEGER NOT NULL,
    method            VARCHAR NOT NULL,
    url               VARCHAR NOT NULL,      -- fully encoded, including query string
    params            JSON,
    started_at        TIMESTAMPTZ NOT NULL,
    ttfb_ms           DOUBLE,                -- until response headers arrived
    elapsed_ms        DOUBLE,                -- until body fully read (or failure)
    http_status       INTEGER,
    outcome           VARCHAR NOT NULL,      -- see wprdc/http_client.py OUTCOMES
    error_class       VARCHAR,
    error_message     VARCHAR,
    content_type      VARCHAR,
    content_length    BIGINT,                -- header value, when the server sent one
    bytes_received    BIGINT,
    body_sha256       VARCHAR,
    body_snippet      VARCHAR,               -- first 500 chars; identifies which layer failed
    ckan_success      BOOLEAN,               -- CKAN envelope "success" flag, JSON endpoints only
    server            VARCHAR,
    via               VARCHAR,
    x_cache           VARCHAR,               -- CloudFront hit/miss/error
    cf_id             VARCHAR,               -- X-Amz-Cf-Id, quotable to WPRDC if escalating
    retry_after       VARCHAR,
    response_headers  JSON,
    will_retry        BOOLEAN NOT NULL,
    backoff_s         DOUBLE                 -- sleep before the next attempt, if any
);

-- One row per resource per run: what CKAN said about it, and where the bytes landed.
CREATE TABLE IF NOT EXISTS ops.fetch_resource (
    run_id                     VARCHAR NOT NULL,
    resource_id                VARCHAR NOT NULL,
    strategy                   VARCHAR NOT NULL,   -- dump | datastore
    package_id                 VARCHAR NOT NULL,
    resource_name              VARCHAR,
    ckan_last_modified         TIMESTAMP,          -- resource.last_modified (naive UTC)
    package_metadata_modified  TIMESTAMP,          -- package.metadata_modified (naive UTC)
    ckan_reported_total        BIGINT,             -- datastore_search total at fetch time
    status                     VARCHAR NOT NULL,   -- fetched | skipped_unchanged | failed
    raw_path                   VARCHAR,            -- relative to project root
    raw_sha256                 VARCHAR,
    raw_bytes                  BIGINT,
    raw_rows                   BIGINT,             -- rows counted in the landed file(s)
    content_changed            BOOLEAN,            -- sha differs from previous successful fetch
    raw_table                  VARCHAR,            -- set when loaded into the raw schema
    started_at                 TIMESTAMPTZ NOT NULL,
    finished_at                TIMESTAMPTZ,
    error_message              VARCHAR,
    PRIMARY KEY (run_id, resource_id, strategy)
);

-- Failure rate and latency by local hour of day.
CREATE OR REPLACE VIEW ops.v_reliability_by_hour AS
SELECT
    hour(started_at AT TIME ZONE 'America/New_York')                 AS hour_et,
    endpoint,
    count(*)                                                         AS attempts,
    count(*) FILTER (WHERE outcome = 'ok')                           AS ok,
    round(100.0 * count(*) FILTER (WHERE outcome <> 'ok') / count(*), 1) AS failure_pct,
    round(median(elapsed_ms))                                        AS median_ms,
    round(quantile_cont(elapsed_ms, 0.95))                           AS p95_ms
FROM ops.fetch_attempt
GROUP BY ALL
ORDER BY hour_et, endpoint;

-- What kinds of failure, from which layer, with an example body.
CREATE OR REPLACE VIEW ops.v_failure_breakdown AS
SELECT
    endpoint,
    outcome,
    http_status,
    server,
    x_cache,
    count(*)                 AS attempts,
    min(started_at)          AS first_seen,
    max(started_at)          AS last_seen,
    round(median(elapsed_ms)) AS median_ms,
    any_value(body_snippet)  AS sample_snippet
FROM ops.fetch_attempt
WHERE outcome <> 'ok'
GROUP BY ALL
ORDER BY attempts DESC;

-- One row per logical request: how many tries it took and how it ended.
CREATE OR REPLACE VIEW ops.v_request_outcomes AS
SELECT
    run_id,
    request_key,
    endpoint,
    count(*)                          AS attempts,
    arg_max(outcome, attempt_no)      AS final_outcome,
    min(started_at)                   AS first_attempt_at,
    round(sum(elapsed_ms))            AS total_elapsed_ms,
    sum(coalesce(backoff_s, 0))       AS total_backoff_s
FROM ops.fetch_attempt
GROUP BY ALL;
