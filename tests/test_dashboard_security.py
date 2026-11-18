"""The dashboard only answers its own page: other websites in the same browser are refused."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from clipwatcher import dashboard


@pytest.fixture
def server(home):
    dashboard.Handler.manager = dashboard.Manager()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def request(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest(method, path, skip_host=True)
    for k, v in {"Host": f"localhost:{port}", **(headers or {})}.items():
        conn.putheader(k, v)
    data = json.dumps(body).encode() if body is not None else b""
    if data:
        conn.putheader("Content-Length", str(len(data)))
    conn.endheaders(data or None)
    res = conn.getresponse()
    status = res.status
    res.read()
    conn.close()
    return status


def test_own_page_works(server):
    page = {"Origin": f"http://localhost:{server}", "Content-Type": "application/json"}
    assert request(server, "GET", "/api/state") == 200
    assert request(server, "GET", "/api/state", {"Host": f"127.0.0.1:{server}"}) == 200
    assert request(server, "POST", "/api/storage", page, {"keep_sources_days": 3}) == 200
    assert request(server, "POST", "/api/storage", {"Content-Type": "application/json"}, {}) == 200   # curl: no Origin


def test_dns_rebinding_is_refused(server):
    assert request(server, "GET", "/api/state", {"Host": f"evil.example:{server}"}) == 403
    assert request(server, "GET", "/api/ai", {"Host": "attacker.test"}) == 403


def test_other_websites_cannot_change_anything(server):
    evil = {"Origin": "https://evil.example", "Content-Type": "application/json"}
    assert request(server, "POST", "/api/ai", evil, {"provider": "custom", "base_url": "https://evil.example"}) == 403
    assert request(server, "POST", "/api/streamers", evil, {"channel": "x"}) == 403
    assert request(server, "DELETE", "/api/streamers/x", {"Origin": "https://evil.example"}) == 403
    assert request(server, "POST", "/api/storage", {"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"}, {}) == 403


def test_plain_form_posts_are_refused(server):
    # an HTML form can't send JSON, so a non-JSON body is refused even without an Origin header
    assert request(server, "POST", "/api/storage", {"Content-Type": "application/x-www-form-urlencoded"},
                   {"keep_clips_days": 1}) == 415
