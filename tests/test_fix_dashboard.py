"""Dashboard hardening: slow or piled-up connections, HTTPS proxies, odd input, the cookie, and the
node inside a VPN container not serving its own keyless dashboard."""

import http.client
import json
import socket
import time

import pytest

from flotilla import dashboard
from flotilla.dashboard import Dashboard
from flotilla.remote import RemoteEngine

KEY = "k" * 43


class FakeEngine:
    def __init__(self, tmp_path, fail=None):
        from flotilla.history import History

        self.history = History(tmp_path / "h.db")
        self.calls, self.fail = [], fail

    def snapshot(self):
        return {"ok": 1}

    def submit(self, action, body):
        if self.fail:
            raise self.fail
        self.calls.append((action, body))
        return "queued"


def req(dash, method, path, body=None, headers=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=5)
    h = {"Host": f"10.0.0.5:{dash.port}", **(headers or {})}
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.status, r.getheaders(), r.read()
    c.close()
    return out


@pytest.fixture
def make(tmp_path):
    made = []

    def make(key=KEY, fail=None, **kw):
        eng = FakeEngine(tmp_path, fail)
        dash = Dashboard(eng, 0, key=key, **kw)
        dash.start()
        made.append(dash)
        return eng, dash

    yield make
    for d in made:
        d.stop()


def _good(dash, **extra):
    return {"X-Flotilla-Key": KEY, "X-Flotilla-Token": dash.token, **extra}


# ---------------------------------------------------------------- 1. slow and piled-up connections


def test_a_request_dripped_a_byte_at_a_time_is_cut_off(make, monkeypatch):
    monkeypatch.setattr(dashboard, "REQUEST_TIMEOUT", 1.0)
    _, dash = make()
    s = socket.create_connection(("127.0.0.1", dash.port), timeout=5)
    start = time.monotonic()
    closed = False
    try:
        for ch in b"GET / HTTP/1.1\r\nHost: x\r\nX-Slow: " + b"a" * 100:
            s.sendall(bytes([ch]))          # each byte well inside a per-read timeout
            time.sleep(0.1)
            if time.monotonic() - start > 4:
                break
    except OSError:
        closed = True
    if not closed:
        s.settimeout(3)
        try:
            closed = s.recv(1) == b""
        except OSError:
            closed = True
    s.close()
    assert closed and time.monotonic() - start < 4


def test_connections_past_the_cap_are_closed_and_the_slots_come_back(make, monkeypatch):
    monkeypatch.setattr(dashboard, "MAX_CONNECTIONS", 3)
    monkeypatch.setattr(dashboard._Server.__init__, "__defaults__", (3,))
    monkeypatch.setattr(dashboard, "REQUEST_TIMEOUT", 1.5)
    _, dash = make()
    held = [socket.create_connection(("127.0.0.1", dash.port), timeout=5) for _ in range(3)]
    time.sleep(0.3)
    extra = socket.create_connection(("127.0.0.1", dash.port), timeout=5)
    try:
        extra.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n")
        got = extra.recv(100)
    except OSError:
        got = b""
    extra.close()
    assert got == b""                                 # closed unanswered: every slot is taken
    for s in held:
        s.close()
    time.sleep(0.3)
    assert req(dash, "GET", "/api/state", headers=_good(dash))[0] == 200   # slots freed when threads end


def test_keep_alive_still_works(make):
    _, dash = make()
    c = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=5)
    for _ in range(3):
        c.request("GET", "/api/state", headers={"Host": "x", "X-Flotilla-Key": KEY})
        r = c.getresponse()
        assert r.status == 200 and json.loads(r.read()) == {"ok": 1}
    c.close()


# ---------------------------------------------------------------- 2. HTTPS proxies


