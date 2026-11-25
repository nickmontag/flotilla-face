"""Hostile and broken replies on the network paths: the tracker scrape, netsafe's fetches, gluetun's
control server, the magnet index's files, the Hugging Face token's re-check and the hash ledger.

Every server here is on loopback; none of these tests reach the internet."""

from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

from flotilla import hfauth, magnetindex, netsafe, sweep, vpnport
from flotilla.hfauth import Account, Connection, TokenError
from flotilla.ledger import load_ledger
from flotilla.netsafe import FetchError, UnsafeURL
from flotilla.signing import SecretKey
from flotilla.torrentmeta import bencode, make_torrent, parse_torrent


class Raw:
    """A loopback TCP server that reads a request, then plays `script`: bytes to send, or a float to
    sleep for. Closes afterwards (or resets the connection with reset=True)."""

    def __init__(self, script, reset=False):
        self.script, self.reset = script, reset
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._play, args=(c,), daemon=True).start()

    def _play(self, c):
        try:
            c.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                got = c.recv(65536)
                if not got:
                    return
                data += got
            for step in self.script:
                if self.stop.is_set():
                    return
                if isinstance(step, float):
                    time.sleep(step)
                else:
                    c.sendall(step)
            if self.reset:
                c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.stop.set()
        self.srv.close()


def trickle(head: bytes = b"HTTP/1.1 200 OK\r\n\r\n", n: int = 200, every: float = 0.1):
    return [head] + [x for _ in range(n) for x in (every, b"x")]


@pytest.fixture
def local_https(monkeypatch):
    """netsafe's opener, its connections sent to a loopback HTTP server instead (https URLs pass the
    allowlist; the deadline machinery is the real one)."""
    servers = []

    def use(server: Raw):
        servers.append(server)

        def conn(host, timeout=None, context=None, **kw):
            return http.client.HTTPConnection("127.0.0.1", server.port, timeout=timeout)

        opener = urllib.request.build_opener(netsafe._NoRedirect(), netsafe._WatchedHTTPSHandler(conn, None))
        monkeypatch.setattr(netsafe, "_OPENER", opener)
        return server

    yield use
    for s in servers:
        s.close()


# ---------------------------------------------------------------- 1. the tracker scrape


def H(i: int) -> str:
    return f"{i:040x}"


def test_scrape_keeps_only_real_counts():
    hashes = [H(i) for i in range(6)]
    files = {bytes.fromhex(hashes[0]): {b"complete": 3, b"incomplete": b"lots"},     # bytes: dropped
             bytes.fromhex(hashes[1]): {b"complete": b"7", b"incomplete": 1},
             bytes.fromhex(hashes[2]): {b"complete": -4, b"incomplete": 1},
             bytes.fromhex(hashes[3]): {b"complete": 2 ** 62, b"incomplete": 2 ** 62},   # clamped
             bytes.fromhex(hashes[4]): {b"complete": 2, b"incomplete": [1]},
             bytes.fromhex(hashes[5]): {b"complete": 5, b"incomplete": 7}}
    got = sweep.scrape(hashes, get=lambda path: bencode({b"files": files}))
    assert got[hashes[0]] == (3, 0) and got[hashes[1]] == (0, 0) and got[hashes[2]] == (0, 0)
    assert got[hashes[3]] == (sweep.MAX_COUNT, sweep.MAX_COUNT) and got[hashes[4]] == (2, 0)
    assert got[hashes[5]] == (5, 7)
    json.dumps(got)                     # it goes into state.json: this must never raise
    assert sweep._count(True) is None and sweep._count(0) == 0   # a bool isn't a count (a `get` may hand one)


@pytest.mark.parametrize("script", [
    [b"SSH-2.0-OpenSSH_9.6\r\n"],                                           # BadStatusLine
    [b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"d5:filesd" ],     # IncompleteRead
    [b"HTTP/1.1 200 OK\r\nX: " + b"a" * 70000 + b"\r\n\r\n"],                # LineTooLong
])
def test_a_broken_scrape_reply_is_an_oserror(script):
    srv = Raw(script)
    try:
        with pytest.raises(OSError):
            sweep.scrape([H(1)], get=lambda p: sweep._get(p, timeout=5, deadline=10, host="127.0.0.1",
                                                          port=srv.port))
    finally:
        srv.close()


def test_scrape_turns_http_exceptions_from_any_get_into_oserror():
    def get(path):
        raise http.client.BadStatusLine("junk")

    with pytest.raises(OSError):
        sweep.scrape([H(1)], get=get)


