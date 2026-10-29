-- Data quality results, the data steward's issue list, and alerts for the
-- engineer. Written by python -m wprdc.transform (see wprdc/dq.py); checks are
-- defined in wprdc/checks.py.
--
-- Schema files are applied alphabetically, before ops and raw, so nothing here
-- may reference ops.* or raw.* objects.

CREATE SCHEMA IF NOT EXISTS dq;

-- One row per evaluation point: a snapshot as of one fetch of it. If the source
-- goes A -> B -> A, the second A is a new evaluation (a later as_of).
CREATE SEQUENCE IF NOT EXISTS dq.evaluation_seq;
CREATE TABLE IF NOT EXISTS dq.evaluation (
    eval_id        BIGINT PRIMARY KEY DEFAULT nextval('dq.evaluation_seq'),
    snapshot_id    VARCHAR NOT NULL,
    as_of          TIMESTAMPTZ NOT NULL,   -- the snapshot's last_fetched_at
    download_date  DATE NOT NULL,          -- UTC date of its first fetch
    n_rows         BIGINT NOT NULL,
    run_id         VARCHAR NOT NULL,
    evaluated_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (snapshot_id, as_of)
);

-- Rate history: one row per (evaluation, check, check version).
CREATE TABLE IF NOT EXISTS dq.check_run (
    eval_id        BIGINT NOT NULL,
    check_id       VARCHAR NOT NULL,
    check_version  VARCHAR NOT NULL,
    action         VARCHAR NOT NULL,       -- quarantine | flag, as defined at the time
    description    VARCHAR,
    n_failing      BIGINT NOT NULL,
    n_rows         BIGINT NOT NULL,
    rate           DOUBLE NOT NULL,
    PRIMARY KEY (eval_id, check_id, check_version)
);

-- One row per failing record per (evaluation, check, check version).
CREATE TABLE IF NOT EXISTS dq.check_result (
    eval_id            BIGINT NOT NULL,
    check_id           VARCHAR NOT NULL,
    check_version      VARCHAR NOT NULL,
    action             VARCHAR NOT NULL,
    source_row_number  BIGINT NOT NULL,
    sale_key           VARCHAR NOT NULL,
    PARID              VARCHAR,
    DEEDBOOK           VARCHAR,
    DEEDPAGE           VARCHAR,
    SALEDATE           VARCHAR
);

-- The steward's list: one row per issue occurrence (check x record key). Opened
-- when a record first fails, resolved when a later evaluation shows it gone.
-- Outcomes are decided at evaluation time, while that snapshot's raw rows are
-- certainly loaded, so the list survives raw pruning.
CREATE SEQUENCE IF NOT EXISTS dq.issue_seq;
CREATE TABLE IF NOT EXISTS dq.issue (
    issue_id              BIGINT PRIMARY KEY DEFAULT nextval('dq.issue_seq'),
    check_id              VARCHAR NOT NULL,
    check_version         VARCHAR NOT NULL,   -- version it last failed at
    action                VARCHAR NOT NULL,   -- current action of the check
    sale_key              VARCHAR NOT NULL,
    PARID                 VARCHAR,
    DEEDBOOK              VARCHAR,
    DEEDPAGE              VARCHAR,
    SALEDATE              VARCHAR,
    has_real_deed         BOOLEAN NOT NULL,
    first_seen_eval_id    BIGINT NOT NULL,
    last_seen_eval_id     BIGINT NOT NULL,
    resolved_eval_id      BIGINT,
    outcome               VARCHAR,            -- fixed | replaced | removed | rule_changed
    replaced_by_sale_key  VARCHAR
);

-- Alerts and notices for the engineer.
CREATE SEQUENCE IF NOT EXISTS dq.alert_seq;
CREATE TABLE IF NOT EXISTS dq.alert (
    alert_id       BIGINT PRIMARY KEY DEFAULT nextval('dq.alert_seq'),
    created_at     TIMESTAMPTZ NOT NULL,
    run_id         VARCHAR NOT NULL,
    eval_id        BIGINT NOT NULL,
    snapshot_id    VARCHAR NOT NULL,
    check_id       VARCHAR NOT NULL,
    check_version  VARCHAR NOT NULL,
    kind           VARCHAR NOT NULL,       -- rate_jump | key_collision | definition_changed
    previous_rate  DOUBLE,
    new_rate       DOUBLE,
    previous_n     BIGINT,
    new_n          BIGINT,
    subject        VARCHAR NOT NULL,
    body           VARCHAR NOT NULL,
    notified_at    TIMESTAMPTZ,
    UNIQUE (eval_id, check_id, check_version, kind)
);

CREATE OR REPLACE VIEW dq.v_issues AS
SELECT
    i.issue_id, i.check_id, i.action,
    CASE WHEN i.resolved_eval_id IS NULL THEN 'open' ELSE 'resolved' END AS status,
    i.PARID, i.DEEDBOOK, i.DEEDPAGE, i.SALEDATE,
    f.as_of AS first_seen_at,
    l.as_of AS last_seen_at,
    r.as_of AS resolved_at,
    i.outcome, i.replaced_by_sale_key,
    i.sale_key, i.check_version, i.has_real_deed
FROM dq.issue i
JOIN dq.evaluation f ON f.eval_id = i.first_seen_eval_id
JOIN dq.evaluation l ON l.eval_id = i.last_seen_eval_id
LEFT JOIN dq.evaluation r ON r.eval_id = i.resolved_eval_id;

CREATE OR REPLACE VIEW dq.v_open_issues AS
SELECT * FROM dq.v_issues WHERE status = 'open';
