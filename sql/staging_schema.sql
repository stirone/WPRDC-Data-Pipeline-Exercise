-- Staging layer: intermediate tables the pipeline builds and checks on the way
-- to clean.*, not meant for analysis. Rebuilt in full from the latest raw
-- snapshot by python -m wprdc.transform (see wprdc/transform.py), so only the
-- schema is declared here:
--   staging.property_sales_typed  every row of the snapshot, typed and trimmed,
--                                 including rows the checks will quarantine

CREATE SCHEMA IF NOT EXISTS staging;
