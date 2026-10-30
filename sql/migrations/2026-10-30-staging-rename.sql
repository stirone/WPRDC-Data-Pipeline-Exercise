-- One-time migration, 2026-10-30: clean.property_sales_typed moved to
-- staging.property_sales_typed, so it is not mistaken for the silver table.
--
-- Two checks named the table in their condition, so the rename changed their
-- version hashes although neither rule changed. They now refer to it as
-- `typed` (a CTE in wprdc/dq.py). This maps their stored history to the new
-- versions, so open issues stay open and the rate baseline carries on, instead
-- of the rename being recorded as a rule change.
--
--   possible_duplicate  b9522cb72371 -> 99d04437dcc8
--   key_not_unique      5beed7f3e5ae -> d30fde1f0463
--
-- Safe to run more than once. A database built after this change never needs
-- it. Run with the pipeline stopped:
--   python -c "from wprdc.db import connect; c = connect(); c.execute(open('sql/migrations/2026-10-30-staging-rename.sql').read())"

BEGIN TRANSACTION;

UPDATE dq.check_run    SET check_version = '99d04437dcc8' WHERE check_id = 'possible_duplicate' AND check_version = 'b9522cb72371';
UPDATE dq.check_result SET check_version = '99d04437dcc8' WHERE check_id = 'possible_duplicate' AND check_version = 'b9522cb72371';
UPDATE dq.issue        SET check_version = '99d04437dcc8' WHERE check_id = 'possible_duplicate' AND check_version = 'b9522cb72371';
UPDATE dq.alert        SET check_version = '99d04437dcc8' WHERE check_id = 'possible_duplicate' AND check_version = 'b9522cb72371';

UPDATE dq.check_run    SET check_version = 'd30fde1f0463' WHERE check_id = 'key_not_unique' AND check_version = '5beed7f3e5ae';
UPDATE dq.check_result SET check_version = 'd30fde1f0463' WHERE check_id = 'key_not_unique' AND check_version = '5beed7f3e5ae';
UPDATE dq.issue        SET check_version = 'd30fde1f0463' WHERE check_id = 'key_not_unique' AND check_version = '5beed7f3e5ae';
UPDATE dq.alert        SET check_version = 'd30fde1f0463' WHERE check_id = 'key_not_unique' AND check_version = '5beed7f3e5ae';

DROP TABLE IF EXISTS clean.property_sales_typed;

COMMIT;
