-- Cleaned layer. Its tables are rebuilt in full from the latest raw snapshot
-- by python -m wprdc.transform (see wprdc/transform.py), so only the schema is
-- declared here:
--   clean.property_sales_typed  every row of the snapshot, typed and trimmed
--   clean.property_sales        typed rows minus quarantined ones, with dq_flags

CREATE SCHEMA IF NOT EXISTS clean;