def test_posts_through_an_https_proxy_are_accepted(make):
    eng, dash = make()
    host = f"media.tail1234.ts.net:{dash.port}"
    h = _good(dash, Host=host, Origin=f"https://{host}")
    assert req(dash, "POST", "/api/pause", {}, headers=h)[0] == 200
    assert req(dash, "POST", "/api/pause", {}, headers={**h, "Origin": "https://evil.example"})[0] == 403
    assert eng.calls == [("pause", {})]


def test_the_local_dashboard_still_wants_plain_http_on_its_own_names(make):
    eng, dash = make(key=None)
    host = f"127.0.0.1:{dash.port}"
    h = {"Host": host, "X-Flotilla-Token": dash.token}
    assert req(dash, "POST", "/api/pause", {}, headers={**h, "Origin": f"https://{host}"})[0] == 403
    assert req(dash, "POST", "/api/pause", {}, headers={**h, "Origin": f"http://{host}"})[0] == 200


# ---------------------------------------------------------------- 3. odd input never drops the connection


def test_non_ascii_keys_and_tokens_are_just_wrong(make):
    _, dash = make()
    assert req(dash, "GET", "/?key=%C3%A9")[0] == 401
    c = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=5)
    c.putrequest("GET", "/api/state", skip_host=True)
    c.putheader("Host", "x")
    c.putheader("X-Flotilla-Key", "é".encode())
    c.endheaders()
    assert c.getresponse().status == 401
    c.close()
    c = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=5)
    c.putrequest("POST", "/api/pause", skip_host=True)
    for k, v in [("Host", "x"), ("X-Flotilla-Key", KEY), ("Content-Type", "application/json"),
                 ("Content-Length", "2")]:
        c.putheader(k, v)
    c.putheader("X-Flotilla-Token", "é".encode())
    c.endheaders(b"{}")
    assert c.getresponse().status == 403
    c.close()


@pytest.mark.parametrize("raw", [b"[]", b"null", b"3", b"[" * 10000 + b"]" * 10000, b"\xff\xfe"])
def test_bodies_that_arent_a_json_object_get_a_400(make, raw):
    eng, dash = make()
    monkey = dashboard.MAX_BODY
    try:
        dashboard.MAX_BODY = 64 * 1024
        code, _, body = req(dash, "POST", "/api/pause", raw=raw, headers=_good(dash))
    finally:
        dashboard.MAX_BODY = monkey
    assert code == 400 and json.loads(body)["error"] and eng.calls == []


def test_a_failing_action_gets_a_500_not_a_dropped_connection(make):
    _, dash = make(fail=OSError(28, "No space left on device"))
    code, _, body = req(dash, "POST", "/api/settings", {"x": 1}, headers=_good(dash))
    assert code == 500 and "No space left" in json.loads(body)["error"]
    assert req(dash, "GET", "/api/state", headers=_good(dash))[0] == 200


def test_a_client_that_hangs_up_is_forgotten_quietly(make, capsys):
    _, dash = make()
    for _ in range(5):
        s = socket.create_connection(("127.0.0.1", dash.port), timeout=5)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        s.sendall(f"GET /api/state HTTP/1.1\r\nHost: x\r\nX-Flotilla-Key: {KEY}\r\n\r\n".encode())
        s.close()                           # a reset, before the answer is read
    time.sleep(0.5)
    assert "Traceback" not in capsys.readouterr().err
    assert req(dash, "GET", "/api/state", headers=_good(dash))[0] == 200


def test_remote_submit_turns_a_state_folder_it_cant_write_into_a_clear_error(tmp_path):
    (tmp_path / "inbox").write_text("not a folder")
    with pytest.raises(RuntimeError, match="couldn't hand the request to the node"):
        RemoteEngine(tmp_path).submit("pause", {})


# ---------------------------------------------------------------- 6. the cookie


def _cookies(headers):
    return [v for k, v in headers if k.lower() == "set-cookie"]


