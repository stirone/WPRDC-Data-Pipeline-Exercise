import csv
import io

import duckdb
import pytest

from tests.dq_fixtures import sale
from tests.test_ingest import RESOURCE_ID, StubCkan
from wprdc import config, ingest, pipeline
from wprdc.load_raw import SOURCE_COLUMNS


@pytest.fixture
def calls(monkeypatch):
    """Replace each step's main with a recorder."""
    seen = []
    for name, step in pipeline.STEPS:
        monkeypatch.setattr(step, "main", lambda argv, name=name: seen.append((name, argv)))
    return seen


def test_steps_run_in_order(calls):
    pipeline.main([])
    assert calls == [("ingest", []), ("load_raw", []), ("transform", []), ("analytics", [])]


def test_force_reaches_ingest_only(calls):
    pipeline.main(["--force"])
    assert calls[0] == ("ingest", ["--force"])
    assert [argv for _, argv in calls[1:]] == [[], [], []]


def test_failing_step_stops_the_rest(calls, monkeypatch):
    def boom(argv):
        calls.append(("transform", argv))
        raise RuntimeError("transform broke")
    monkeypatch.setattr(pipeline.transform, "main", boom)
    with pytest.raises(RuntimeError, match="transform broke"):
        pipeline.main([])
    assert [name for name, _ in calls] == ["ingest", "load_raw", "transform"]


def _csv(rows: list[dict]) -> bytes:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=SOURCE_COLUMNS, lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue().encode()


def test_end_to_end_with_stubbed_ckan(tmp_path, monkeypatch, capsys):
    body = _csv([sale(SALEDATE="2025-03-01", RECORDDATE="2025-03-10", SALECODE="3"),
                 sale(SALEDATE="2025-04-01", RECORDDATE="2025-04-10", DEEDPAGE="2")])
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "e2e.duckdb")
    monkeypatch.setattr(config, "MAIN_RESOURCE_ID", RESOURCE_ID)
    monkeypatch.setattr(ingest, "Ckan", lambda http: StubCkan(body))

    pipeline.main([])
    pipeline.main([])                       # re-run: nothing new, same result
    assert "2025" in capsys.readouterr().out

    c = duckdb.connect(str(tmp_path / "e2e.duckdb"))
    assert c.execute("SELECT count(*) FROM raw.property_sales").fetchone() == (2,)
    assert c.execute("SELECT sale_year, all_transfers, family_transfers "
                     "FROM analytics.family_transfers_by_year").fetchall() == [(2025, 2, 1)]
    assert c.execute("SELECT command, status, count(*) FROM ops.fetch_run "
                     "GROUP BY ALL ORDER BY 1").fetchall() == [
        ("analytics", "succeeded", 2), ("ingest", "succeeded", 2), ("transform", "succeeded", 2)]
    c.close()
