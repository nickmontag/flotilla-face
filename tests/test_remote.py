"""A dashboard outside the node's process (a node inside a VPN container), and its access key."""

import http.client
import json
import os
import stat
import threading
import time

import pytest

from flotilla import remote
from flotilla.dashboard import NETWORK, Dashboard
from flotilla.remote import RemoteEngine, dashboard_key, take_requests


class FakeEngine:
    def __init__(self, tmp_path):
        from flotilla.history import History

        self.history = History(tmp_path / "h.db")
        self.calls = []

    def snapshot(self):
        return {"ok": 1}

    def submit(self, action, body):
        self.calls.append((action, body))
        return "queued"


def req(dash, method, path, body=None, host=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=5)
    h = {"Host": host or f"10.0.0.5:{dash.port}", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.status, dict(r.getheaders()), r.read()
    c.close()
    return out


@pytest.fixture
def netdash(tmp_path):
    eng = FakeEngine(tmp_path)
    dash = Dashboard(eng, 0, host="127.0.0.1", key="k" * 43)
    dash.start()
    yield eng, dash
    dash.stop()


def test_network_dashboard_needs_its_key(netdash):
    eng, dash = netdash
    assert req(dash, "GET", "/")[0] == 401
    assert req(dash, "GET", "/api/state")[0] == 401
    assert req(dash, "GET", "/?key=wrong")[0] == 401
    code, headers, _ = req(dash, "GET", "/?key=" + "k" * 43)
    assert code == 303 and headers["Location"] == "/"
    cookie = next(v for k, v in headers.items() if k == "Set-Cookie")   # http.client keeps the last; see below
    name = f"flotilla_key_{dash.port}"
    jar = {"Cookie": f"{name}={dash.cookie_value()}"}
    assert "k" * 43 not in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    code, _, page = req(dash, "GET", "/", headers=jar)
    assert code == 200 and dash.token.encode() in page and NETWORK.encode() in page
    assert req(dash, "GET", "/api/state", headers={"X-Flotilla-Key": "k" * 43})[0] == 200
    assert req(dash, "GET", "/api/state", host=f"media-box.lan:{dash.port}", headers=jar)[0] == 200   # any name


def test_network_dashboard_posts_need_key_token_and_same_origin(netdash):
    eng, dash = netdash
    good = {"Cookie": "flotilla_key=" + "k" * 43, "X-Flotilla-Token": dash.token,
            "Origin": f"http://10.0.0.5:{dash.port}"}
    assert req(dash, "POST", "/api/pause", {}, headers=good)[0] == 200
    assert eng.calls == [("pause", {})]
    assert req(dash, "POST", "/api/pause", {}, headers={**good, "Origin": "http://evil.example"})[0] == 403
    assert req(dash, "POST", "/api/pause", {}, headers={k: v for k, v in good.items() if k != "Cookie"})[0] == 401
    assert req(dash, "POST", "/api/pause", {}, headers={**good, "X-Flotilla-Token": "x"})[0] == 403
    assert len(eng.calls) == 1


def test_local_dashboard_is_unchanged(tmp_path):
    dash = Dashboard(FakeEngine(tmp_path), 0)
    dash.start()
    try:
        assert req(dash, "GET", "/api/state", host=f"127.0.0.1:{dash.port}")[0] == 200
        assert req(dash, "GET", "/api/state")[0] == 403          # other names are refused, as before
    finally:
        dash.stop()


def test_key_is_made_once_and_kept_private(tmp_path):
    k = dashboard_key(tmp_path)
    assert len(k) >= 32 and dashboard_key(tmp_path) == k
    assert stat.S_IMODE(os.stat(tmp_path / "dashboard.key").st_mode) == 0o600


def test_bad_requests_in_the_inbox_are_dropped(tmp_path):
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "../evil.json".replace("../", "")).write_text("{}")
    (tmp_path / "inbox" / ("a" * 16 + ".json")).write_text("not json")
    calls = []
    assert take_requests(tmp_path, lambda a, b: calls.append(a) or "ok") == 2
    assert not list((tmp_path / "inbox").iterdir()) and not calls
    answer = json.loads((tmp_path / "outbox" / ("a" * 16 + ".json")).read_text())
    assert answer["ok"] is False
    assert not (tmp_path / "outbox" / "evil.json").exists()


def test_dashboard_drives_an_engine_in_another_process(tmp_path, monkeypatch):
    pytest.importorskip("libtorrent")
    from flotilla.config import Config, State
    from flotilla.engine import Engine
    from flotilla.feed import parse_feed

    monkeypatch.setattr(remote, "WRITE_EVERY", 0.2)
    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    eng = Engine(cfg, State(tmp_path / "state"), log=lambda *_: None, listen_host="127.0.0.1", dht=False)
    doc = {"schema": 1, "version": 1, "issued": "2026-01-01T00:00:00+00:00", "expires": "2099-01-01T00:00:00+00:00",
           "models": [{"repo": "org/a", "scores": {"capability": 50}}]}
    eng.feed = parse_feed(json.dumps(doc).encode())
    far = RemoteEngine(tmp_path / "state")
    stop = threading.Event()

    def engine_loop():                      # what `flotilla run` does each second, sped up
        while not stop.is_set():
            eng.serve_remote(time.time())
            time.sleep(0.05)

    t = threading.Thread(target=engine_loop, daemon=True)
    t.start()
    try:
        snap = far.snapshot()
        assert [p["repo"] for p in snap["priorities"]] == ["org/a"]
        assert far.submit("skip", {"repo": "org/a"}) == "queued"
        assert eng.state.data["dropped"] == ["org/a"]
        with pytest.raises(ValueError, match="which model"):
            far.submit("skip", {"repo": "../x"})
        with pytest.raises(ValueError, match="unknown action"):
            far.submit("Rm -rf", {})
        eng.history.record(time.time(), {"b" * 40: (100, 0)}, 60, {"b" * 40: "org/a"})
        assert far.history.query("1h")["up"] == 100
    finally:
        stop.set()
        t.join(2)
    old = time.time() - 120                  # the engine stopped: its last snapshot ages
    os.utime(tmp_path / "state" / remote.SNAPSHOT, (old, old))
    with pytest.raises(RuntimeError, match="isn't running"):
        far.snapshot()
    eng.shutdown()
