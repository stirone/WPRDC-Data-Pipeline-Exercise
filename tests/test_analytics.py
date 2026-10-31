from datetime import date

import duckdb
import pytest

from tests.dq_fixtures import con, make_snapshot, sale  # noqa: F401  (con is a fixture)
from wprdc import analytics as analytics_module
from wprdc import config
from wprdc import transform as transform_module
from wprdc.analytics import OUTPUT, SALES, TABLES, AnalyticsError, build_analytics
from wprdc.transform import transform


def build(con, rows):
    make_snapshot(con, "s1", rows)
    transform(con, "r1")
    return build_analytics(con)


def in_2025(**overrides):
    return sale(SALEDATE="2025-03-01", RECORDDATE="2025-03-10", **overrides)


def test_build_without_clean_raises(con):
    with pytest.raises(AnalyticsError, match="transform"):
        build_analytics(con)
    assert con.execute("SELECT count(*) FROM information_schema.tables "
                       "WHERE table_schema = 'analytics'").fetchone() == (0,)


def test_empty_clean_builds(con):
    assert build(con, [])["n_sales"] == 0
    assert con.execute(f"SELECT count(*) FROM {OUTPUT}").fetchone() == (0,)


def test_sales_classified(con):
    result = build(con, [
        sale(SALECODE="3", SALEDESC="LOVE AND AFFECTION SALE"),
        sale(DEEDPAGE="2", SALECODE="3", INSTRTYP="FD", INSTRTYPDESC="FIDUCIARY DEED"),
        sale(DEEDPAGE="3", SALECODE="0", INSTRTYP="FD", PRICE="150000"),
    ])
    assert result["n_sales"] == 3
    assert con.execute(f"SELECT DEEDPAGE, sale_class FROM {SALES} ORDER BY 1").fetchall() == [
        ("2", "estate_family"), ("3", "estate_sale"), ("534", "family")]


def test_ward_rollup(con):
    build(con, [
        sale(PARID="P1", MUNICODE="109", MUNIDESC="9th Ward  -  McKEESPORT"),
        sale(PARID="P2", MUNICODE="101", MUNIDESC="1st Ward  - PITTSBURGH"),
        sale(PARID="P3", MUNICODE="911", MUNIDESC="Forward"),
        sale(PARID="P4"),
    ])
    assert con.execute("SELECT municipality_code, municipality, is_ward, municipality_rollup "
                       "FROM analytics.municipality ORDER BY 1").fetchall() == [
        ("101", "1st Ward - PITTSBURGH", True, "PITTSBURGH"),
        ("109", "9th Ward - McKEESPORT", True, "McKEESPORT"),
        ("911", "Forward", False, "Forward"),
        ("945", "South Park", False, "South Park"),
    ]


def test_parcel_attributes_from_latest_sale(con):
    build(con, [
        sale(PROPERTYZIP="15102"),
        sale(DEEDPAGE="2", SALEDATE="2020-01-01", RECORDDATE="2020-01-10", PROPERTYZIP="15129"),
    ])
    assert con.execute("SELECT PARID, zip FROM analytics.parcel").fetchall() == [("1010G00075000000", "15102")]


def test_sale_type_includes_mapped_codes_missing_from_data(con):
    build(con, [sale()])
    assert con.execute("SELECT count(*) FROM analytics.sale_type").fetchone() == (47,)
    rows = dict((code, (desc, cls)) for code, desc, cls in con.execute(
        "SELECT sale_code, sale_desc, sale_class FROM analytics.sale_type").fetchall())
    assert rows["3"] == (None, "family")
    assert rows["H"] == ("MULTI-PARCEL SALE", "other")


def test_null_codes_kept_in_fact(con):
    build(con, [sale(), sale(DEEDPAGE="2", MUNICODE="", SCHOOLCODE="", INSTRTYP="", SALECODE="")])
    assert con.execute(f"SELECT DEEDPAGE, sale_class FROM {SALES} ORDER BY 1").fetchall() == [
        ("2", "unclassified"), ("534", "other")]
    for table, key in (("municipality", "municipality_code"), ("school_district", "school_code"),
                       ("instrument_type", "instrument_type")):
        assert con.execute(f"SELECT count(*) FROM analytics.{table} WHERE {key} IS NULL").fetchone() == (0,)


