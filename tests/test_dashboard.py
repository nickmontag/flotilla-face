"""The dashboard is reachable only from this machine and only by its own page."""

import http.client
import json
import time

import pytest

pytest.importorskip("libtorrent")

from flotilla.config import Config, State, load_config, render_config
from flotilla.dashboard import Dashboard
from flotilla.engine import Engine


@pytest.fixture
def node(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    path.write_text(render_config(cfg))
    cfg = load_config(path)
    cfg.listen_port = 0
    logs = []
    eng = Engine(cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False)
    dash = Dashboard(eng, 0)
    dash.start()
    yield eng, dash, path, logs
    dash.stop()
    eng.shutdown()


def call(dash, method, path, body=None, host=None, token=None, ctype="application/json", origin=None):
    c = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{dash.port}"}
    if token:
        headers["X-Flotilla-Token"] = token
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        headers["Content-Type"] = ctype
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    out = r.status, dict(r.getheaders()), r.read()
    c.close()
    return out


def test_page_and_state(node):
    eng, dash, _, _ = node
    code, headers, body = call(dash, "GET", "/")
    assert code == 200 and dash.token.encode() in body
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert not any(k.lower() == "access-control-allow-origin" for k in headers)
    code, _, body = call(dash, "GET", "/api/state")
    doc = json.loads(body)
    assert code == 200 and {"settings", "disk", "torrents", "log", "memory"} <= doc.keys()
    assert doc["memory"]["budget"] > 0 and doc["memory"]["paused"] == []


def test_state_shows_list_entries_with_their_origin(node):
    from flotilla.feed import parse_feed

    eng, dash, _, _ = node
    doc = {"schema": 1, "version": 1, "issued": "2026-01-01T00:00:00+00:00", "expires": "2099-01-01T00:00:00+00:00",
           "models": [{"repo": "org/tuned", "scores": {"capability": 50},
                       "origin": {"base": "org/base", "method": "fine-tune", "data": "undisclosed"}},
                      {"repo": "org/base", "scores": {"capability": 40}, "origin": {"method": "original"}}]}
    eng.feed = parse_feed(json.dumps(doc).encode())
    code, _, body = call(dash, "GET", "/api/state")
    assert code == 200, body
    rows = {p["repo"]: p for p in json.loads(body)["priorities"]}
    assert rows["org/tuned"]["origin"]["tag"] == "fine-tuned" and rows["org/tuned"]["origin"]["warn"]
    assert "official release" in rows["org/base"]["origin"]["text"]


def test_history_endpoint_filters_and_validates(node):
    eng, dash, _, _ = node
    now = time.time()
    eng.history.record(now, {"a" * 40: (4000, 100)}, 60, {"a" * 40: "org/model"})
    code, _, body = call(dash, "GET", "/api/history?range=24h&tz=-300")
    doc = json.loads(body)
    assert code == 200 and doc["up"] == 4000 and doc["models"][0]["repo"] == "org/model"
    doc = json.loads(call(dash, "GET", "/api/history?range=30d&repo=org/model")[2])
    assert doc["up"] == 4000 and doc["bucket"] == 6 * 3600
    assert json.loads(call(dash, "GET", "/api/history?range=7d&repo=org/other")[2])["up"] == 0
    for bad in ("range=5y", "repo=../etc", "tz=99999", "repo=a%20b/c"):
        assert call(dash, "GET", "/api/history?" + bad)[0] == 400, bad
    assert call(dash, "GET", "/api/history", host=f"attacker.com:{dash.port}")[0] == 403


def test_dns_rebinding_and_foreign_origins_refused(node):
    _, dash, _, _ = node
    assert call(dash, "GET", "/", host="evil.example:7421")[0] == 403
    assert call(dash, "GET", "/api/state", host=f"attacker.com:{dash.port}")[0] == 403
    assert call(dash, "POST", "/api/pause", {}, token=dash.token, origin="https://evil.example")[0] == 403


def test_posts_need_token_and_json(node):
    eng, dash, _, _ = node
    assert call(dash, "POST", "/api/pause", {})[0] == 403
    assert call(dash, "POST", "/api/pause", {}, token="wrong")[0] == 403
    assert call(dash, "POST", "/api/pause", b"x=1", token=dash.token, ctype="application/x-www-form-urlencoded")[0] == 415
    assert call(dash, "POST", "/api/pause", {}, token=dash.token, origin=f"http://127.0.0.1:{dash.port}")[0] == 200
    eng.process_commands()
    assert eng.user_paused and eng.ses.is_paused()
    assert call(dash, "POST", "/api/resume", {}, token=dash.token)[0] == 200
    eng.process_commands()
    assert not eng.user_paused and not eng.ses.is_paused()


def test_settings_validated_applied_and_saved(node):
    eng, dash, path, _ = node
    for bad in [{"budget_gb": -1}, {"upload_share": 2}, {"evil": 1}, {"budget_gb": "50"}, {}]:
        assert call(dash, "POST", "/api/settings", bad, token=dash.token)[0] == 400, bad
    code, _, _ = call(dash, "POST", "/api/settings", {"budget_gb": 42, "upload_mbps": 8, "upload_share": 0.5},
                      token=dash.token)
    assert code == 200
    eng.process_commands()
    assert eng.cfg.budget_gb == 42 and eng.cfg.upload_mbps == 8
    assert eng.ses.get_settings()["upload_rate_limit"] == int(8 * 125_000 * 0.5)
    saved = load_config(path)
    assert saved.budget_gb == 42 and saved.upload_share == 0.5


def test_hugging_face_switch_starts_off_and_is_saved(node):
    eng, dash, path, logs = node
    assert eng.cfg.hf_fallback is False                     # off until the user turns it on
    code, _, body = call(dash, "GET", "/api/state", token=dash.token)
    assert code == 200
    for bad in [{"hf_fallback": "yes"}, {"hf_fallback": 1}, {"hf_gb_per_day": -1}, {"hf_gb_per_day": True}]:
        assert call(dash, "POST", "/api/settings", bad, token=dash.token)[0] == 400, bad
    assert call(dash, "POST", "/api/settings", {"hf_fallback": True, "hf_gb_per_day": 5},
                token=dash.token)[0] == 200
    eng.process_commands()
    assert eng.cfg.hf_fallback is True and eng.cfg.hf_gb_per_day == 5
    assert any("hf_fallback = on" in l for l in logs)
    saved = load_config(path)
    assert saved.hf_fallback is True and saved.hf_gb_per_day == 5
    assert eng.snapshot()["settings"]["hf_fallback"] is True


def test_skip_and_restore_a_model(node):
    from flotilla.feed import parse_feed

    eng, dash, _, logs = node
    doc = {"schema": 1, "version": 1, "issued": "2026-01-01T00:00:00+00:00", "expires": "2099-01-01T00:00:00+00:00",
           "models": [{"repo": "org/keep", "scores": {"capability": 50}}, {"repo": "org/skip", "scores": {"capability": 60}}]}
    eng.feed = parse_feed(json.dumps(doc).encode())
    for bad in ({"repo": "../x"}, {"repo": 3}, {}):
        assert call(dash, "POST", "/api/skip", bad, token=dash.token)[0] == 400, bad
    assert call(dash, "POST", "/api/unskip", {"repo": "org/skip"}, token=dash.token)[0] == 400   # not skipped
    assert call(dash, "POST", "/api/skip", {"repo": "org/skip"}, token=dash.token)[0] == 200
    eng.process_commands()
    state = json.loads(call(dash, "GET", "/api/state")[2])
    assert state["dropped"] == ["org/skip"] and [p["repo"] for p in state["priorities"]] == ["org/keep"]
    assert any("skipped org/skip" in line for line in logs)
    assert call(dash, "POST", "/api/unskip", {"repo": "org/skip"}, token=dash.token)[0] == 200
    eng.process_commands()
    state = json.loads(call(dash, "GET", "/api/state")[2])
    assert state["dropped"] == [] and {p["repo"] for p in state["priorities"]} == {"org/keep", "org/skip"}


def test_drop_needs_a_held_model(node):
    _, dash, _, _ = node
    assert call(dash, "POST", "/api/drop", {"infohash": "a" * 40}, token=dash.token)[0] == 400
    assert call(dash, "POST", "/api/nuke", {}, token=dash.token)[0] == 400


def test_add_and_remove_picks_through_the_dashboard(node, tmp_path):
    eng, dash, _, _ = node
    eng.cfg.local_feed = tmp_path / "my.json"
    assert call(dash, "POST", "/api/add", {"ref": "https://evil.example/a/b"}, token=dash.token)[0] == 400
    assert call(dash, "POST", "/api/add", {"ref": "https://huggingface.co/a/b", "priority": 500},
                token=dash.token)[0] == 400
    assert call(dash, "POST", "/api/add", {"ref": "https://huggingface.co/a/b"})[0] == 403   # no token
    assert call(dash, "POST", "/api/add", {"ref": "https://huggingface.co/a/b"}, token=dash.token)[0] == 200
    picks = json.loads(call(dash, "GET", "/api/state")[2])["picks"]     # visible at once, before any replan
    assert [p["repo"] for p in picks] == ["a/b"] and picks[0]["status"] == "will be checked at the next replan"
    eng.next_refresh = time.time() + 3600
    eng.process_commands()
    assert eng.next_refresh <= time.time() + 3                          # and a replan is on its way
    assert call(dash, "POST", "/api/remove", {"ref": "a/b"}, token=dash.token)[0] == 200
    assert json.loads(call(dash, "GET", "/api/state")[2])["picks"] == []


def test_order_and_allotments_through_the_dashboard(node, tmp_path):
    eng, dash, _, _ = node
    eng.cfg.local_feed = tmp_path / "my.json"
    eng.cfg.budget_gb = 50
    ok = lambda action, body: call(dash, "POST", f"/api/{action}", body, token=dash.token)[0]
    assert ok("order", {"order": ["b/b", "a/a"]}) == 200
    assert ok("order", {"order": ["../x"]}) == 400
    assert ok("allot", {"repo": "a/a", "gb": 30}) == 200
    code, _, body = call(dash, "POST", "/api/allot", {"repo": "b/b", "gb": 25}, token=dash.token)
    assert code == 400 and b"more than your 50 GB" in body and b"20 GB is left" in body
    assert ok("allot", {"repo": "b/b", "gb": 20}) == 200
    assert ok("allot", {"repo": "a/a", "gb": None}) == 200          # clear
    doc = json.loads((tmp_path / "my.json").read_text())
    assert doc["order"] == ["b/b", "a/a"] and doc["allot_gb"] == {"b/b": 20.0}
    state = json.loads(call(dash, "GET", "/api/state")[2])
    assert state["allot_total_gb"] == 20.0 and isinstance(state["priorities"], list)


def test_config_with_an_old_default_license_list_gets_the_new_defaults(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config()
    cfg.licenses = ["mit", "apache-2.0", "wtfpl", "openmdw-1.1"]          # what older versions wrote
    path.write_text(render_config(cfg))
    assert "glm-5.3" in load_config(path).licenses
    cfg.licenses = ["mit", "apache-2.0"]                                   # one the user chose: kept
    path.write_text(render_config(cfg))
    assert load_config(path).licenses == ["mit", "apache-2.0"]


def test_reorient_schedule_and_churn_size_through_the_dashboard(node):
    eng, dash, path, logs = node
    doc = json.loads(call(dash, "GET", "/api/state")[2])
    assert doc["reorient"]["every"] == "weekly" and doc["settings"]["churn_gb"] == 20
    assert doc["reorient"]["preview"] is None and doc["reorient"]["next"] is None      # no plan made yet
    for bad in [{"reorient": "hourly"}, {"reorient": 7}, {"churn_gb": -1}, {"churn_gb": "10"}]:
        assert call(dash, "POST", "/api/settings", bad, token=dash.token)[0] == 400, bad
    assert call(dash, "POST", "/api/settings", {"reorient": "monthly", "churn_gb": 50}, token=dash.token)[0] == 200
    eng.process_commands()
    assert eng.cfg.reorient == "monthly" and eng.cfg.churn_gb == 50
    assert any("reorient = monthly" in l and "churn_gb = 50" in l for l in logs)
    saved = load_config(path)
    assert saved.reorient == "monthly" and saved.churn_gb == 50
    assert call(dash, "POST", "/api/reorient", {})[0] == 403                           # needs the page's token
    assert call(dash, "POST", "/api/reorient", {}, token=dash.token)[0] == 200
    eng.process_commands()
    assert eng.snapshot()["reorient"]["asked"] and eng.next_refresh == 0


def test_config_refuses_an_unknown_reorient_schedule(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(render_config(Config()).replace('every = "weekly"', 'every = "hourly"'))
    with pytest.raises(ValueError, match="reorient.every"):
        load_config(path)
    path.write_text(render_config(Config()).replace('every = "weekly"', 'every = "never"'))
    assert load_config(path).reorient == "never"


def test_the_dashboard_script_parses(tmp_path):
    """A syntax error anywhere in the page's script (0.2.0-0.2.3 declared `up` twice in render)
    leaves the dashboard empty, and nothing else here runs the script."""
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node.js to parse the script")
    html = (Path(__file__).parent.parent / "flotilla" / "static" / "dashboard.html").read_text()
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert scripts
    for i, js in enumerate(scripts):
        f = tmp_path / f"s{i}.js"
        f.write_text(js)
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
