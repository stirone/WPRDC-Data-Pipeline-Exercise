-- Cleaned layer, for analysis. Its tables are rebuilt in full by
-- python -m wprdc.transform (see wprdc/transform.py), so only the schema is
-- declared here:
--   clean.property_sales  staging.property_sales_typed minus quarantined rows,
--                         with dq_flags

CREATE SCHEMA IF NOT EXISTS clean;
