-- Raw layer: every distinct fetch of the source, kept as an immutable snapshot.
-- Source columns are VARCHAR exactly as delivered (e.g. 'South Park  ' keeps
-- its trailing spaces); typing and cleaning happen downstream.

CREATE SCHEMA IF NOT EXISTS raw;

-- One row per distinct landed file. snapshot_id is the file's sha256, so the
-- same bytes fetched twice are never loaded twice.
CREATE TABLE IF NOT EXISTS raw.property_sales_snapshot (
    snapshot_id         VARCHAR PRIMARY KEY,
    run_id              VARCHAR NOT NULL,     -- ops.fetch_run that fetched it
    resource_id         VARCHAR NOT NULL,
    source_path         VARCHAR NOT NULL,
    ckan_last_modified  TIMESTAMP,            -- naive UTC, from CKAN
    row_count           BIGINT NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS raw.property_sales (
    snapshot_id              VARCHAR NOT NULL,
    source_row_number        BIGINT NOT NULL,  -- 1-based data row within the file
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
