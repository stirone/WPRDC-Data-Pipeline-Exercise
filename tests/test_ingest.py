import hashlib

import pytest

from wprdc import config
from wprdc.db import connect
from wprdc.http_client import DownloadResult
from wprdc.ingest import ingest

RESOURCE_ID = "res-1"


class StubCkan:
    """Stands in for wprdc.ckan.Ckan; 'downloads' a fixed body."""

    def __init__(self, body: bytes, last_modified="2026-10-26T07:08:43.900618"):
        self.body = body
        self.last_modified = last_modified

    def package_show(self, package_id):
        return {"metadata_modified": "2026-10-26T12:21:16",
                "resources": [{"id": RESOURCE_ID, "name": "stub", "last_modified": self.last_modified}]}

    def datastore_total(self, resource_id):
        return 2

    def dump(self, resource_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.body)
        return DownloadResult(dest, hashlib.sha256(self.body).hexdigest(), len(self.body))


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    c = connect(tmp_path / "test.duckdb")
    yield c
    c.close()


def run(con, ckan, run_id, force=False):
    return ingest(con, ckan, run_id, resource_id=RESOURCE_ID, strategy="dump",
                  force=force, page_size=100)


def test_failed_validation_still_records_the_archived_file(con, tmp_path):
    body = b"_id,PARID\r\n1,a\n2,b,EXTRA\n"
    with pytest.raises(ValueError, match="row 2 has 3 fields"):
        run(con, StubCkan(body), "run-bad")
    status, raw_path, sha, n_bytes, err = con.execute(
        "SELECT status, raw_path, raw_sha256, raw_bytes, error_message FROM ops.fetch_resource"
    ).fetchone()
    assert status == "failed"
    assert (tmp_path / raw_path).read_bytes() == body  # file kept, and the record points at it
    assert sha == hashlib.sha256(body).hexdigest() and n_bytes == len(body)
    assert "row 2 has 3 fields" in err


def test_success_then_skip_when_unchanged(con, tmp_path):
    body = b"_id,PARID\r\n1,a\n2,b\n"
    assert run(con, StubCkan(body), "run-1") == "fetched"
    assert run(con, StubCkan(body), "run-2") == "skipped_unchanged"
    assert run(con, StubCkan(body), "run-3", force=True) == "fetched"
    rows = con.execute(
        "SELECT status, raw_rows, content_changed FROM ops.fetch_resource ORDER BY started_at"
    ).fetchall()
    assert rows == [("fetched", 2, True), ("skipped_unchanged", None, False), ("fetched", 2, False)]
    assert len(list((tmp_path / "data" / "raw").rglob("*.csv"))) == 2  # every download kept