@pytest.mark.parametrize("head", [b"HTTP/1.1 200 OK\r\n\r\n", b""])     # trickled body, or trickled headers
def test_a_trickling_tracker_cant_hold_the_refresh(head):
    srv = Raw(trickle(head, n=400, every=0.05) if head else [b"H"] + [x for _ in range(400) for x in (0.05, b"T")])
    try:
        t = time.monotonic()
        with pytest.raises(OSError, match="longer than"):
            sweep._get("/scrape", timeout=5, deadline=1.0, host="127.0.0.1", port=srv.port)
        assert time.monotonic() - t < 4
    finally:
        srv.close()


def test_a_scrape_reply_within_the_deadline_still_works():
    body = bencode({b"files": {bytes.fromhex(H(1)): {b"complete": 2, b"incomplete": 1}}})
    srv = Raw([b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body), body])
    try:
        got = sweep.scrape([H(1)], get=lambda p: sweep._get(p, timeout=5, deadline=10, host="127.0.0.1",
                                                            port=srv.port))
        assert got == {H(1): (2, 1)}
    finally:
        srv.close()


# ---------------------------------------------------------------- 2. netsafe.fetch and post_json


@pytest.mark.parametrize("script, reset", [
    ([b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n", b"x" * 10], False),   # IncompleteRead
    ([b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n", b"x" * 10], True),    # reset mid-body
    ([b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n", b"zz\r\n"], False),  # bad chunk
    ([b"NOT HTTP\r\n"], False),                                                  # BadStatusLine
])
def test_fetch_failures_mid_reply_are_fetch_errors(local_https, script, reset):
    local_https(Raw(script, reset=reset))
    with pytest.raises(FetchError):
        netsafe.fetch("https://pirateface.co/api/x", max_bytes=1 << 20, timeout=5)
    with pytest.raises(FetchError):
        netsafe.post_json("https://pirateface.co/api/rescue", {"repoId": "a/b"}, timeout=5)


def test_fetch_times_out_mid_body_as_a_fetch_error(local_https):
    local_https(Raw([b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n", b"x", 3.0]))
    with pytest.raises(FetchError):
        netsafe.fetch("https://pirateface.co/api/x", max_bytes=1 << 20, timeout=0.5)


@pytest.mark.parametrize("head", [b"HTTP/1.1 200 OK\r\n\r\n", b"HTTP/1.1 200 OK\r\nX-Slow: "])
def test_a_trickling_host_cant_hold_a_fetch(local_https, head):
    local_https(Raw(trickle(head, n=400, every=0.05)))
    t = time.monotonic()
    with pytest.raises(FetchError, match="longer than"):
        netsafe.fetch("https://pirateface.co/api/x", max_bytes=1 << 20, timeout=5, deadline=1.0)
    with pytest.raises(FetchError, match="longer than"):
        netsafe.post_json("https://pirateface.co/api/rescue", {}, timeout=5, deadline=1.0)
    assert time.monotonic() - t < 6


def test_a_reply_in_time_is_unchanged(local_https):
    local_https(Raw([b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n", b'{"a":', 0.2, b" 1}"]))
    r = netsafe.fetch("https://pirateface.co/api/x", max_bytes=1 << 20, timeout=5, deadline=10)
    assert r.json() == {"a": 1} and r.headers["content-type"] == "application/json"


def test_the_default_deadline_leaves_slow_large_downloads_room():
    assert netsafe.default_deadline(64 << 20) > 3600          # a 64 MiB release file at 16 KiB/s
    assert netsafe.default_deadline(1 << 20) >= netsafe.MIN_DEADLINE


@pytest.mark.parametrize("url", ["https://huggingface.co:99999/x", "https://huggingface.co:abc/x",
                                 "https://[huggingface.co/x"])
def test_a_bad_port_or_bracket_is_an_unsafe_url(url):
    with pytest.raises(UnsafeURL):
        netsafe.check_url(url, netsafe.DEFAULT_ALLOWED_HOSTS)


def test_a_redirect_to_a_bad_port_is_an_unsafe_url(monkeypatch):
    class Hdrs(dict):
        def get(self, k, d=None):
            return super().get(k, d)

    class Opener:
        def open(self, req, timeout):
            raise urllib.error.HTTPError(req.full_url, 302, "Found", Hdrs(Location="https://huggingface.co:99999/x"),
                                         None)

    monkeypatch.setattr(netsafe, "_OPENER", Opener())
    with pytest.raises(UnsafeURL):
        netsafe.fetch("https://huggingface.co/x", max_bytes=100)
    with pytest.raises(UnsafeURL):
        netsafe.fetch_range("https://huggingface.co/x", 0, 10, allowed=netsafe.DEFAULT_ALLOWED_HOSTS)


def test_deeply_nested_json_is_a_fetch_error():
    r = netsafe.Response("https://pirateface.co/x", 200, {}, b"[" * 100_000)
    with pytest.raises(FetchError, match="expected JSON"):
        r.json()


# ---------------------------------------------------------------- 5. signed links out of messages


def test_fetch_errors_never_show_a_signed_link():
    signed = "https://cas-bridge.xethub.hf.co/xet-bridge-us/abc?X-Amz-Signature=deadbeef&Expires=1700000000"
    e = FetchError(signed, 500, "HTTP 500")
    assert "deadbeef" not in str(e) and "Expires" not in str(e)
    assert str(e).startswith("https://cas-bridge.xethub.hf.co/xet-bridge-us/abc")
    assert e.url == signed                                   # the code still has it all
    with pytest.raises(UnsafeURL) as u:
        netsafe.check_url("https://evil.example/f?sig=deadbeef", netsafe.DEFAULT_ALLOWED_HOSTS)
    assert "deadbeef" not in str(u.value)
    assert "secret" not in netsafe.shown("https://user:secret@pirateface.co/x")


def test_a_cdn_failure_logged_by_hfpieces_has_no_signature(monkeypatch):
    class Hdrs(dict):
        def get(self, k, d=None):
            return super().get(k, d)

    class Opener:
        def open(self, req, timeout):
            if "?" not in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 302, "Found",
                                             Hdrs(Location="https://cdn-lfs.huggingface.co/f?Signature=deadbeef"), None)
            raise urllib.error.HTTPError(req.full_url, 503, "Busy", Hdrs(), None)

    monkeypatch.setattr(netsafe, "_OPENER", Opener())
    with pytest.raises(FetchError) as e:
        netsafe.fetch_range("https://huggingface.co/a/b/resolve/c/f", 0, 10, allowed=netsafe.DEFAULT_ALLOWED_HOSTS)
    assert "deadbeef" not in f"couldn't fetch from Hugging Face ({e.value})"
    assert "Signature=deadbeef" in e.value.url


# ---------------------------------------------------------------- 3. gluetun's port taken by something else


def test_a_non_http_listener_on_gluetuns_port_isnt_gluetun(monkeypatch, tmp_path):
    srv = Raw([b"\x00\x01binary junk\r\n"])
    base = f"http://127.0.0.1:{srv.port}"
    try:
        with pytest.raises(vpnport.GluetunAbsent):
            vpnport.gluetun_port(base=base)
        with pytest.raises(vpnport.GluetunAbsent):
            vpnport.gluetun_restart_vpn(base=base, timeout=5)
        # The finder stops asking it and goes on to the other ways.
        monkeypatch.setattr(vpnport.gluetun_port, "__defaults__", ("", base, 3.0))
        monkeypatch.setattr(vpnport, "piactl_port", lambda: (0, ""))
        port_file = tmp_path / "port"
        port_file.write_text("40123\n")
        f = vpnport.PortFinder("wg0", port_file=str(port_file), gluetun_auth="", address=lambda: None,
                               log=lambda m: None)
        f.gluetun = True
        fw = f.find(6881)
        assert f.gluetun is False and (fw.port, fw.source) == (40123, "file")
    finally:
        srv.close()


# ---------------------------------------------------------------- 4. the magnet index's files


def test_shards_are_streamed_and_overlong_lines_dont_count(tmp_path, monkeypatch):
    d = tmp_path / "shards"
    d.mkdir()
    (d / "2026-11-09.txt").write_text("x" * (magnetindex.MAX_LINE * 3) + "\n" + "\n" * 1000)
    assert magnetindex.parse_shards(d) == {}
    (d / "removed-2026-11-09.txt").write_text("a" * 40 + "\n" + "b" * (magnetindex.MAX_LINE * 2) + "\n")
    assert magnetindex.parse_shards(d) is None               # an overlong removal fails closed
    (d / "removed-2026-11-09.txt").write_text(("a" * 40 + "\n") * 20)
    assert magnetindex.parse_shards(d) == {}
    monkeypatch.setattr(magnetindex, "MAX_LINES", 10)
    assert magnetindex.parse_shards(d) is None               # a removals list past the cap fails closed


class FakeEng:
    def __init__(self, tmp_path):
        self.state = types.SimpleNamespace(dir=tmp_path / "state", data={})
        self.cfg = types.SimpleNamespace(keep_magnet_index=True)
        self.lock = threading.Lock()
        self.logs: list[str] = []
        self.log = self.logs.append


def build(*shards: str) -> bytes:
    files = [("LICENSE", b"CC0\n")] + [(f"shards/{s}", f"{s}\n".encode()) for s in shards]
    return make_torrent(files, "magnet-index")


class PF:
    def __init__(self, listing, raw=None, fail=False):
        self.listing, self.raw, self.fail = listing, raw, fail

    def torrent(self, repo, quant=None):
        return self.listing

    def torrent_file(self, ih):
        if self.fail:
            raise FetchError("https://pirateface.co/api/torrents/x.torrent", None, "connection reset")
        return self.raw


def test_a_failed_torrent_fetch_is_retried_next_refresh(tmp_path):
    eng = FakeEng(tmp_path)
    idx = magnetindex.MagnetIndex(eng)
    raw = build("2026-11-09.txt")
    ih = parse_torrent(raw).infohash_v1
    with pytest.raises(FetchError):
        idx.sync(PF({"infohash": ih}, fail=True), now=1e9)
    assert "synced" not in idx.rec                           # so the next refresh asks again
    idx.sync(PF({"infohash": ih}, raw), now=1e9 + 1)
    assert idx.rec["synced"] == 1e9 + 1 and idx._pending == ih and (idx.dir / f"{ih}.torrent").exists()


@pytest.mark.parametrize("listing", [["not", "a", "dict"], "html", {"infohash": 12345}, {"infohash": ["a" * 40]}])
def test_an_odd_listing_reply_is_no_build(tmp_path, listing):
    idx = magnetindex.MagnetIndex(FakeEng(tmp_path))
    idx.sync(PF(listing, build("2026-11-09.txt")), now=1e9)
    assert idx.rec["synced"] == 1e9 and idx._pending is None


def _lay_out(idx, raw: bytes, shards: list[str]) -> str:
    ih = parse_torrent(raw).infohash_v1
    idx.dir.mkdir(parents=True, exist_ok=True)
    (idx.dir / f"{ih}.torrent").write_bytes(raw)
    root = idx.files / magnetindex.NAME / "shards"
    root.mkdir(parents=True, exist_ok=True)
    (root.parent / "LICENSE").write_text("CC0\n")
    for s in shards:
        (root / s).write_text(f"{s}\n")
    return ih


def test_old_builds_files_are_deleted_once_unused(tmp_path):
    eng = FakeEng(tmp_path)
    idx = magnetindex.MagnetIndex(eng)
    old = _lay_out(idx, build("2026-11-02.txt", "2026-11-09.txt"), ["2026-11-02.txt", "2026-11-09.txt"])
    # The new build dropped 11-02 (rare, but then it must go) and added 11-16.
    new = _lay_out(idx, build("2026-11-09.txt", "2026-11-16.txt"), ["2026-11-16.txt"])
    (idx.files / f".{old}.parts").write_bytes(b"x")
    (idx.dir / "map.json").write_text("{}")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    shards = idx.files / magnetindex.NAME / "shards"
    os.symlink(outside, shards / "2026-10-26.txt")          # a link: only the link goes
    assert idx.prune({new}) == 4
    assert not (idx.dir / f"{old}.torrent").exists() and (idx.dir / f"{new}.torrent").exists()
    assert sorted(p.name for p in shards.iterdir()) == ["2026-11-09.txt", "2026-11-16.txt"]
    assert (idx.files / magnetindex.NAME / "LICENSE").exists() and (idx.dir / "map.json").exists()
    assert not (idx.files / f".{old}.parts").exists() and outside.read_text() == "keep me"
    assert any("deleted 4 file(s)" in m for m in eng.logs)


def test_pruning_never_follows_a_symlinked_folder(tmp_path):
    idx = magnetindex.MagnetIndex(FakeEng(tmp_path))
    cur = parse_torrent(build("2026-11-09.txt")).infohash_v1
    idx.dir.mkdir(parents=True)
    (idx.dir / f"{cur}.torrent").write_bytes(build("2026-11-09.txt"))
    elsewhere = tmp_path / "elsewhere" / "shards"
    elsewhere.mkdir(parents=True)
    (elsewhere / "2026-01-01.txt").write_text("not ours")
    (idx.files / magnetindex.NAME).mkdir(parents=True)
    os.symlink(elsewhere, idx.files / magnetindex.NAME / "shards")
    idx.prune({cur})
    assert (elsewhere / "2026-01-01.txt").read_text() == "not ours"


def test_pruning_deletes_nothing_when_the_kept_build_cant_be_read(tmp_path):
    idx = magnetindex.MagnetIndex(FakeEng(tmp_path))
    old = _lay_out(idx, build("2026-11-02.txt"), ["2026-11-02.txt"])
    idx.prune({"f" * 40})                                   # its .torrent is missing
    assert (idx.files / magnetindex.NAME / "shards" / "2026-11-02.txt").exists()
    assert not (idx.dir / f"{old}.torrent").exists()        # the old .torrent itself still goes


def test_startup_prunes_what_earlier_runs_left(tmp_path):
    eng = FakeEng(tmp_path)
    idx = magnetindex.MagnetIndex(eng)
    old = _lay_out(idx, build("2026-11-02.txt"), ["2026-11-02.txt"])
    cur = _lay_out(idx, build("2026-11-09.txt"), ["2026-11-09.txt"])
    idx.rec["infohash"] = cur
    idx.resume()
    assert idx._pending == cur and not (idx.dir / f"{old}.torrent").exists()
    assert [p.name for p in (idx.files / magnetindex.NAME / "shards").iterdir()] == ["2026-11-09.txt"]


# ---------------------------------------------------------------- 7. the token is checked again daily


def test_a_token_in_use_is_checked_again_daily(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOTILLA_HF_TOKEN_FILE", raising=False)
    token = "hf_" + "a" * 34
    outcome = [Account("someone", "flotilla", "fineGrained")]
    calls = []

    def check(t):
        calls.append(t)
        if isinstance(outcome[0], Exception):
            raise outcome[0]
        return outcome[0]

    monkeypatch.setattr(hfauth, "check", check)
    path = tmp_path / "hf-token"
    hfauth.save_token(path, token)
    logs = []
    c = Connection(path, logs.append)
    c.refresh()
    assert c.auth() and c.auth() and len(calls) == 1         # not on every fetch
    c._checked_at -= hfauth.RECHECK_EVERY + 1
    assert c.auth() and len(calls) == 2 and sum("using someone" in m for m in logs) == 1
    # Hugging Face can't be reached for the re-check: keep using it, ask again after a while.
    c._checked_at -= hfauth.RECHECK_EVERY + 1
    outcome[0] = FetchError(hfauth.WHOAMI_URL, None, "timed out")
    assert c.auth() and c.auth() and len(calls) == 3 and c.state == "ok"
    c._checked_at -= hfauth.RECHECK_AFTER_ERROR + 1
    # Its permissions were widened since: it stops being used.
    outcome[0] = TokenError("it may also repo.write")
    assert c.auth() is None and len(calls) == 4 and c.state == "refused" and "repo.write" in c.info()["why"]


@pytest.mark.parametrize("fine", [["x"], {"global": "repo.write"}, {"scoped": [{"permissions": "repo.write"}]},
                                  {"scoped": "everything"}])
def test_an_odd_fine_grained_shape_is_not_read_only(fine):
    assert hfauth.read_only_problem({"accessToken": {"role": "fineGrained", "fineGrained": fine}})


# ---------------------------------------------------------------- 8. the hash ledger


def test_a_cached_ledger_signature_that_isnt_text_is_just_unusable(tmp_path):
    cache = tmp_path / "cache" / "ledger"
    cache.mkdir(parents=True)
    (cache / "index.json").write_bytes(b"{}")
    (cache / "index.json.minisig").write_bytes(b"\xff\xfe\x00junk")
    logs = []
    led = load_ledger([SecretKey.generate().public], tmp_path, (), url=None, bundled=None, log=logs.append)
    assert not led.entries and any("cached copy unusable" in m for m in logs)


def test_ledger_fetch_failures_mid_reply_only_mean_fewer_entries(local_https, tmp_path):
    local_https(Raw([b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n", b"{"], reset=True))
    logs = []
    led = load_ledger([SecretKey.generate().public], tmp_path, ("raw.githubusercontent.com",),
                      url="https://raw.githubusercontent.com/x/index.json", bundled=None, log=logs.append)
    assert not led.entries and any("published copy unavailable" in m for m in logs)
