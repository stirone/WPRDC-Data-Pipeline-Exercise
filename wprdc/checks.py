"""Data quality checks for property sales, run by python -m wprdc.transform.

Each check is a SQL condition over clean.property_sales_typed that selects the
failing records. Records failing a "quarantine" check are held out of
clean.property_sales; records failing a "flag" check stay in it, with the check
id in dq_flags. Either way they appear in the data steward's list,
dq.v_open_issues, until a later snapshot shows the problem gone.

Changing checks (engineer only, in code, reviewed through git):
  - Add: append a Check.
  - Change: edit it. Editing `where` gives the check a new version: open issues
    that no longer fail close as 'rule_changed', and the rate baseline restarts
    (a 'definition_changed' notice instead of a rate alert). Editing `action`
    or `description` keeps the version, so issues stay open.
  - Retire: set retired=True. Never delete a check; its history stays readable.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

# A rate jump alerts the engineer when the failure rate more than doubles AND
# at least this many more records fail. Starting values, to tune with history.
ALERT_RATE_FACTOR = 2.0
ALERT_MIN_INCREASE = 10


@dataclass(frozen=True)
class Check:
    id: str
    description: str
    action: Literal["quarantine", "flag"]
    where: str
    # "rate": alert on a rate jump. "any": alert on the first failure, for
    # checks whose normal count is zero.
    alert: Literal["rate", "any"] = "rate"
    retired: bool = False

    @property
    def version(self) -> str:
        """Hash of the condition only, ignoring whitespace."""
        return hashlib.md5(" ".join(self.where.split()).encode()).hexdigest()[:12]


CHECKS: list[Check] = [
    Check(
        id="sale_date_invalid",
        description="SALEDATE unparseable or before 2012 (the source starts 2012-01-01)",
        action="quarantine",
        where="sale_date IS NULL OR sale_date < DATE '2012-01-01'",
    ),
    Check(
        id="record_date_invalid",
        description="RECORDDATE present but unparseable or before 2012 (e.g. 0212-08-01)",
        action="quarantine",
        where="record_date_text IS NOT NULL AND (record_date IS NULL OR record_date < DATE '2012-01-01')",
    ),
    Check(
        id="record_date_future",
        description="RECORDDATE after the date the snapshot was downloaded",
        action="quarantine",
        where="record_date > download_date",
    ),
    # Quarantined with misgivings: most are mobile home sales, certificates of
    # transfer and treasurer deeds, which may never be recorded, so "missing"
    # may mean "not applicable". To discuss with the data steward; likely to
    # become a flag.
    Check(
        id="record_date_missing",
        description="RECORDDATE missing or blank",
        action="quarantine",
        where="record_date_text IS NULL",
    ),
    Check(
        id="record_before_sale",
        description="RECORDDATE earlier than SALEDATE (both valid)",
        action="flag",
        where=("record_date >= DATE '2012-01-01' AND sale_date >= DATE '2012-01-01' "
               "AND record_date < sale_date"),
    ),
    Check(
        id="possible_duplicate",
        description="No real deed reference, and another such record has the same PARID and SALEDATE",
        action="flag",
        where="""NOT has_real_deed AND (PARID, SALEDATE) IN (
                     SELECT (PARID, SALEDATE) FROM clean.property_sales_typed
                     WHERE NOT has_real_deed GROUP BY ALL HAVING count(*) > 1)""",
    ),
    Check(
        id="key_not_unique",
        description="Record key (PARID, DEEDBOOK, DEEDPAGE, SALEDATE) shared with another record",
        action="flag",
        where="""sale_key IN (
                     SELECT sale_key FROM clean.property_sales_typed
                     GROUP BY 1 HAVING count(*) > 1)""",
        alert="any",
    ),
]
