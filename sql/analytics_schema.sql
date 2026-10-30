-- Analytics layer: a star schema over clean.property_sales, and the output
-- table for the question "Are family transfers of property in Allegheny County
-- rising as the population ages?". Rebuilt in full by
-- python -m wprdc.analytics (see wprdc/analytics.py), so only the schema is
-- declared here:
--   analytics.build_info        the snapshot (and download date) built from
--   analytics.sales             fact: one row per clean sale, with sale_class
--   analytics.parcel            one row per PARID: address, municipality, school
--   analytics.municipality      175 codes, wards rolled up to their city
--   analytics.school_district
--   analytics.sale_type         sale code -> sale_class (wprdc/sale_classes.py)
--   analytics.instrument_type   deed type, with is_estate_deed
--   analytics.family_transfers_by_year   view: the output table
-- The view is created by the build, not here: DuckDB will not create a view
-- over a table that does not exist yet.

CREATE SCHEMA IF NOT EXISTS analytics;
