import http.server
import json
import threading

import pytest

from wprdc.db import connect, run_context
from wprdc.http_client import FetchError, LoggedHttp, classify_download, classify_json

OK = json.dumps({"success": True, "result": {"x": 1}}).encode()
CF_HTML = b"<!DOCTYPE HTML PUBLIC><HTML><BODY><H1>504 Gateway Timeout</H1>CloudFront</BODY></HTML>"


@pytest.mark.parametrize("status, ctype, body, expected", [
    (200, "application/json", OK, "ok"),
    (200, "application/json", b"", "empty_body"),
    (200, "text/html", CF_HTML, "html_body"),
    (200, "application/json", CF_HTML, "html_body"),  # mislabeled content type
    (200, "application/json", b'{"success": tr', "non_json"),
    (409, "application/json", b'{"success": false, "error": {"message": "bad"}}', "ckan_error"),
    (404, "text/html", CF_HTML, "http_4xx"),
    (502, "text/html", CF_HTML, "http_5xx"),
    (429, "text/plain", b"slow down", "rate_limited"),
    (200, "application/json", b"[]", "unexpected_body"),
])
def test_classify_json(status, ctype, body, expected):
    assert classify_json(status, ctype, body)[0] == expected


@pytest.mark.parametrize("status, ctype, head, n, expected", [
    (200, "application/octet-stream", b"_id,PARID\r\n1,a\n", 16, "ok"),
    (200, "application/octet-stream", b"\xef\xbb\xbf_id,PARID\r\n", 14, "ok"),
    (200, "application/octet-stream", b"", 0, "empty_body"),
    (200, "text/html", CF_HTML, len(CF_HTML), "html_body"),
    (200, "application/octet-stream", b"PARID,PRICE\n", 12, "unexpected_body"),
])
def test_classify_download(status, ctype, head, n, expected):
    assert classify_download(status, ctype, head, n, b"_id,") == expected


@pytest.fixture
def scripted_server():
    """Local HTTP server that replies with a queued list of (status, ctype, body)."""
    script: list[tuple[int, str, bytes]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, ctype, body = script.pop(0)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def version_string(self):
            return "fake-nginx"

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", script
    srv.shutdown()


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "test.duckdb")
    yield c
    c.close()


def test_blank_then_html_then_ok_is_retried_and_every_attempt_logged(scripted_server, con):
    base, script = scripted_server
    script += [(200, "application/json", b""),
               (200, "text/html", CF_HTML),
               (200, "application/json", OK)]
    with run_context(con, "test") as run_id:
        http = LoggedHttp(con, run_id, sleep=lambda s: None)
        resp = http.get_json("package_show", "k", f"{base}/api/3/action/package_show", {"id": "x"})
    assert resp.payload["result"] == {"x": 1}

    rows = con.execute("""
        SELECT attempt_no, outcome, will_retry, server, body_snippet LIKE '%CloudFront%'
        FROM ops.fetch_attempt ORDER BY attempt_no""").fetchall()
    assert rows == [
        (1, "empty_body", True, "fake-nginx", False),
        (2, "html_body", True, "fake-nginx", True),
        (3, "ok", False, "fake-nginx", False),
    ]
    assert con.execute("SELECT status FROM ops.fetch_run").fetchone() == ("succeeded",)


def test_gives_up_after_max_attempts(scripted_server, con):
    base, script = scripted_server
    script += [(502, "text/html", CF_HTML)] * 3
    with pytest.raises(FetchError):
        with run_context(con, "test") as run_id:
            LoggedHttp(con, run_id, max_attempts=3, sleep=lambda s: None).get_json(
                "package_show", "k", f"{base}/x")
    assert con.execute("SELECT count(*), bool_and(outcome = 'http_5xx') FROM ops.fetch_attempt").fetchone() == (3, True)
    assert con.execute("SELECT status FROM ops.fetch_run").fetchone() == ("failed",)


def test_ckan_error_is_not_retried(scripted_server, con):
    base, script = scripted_server
    script += [(404, "application/json", b'{"success": false, "error": {"message": "Not found"}}')]
    with run_context(con, "test") as run_id, pytest.raises(FetchError):
        LoggedHttp(con, run_id, sleep=lambda s: None).get_json("package_show", "k", f"{base}/x")
    assert con.execute("SELECT count(*) FROM ops.fetch_attempt").fetchone() == (1,)


def test_failed_download_leaves_no_file(scripted_server, con, tmp_path):
    base, script = scripted_server
    script += [(200, "text/html", CF_HTML)] * 2
    dest = tmp_path / "out.csv"
    with run_context(con, "test") as run_id, pytest.raises(FetchError):
        LoggedHttp(con, run_id, max_attempts=2, sleep=lambda s: None).download(
            "datastore_dump", "k", f"{base}/dump", dest, expected_prefix=b"_id,")
    assert not dest.exists()
    assert list(tmp_path.glob("*.part")) == []