def test_the_cookie_is_derived_port_named_and_lax(make):
    _, dash = make()
    code, headers, _ = req(dash, "GET", "/?key=" + KEY)
    assert code == 303
    new, old = _cookies(headers)
    name, value = new.split(";", 1)[0].split("=", 1)
    assert name == f"flotilla_key_{dash.port}" and value == dash.cookie_value() and KEY not in new
    assert "HttpOnly" in new and "SameSite=Lax" in new
    assert old.startswith("flotilla_key=;") and "Max-Age=0" in old        # the old raw-key cookie goes
    code, headers, page = req(dash, "GET", "/", headers={"Cookie": f"{name}={value}"})
    assert code == 200 and dash.token.encode() in page and not _cookies(headers)
    assert req(dash, "GET", "/api/state", headers={"Cookie": f"{name}={KEY}"})[0] == 401   # the key isn't the cookie
    assert req(dash, "GET", "/api/state", headers={"Cookie": f"flotilla_key_1={value}"})[0] == 401


def test_old_cookies_still_work_once_and_are_replaced(make):
    _, dash = make()
    now = int(time.time() // dashboard.COOKIE_PERIOD)
    for cookie in (f"flotilla_key={KEY}", f"flotilla_key_{dash.port}={dash.cookie_value(now - 1)}"):
        code, headers, _ = req(dash, "GET", "/", headers={"Cookie": cookie})
        assert code == 200
        assert any(c.startswith(f"flotilla_key_{dash.port}={dash.cookie_value()}") for c in _cookies(headers))
    stale = f"flotilla_key_{dash.port}={dash.cookie_value(now - 2)}"
    assert req(dash, "GET", "/", headers={"Cookie": stale})[0] == 401


# ---------------------------------------------------------------- 4. the node inside a VPN container


def test_no_dashboard_env_stops_the_nodes_own_dashboard_but_not_the_container_one(tmp_path, monkeypatch):
    monkeypatch.setenv(dashboard.NO_DASHBOARD_ENV, "1")
    with pytest.raises(OSError, match="dashboard container"):       # `flotilla run` logs it and seeds on
        Dashboard(FakeEngine(tmp_path), 0)
    d = Dashboard(RemoteEngine(tmp_path), 0, key=KEY)
    d.httpd.server_close()


def test_the_node_container_is_told_not_to_serve_a_dashboard():
    from flotilla import containers

    vpn = containers.VpnContainer("gluetun", "qmcgaw/gluetun")
    node = containers.node_command(vpn, "flotilla", "img")
    assert f"{dashboard.NO_DASHBOARD_ENV}=1" in node
    dash = containers.dashboard_command(7421, "flotilla", "lan", None, None, "img")
    assert not any(dashboard.NO_DASHBOARD_ENV in part for part in dash)


# ---------------------------------------------------------------- 5. gluetun's key stays off argv and out of print


def test_secret_env_goes_to_docker_in_a_private_file_removed_afterwards(monkeypatch):
    import os
    import stat
    import subprocess

    from flotilla import containers

    vpn = containers.VpnContainer("gluetun", "qmcgaw/gluetun",
                                  env={"HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE": '{"auth":"apikey","apikey":"s3cret"}'})
    cmd = containers.node_command(vpn, "flotilla", "img")
    seen = {}

    def fake_run(argv, timeout=None, env=None):
        path = argv[argv.index("--env-file") + 1]
        with open(path) as f:
            seen.update(argv=argv, mode=stat.S_IMODE(os.stat(path).st_mode), text=f.read(), path=path)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(containers.subprocess, "run", fake_run)
    assert containers.run(cmd) == 0
    assert seen["argv"][:3] == ["docker", "run", "--env-file"] and seen["argv"][-2:] == ["img", "run"]
    assert not any("s3cret" in a for a in seen["argv"]) and "s3cret" not in containers.shell(cmd)
    assert seen["mode"] == 0o600 and seen["text"] == 'FLOTILLA_GLUETUN_AUTH={"auth":"apikey","apikey":"s3cret"}\n'
    assert not os.path.exists(seen["path"])
    assert "-e 'FLOTILLA_GLUETUN_AUTH=<HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE of gluetun>'" in containers.shell(cmd)


# ---------------------------------------------------------------- 10. an update keeps what the container was started with


def _node(**over):
    from tests.test_release import IMAGE_NODE

    doc = json.loads(json.dumps(IMAGE_NODE))
    for k, v in over.items():
        doc[k] = {**doc.get(k, {}), **v} if isinstance(v, dict) else v
    return doc


def _installed(doc, monkeypatch, image_env=None):
    import subprocess

    from flotilla import containers
    from tests.test_release import _from

    old = _from(doc)

    def docker(*a):
        if a[:2] == ("image", "inspect") and image_env is not None:
            return subprocess.CompletedProcess(a, 0, json.dumps([{"Config": {"Env": image_env}}]), "")
        if a[:2] == ("inspect", "--format"):
            return subprocess.CompletedProcess(a, 0, "/gluetun\n", "")
        return subprocess.CompletedProcess(a, 1, "", "No such object")

    monkeypatch.setattr(containers, "_docker", docker)
    return old


def test_update_carries_mounts_user_env_and_ports(monkeypatch):
    from flotilla import containers

    doc = _node(
        Mounts=[{"Type": "volume", "Name": "flotilla", "Destination": "/root"},
                {"Type": "bind", "Source": "/mnt/models,big", "Destination": "/root/seeds", "RW": True},
                {"Type": "bind", "Source": "/etc/ssl", "Destination": "/certs", "RW": False},
                {"Type": "volume", "Name": "cache", "Destination": "/cache"}],
        Config={"User": "1000:1000", "Env": ["HOME=/root", "PATH=/usr/bin", "TZ=Europe/Paris",
                                             "HTTPS_PROXY=http://u:pw@proxy:3128", "FLOTILLA_X=1"]},
        HostConfig={"NetworkMode": "bridge",
                    "PortBindings": {"7421/tcp": [{"HostIp": "10.0.0.5", "HostPort": "7421"},
                                                  {"HostIp": "fd00::5", "HostPort": "7421"}],
                                     "6881/udp": [{"HostIp": "", "HostPort": "6881"}]},
                    "CapAdd": ["NET_ADMIN"]})
    old = _installed(doc, monkeypatch, image_env=["HOME=/root", "PATH=/usr/bin"])
    cmd = containers.run_args(old, "img")
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--mount"]
    assert mounts == ['type=bind,"source=/mnt/models,big",target=/root/seeds',     # CSV-quoted for --mount
                      "type=bind,source=/etc/ssl,target=/certs,readonly",
                      "type=volume,source=cache,target=/cache"]
    assert cmd[cmd.index("--user") + 1] == "1000:1000" and "flotilla:/root" in cmd
    ports = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-p"]
    assert ports == ["10.0.0.5:7421:7421", "[fd00::5]:7421:7421", "6881:6881/udp"]
    assert cmd[cmd.index("--cap-add") + 1] == "NET_ADMIN" and "FLOTILLA_X=1" in cmd
    assert cmd.env == {"TZ": "Europe/Paris", "HTTPS_PROXY": "http://u:pw@proxy:3128"}   # in the env file
    assert not any("pw@proxy" in a or a.startswith(("HOME=", "PATH=")) for a in cmd)


def test_update_without_the_old_image_still_drops_the_pythons_own_env(monkeypatch):
    from flotilla import containers

    doc = _node(Config={"Env": ["PATH=/usr/local/bin", "PYTHON_VERSION=3.13.1", "GPG_KEY=x", "LANG=C.UTF-8",
                                "PUID=1000"]})
    cmd = containers.run_args(_installed(doc, monkeypatch), "img")
    assert cmd.env == {"PUID": "1000"}


def test_the_node_stops_serving_its_own_dashboard_after_an_update(monkeypatch):
    from flotilla import containers
    from flotilla.dashboard import NO_DASHBOARD_ENV

    node = containers.run_args(_installed(_node(), monkeypatch), "img")
    assert node[node.index("--network") + 1] == "container:gluetun"
    assert node.count(f"{NO_DASHBOARD_ENV}=1") == 1 and node[-2:] == ["img", "run"]


def test_a_hand_made_node_keeps_its_dashboard_flag_once(monkeypatch):
    from flotilla import containers
    from flotilla.dashboard import NO_DASHBOARD_ENV

    doc = _node(Config={"Env": ["HOME=/root", f"{NO_DASHBOARD_ENV}=1"]})
    node = containers.run_args(_installed(doc, monkeypatch), "img")
    assert node.count(f"{NO_DASHBOARD_ENV}=1") == 1


@pytest.mark.parametrize("over,says", [
    ({"Config": {"Labels": {"com.docker.compose.project": "media"}}}, "Docker Compose"),
    ({"Mounts": [{"Type": "npipe", "Source": "x", "Destination": "/p"}]}, "mount at /p"),
    ({"Mounts": [{"Type": "bind", "Source": "/srv", "Destination": "/s", "Mode": "z"}]}, "SELinux"),
    ({"HostConfig": {"Links": ["/db:/flotilla/db"]}}, "--link"),
    ({"Config": {"Env": ["NOTE=two\nlines"]}}, "line break"),
])
def test_an_update_that_cant_carry_something_refuses_before_changing_anything(monkeypatch, over, says):
    from flotilla import containers

    old = _installed(_node(**over), monkeypatch)
    calls = []
    monkeypatch.setattr(containers, "_docker", lambda *a: calls.append(a))
    with pytest.raises(containers.DockerError, match=says):
        containers.replace(old, "img")
    assert not any(c[0] in ("rename", "stop", "rm") for c in calls)


# ---------------------------------------------------------------- 7. opening the dashboard keeps the key off argv


@pytest.mark.parametrize("platform,browser_env", [("darwin", None), ("darwin", "firefox"), ("linux", None)])
def test_opening_a_keyed_dashboard_never_puts_the_key_on_a_command_line(tmp_path, monkeypatch, capsys,
                                                                        platform, browser_env):
    import argparse
    import webbrowser

    from flotilla import cli
    from flotilla.config import Config, render_config
    from flotilla.remote import dashboard_key

    state = tmp_path / "state"
    key = dashboard_key(state)
    dash = Dashboard(FakeEngine(tmp_path), 0, key=key, access="lan")
    dash.start()
    try:
        cfg = Config()
        cfg.dashboard_port, cfg.dashboard_access = dash.port, "lan"
        (tmp_path / "c.toml").write_text(render_config(cfg))
        argv_opened, stdin_opened = [], []

        class OSA:                                      # macOS: the address goes to osascript's stdin
            def __init__(self, name):
                pass

            def open(self, url):
                stdin_opened.append(url)

        monkeypatch.setattr(cli.sys, "platform", platform)
        monkeypatch.setattr(webbrowser, "MacOSXOSAScript", OSA, raising=False)
        monkeypatch.setattr(webbrowser, "open", lambda url: argv_opened.append(url))   # a browser's argv
        if browser_env:
            monkeypatch.setenv("BROWSER", browser_env)
        else:
            monkeypatch.delenv("BROWSER", raising=False)
        args = argparse.Namespace(config=str(tmp_path / "c.toml"), state=str(state), port=None, url=False,
                                  serve=False, listen="127.0.0.1", access=None, no_open=False)
        assert cli.cmd_dashboard(args) == 0
    finally:
        dash.stop()
    assert not any(key in u for u in argv_opened)
    if platform == "darwin" and not browser_env:
        assert stdin_opened == [f"http://127.0.0.1:{dash.port}/?key={key}"] and not argv_opened
    else:
        assert argv_opened == [f"http://127.0.0.1:{dash.port}"] and not stdin_opened
        assert "open the address above once" in capsys.readouterr().out


# ---------------------------------------------------------------- 8. systemd reads ExecStart back exactly


def _systemd_split(line):
    """systemd's own reading of an ExecStart line, for the escapes _systemd_word writes."""
    words, i = [], 0
    while i < len(line):
        if line[i] == " ":
            i += 1
            continue
        w = ""
        if line[i] == '"':
            i += 1
            while line[i] != '"':
                if line[i] == "\\":
                    if line[i + 1] == "x":
                        w, i = w + chr(int(line[i + 2:i + 4], 16)), i + 4
                    else:
                        w, i = w + line[i + 1], i + 2
                    continue
                w, i = w + line[i], i + 1
            i += 1
        else:
            while i < len(line) and line[i] != " ":
                w, i = w + line[i], i + 1
        assert "%" not in w.replace("%%", "") and "$" not in w.replace("$$", "")   # nothing left to expand
        words.append(w.replace("%%", "%").replace("$$", "$"))
    return words


def test_systemd_unit_escapes_specifiers_variables_and_quotes():
    from pathlib import Path

    from flotilla import service

    exe = ["/home/x/100% $HOME \"q\" it's\\;/flotilla"]
    text = service.systemd_unit(exe, Path("/home/x/c;fg %h.toml"), Path("/home/x/st ate"))
    line = next(l for l in text.splitlines() if l.startswith("ExecStart="))
    assert _systemd_split(line.removeprefix("ExecStart=")) == [
        exe[0], "--config", "/home/x/c;fg %h.toml", "--state", "/home/x/st ate", "run"]


@pytest.mark.parametrize("bad", ["/home/x/a\nExecStartPre=/bin/evil", "/home/x/t\tab", "/home/x/\x1b[2J"])
def test_systemd_unit_refuses_a_path_with_a_line_break_or_control_character(bad):
    from pathlib import Path

    from flotilla import service

    with pytest.raises(ValueError, match="line break or another control character"):
        service.systemd_unit(["/bin/flotilla"], Path(bad), Path("/s"))


# ---------------------------------------------------------------- 9. log rotation recovers


FLAKY_ROTATE = """
import os
from flotilla import service
real, fails = os.open, [1]
def flaky(p, *a, **k):
    if str(p).endswith(".new") and fails:
        fails.pop()
        raise OSError(24, "Too many open files")
    return real(p, *a, **k)
os.open = flaky
print(service.rotate_own_log(max_bytes=1000, keep=2, every=0), flush=True)
print(service.rotate_own_log(max_bytes=1000, keep=2, every=0), flush=True)
print("after", flush=True)
"""

STUCK = """
import os
from flotilla import service
p = os.environ[service.LOG_ENV]
os.replace(p, p + ".1")         # what a rotation that couldn't open a new log left behind
print("stuck", flush=True)
print(service.rotate_own_log(max_bytes=10**9, every=0), flush=True)
print("after", flush=True)
"""


def test_a_rotation_that_cant_open_the_new_log_moves_nothing_and_retries(tmp_path):
    from tests.test_service import _run_logging

    log = tmp_path / "flotilla.log"
    log.write_text("x" * 3000 + "\n")
    _run_logging(log, log, FLAKY_ROTATE)
    assert log.read_text() == "True\nafter\n"
    assert (tmp_path / "flotilla.log.1").read_text().endswith("x\nFalse\n")
    assert not (tmp_path / "flotilla.log.new").exists()


def test_stdout_left_on_the_old_copy_gets_a_fresh_log(tmp_path):
    from tests.test_service import _run_logging

    log = tmp_path / "flotilla.log"
    log.write_text("start\n")
    _run_logging(log, log, STUCK)
    assert log.read_text() == "True\nafter\n"
    assert (tmp_path / "flotilla.log.1").read_text() == "start\nstuck\n"