def test_output_view(con):
    build(con, [
        in_2025(DEEDPAGE="1", SALECODE="3"),
        in_2025(DEEDPAGE="2", SALECODE="3"),
        in_2025(DEEDPAGE="3", SALECODE="3", INSTRTYP="FD"),
        in_2025(DEEDPAGE="4", SALECODE="AA"),
        in_2025(DEEDPAGE="5"),
        sale(DEEDPAGE="6", SALEDATE="2026-03-01", RECORDDATE="2026-03-10"),   # incomplete year
    ])
    assert con.execute(f"SELECT * FROM {OUTPUT}").fetchall() == [(2025, 5, 2, 0.4, 1, 0.2, 1)]


def snapshot_tables(con):
    return {t: con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in (*TABLES, OUTPUT)}


def test_rebuild_is_identical(con):
    build(con, [sale(), sale(DEEDPAGE="2", SALECODE="3"), sale(PARID="P2", MUNICODE="101",
                                                             MUNIDESC="1st Ward  - PITTSBURGH")])
    first = snapshot_tables(con)
    build_analytics(con)
    assert snapshot_tables(con) == first


def test_failure_keeps_previous_tables(con, monkeypatch):
    build(con, [sale()])
    make_snapshot(con, "s2", [sale(), sale(DEEDPAGE="2")], minutes=1)
    transform(con, "r2")

    def boom(*args, **kwargs):
        raise RuntimeError("cannot build")
    monkeypatch.setattr(analytics_module, "_create_output_view", boom)
    with pytest.raises(RuntimeError):
        build_analytics(con)
    assert con.execute(f"SELECT count(*) FROM {SALES}").fetchone() == (1,)


def test_main_logs_an_analytics_run(tmp_path, monkeypatch, capsys):
    db = tmp_path / "main.duckdb"
    c = transform_module.connect(db)
    make_snapshot(c, "s1", [in_2025(SALECODE="3")])
    c.close()
    monkeypatch.setattr(config, "DB_PATH", db)
    transform_module.main([])
    analytics_module.main([])
    out = capsys.readouterr().out
    assert "family" in out and "2025" in out
    c = duckdb.connect(str(db))
    assert c.execute("SELECT command, status FROM ops.fetch_run ORDER BY started_at").fetchall() == [
        ("transform", "succeeded"), ("analytics", "succeeded")]
    c.close()


def test_view_frozen_at_build_time(con):
    build(con, [in_2025(), sale(DEEDPAGE="2", SALEDATE="2026-03-01", RECORDDATE="2026-03-10")])
    before = con.execute(f"SELECT * FROM {OUTPUT}").fetchall()
    # transform loads a snapshot downloaded a year later; analytics is not rebuilt
    make_snapshot(con, "s2", [in_2025(), sale(DEEDPAGE="2", SALEDATE="2026-03-01", RECORDDATE="2026-03-10")],
                  minutes=365 * 24 * 60)
    transform(con, "r2")
    assert con.execute(f"SELECT * FROM {OUTPUT}").fetchall() == before
    assert con.execute("SELECT snapshot_id, download_date FROM analytics.build_info").fetchall() == [
        ("s1", date(2026, 10, 28))]


def test_output_view_documented(con):
    build(con, [in_2025()])
    schema, view = OUTPUT.split(".")
    comments = dict(con.execute("SELECT column_name, comment FROM duckdb_columns() "
                                "WHERE schema_name = ? AND table_name = ?", [schema, view]).fetchall())
    assert comments == analytics_module.OUTPUT_COLUMN_COMMENTS
    assert con.execute("SELECT comment FROM duckdb_views() WHERE schema_name = ? AND view_name = ?",
                       [schema, view]).fetchone()[0] == analytics_module.OUTPUT_COMMENT
