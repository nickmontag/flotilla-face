"""Pieces no peer can supply, fetched from Hugging Face (flotilla.hfpieces), and the network
rules those requests go through (netsafe.fetch_range, netsafe.pin)."""

import hashlib
import random
import socket
import threading
import time
import types
import urllib.error
import urllib.parse

import pytest

lt = pytest.importorskip("libtorrent")

from flotilla import hfpieces, netsafe
from flotilla.config import Config, State, load_config, render_config
from flotilla.engine import Engine
from flotilla.hfpieces import HF_HOSTS, Gated, HFPieces, Later, Refused, link_expires
from flotilla.torrentmeta import parse_torrent

REV = "c0ffee" * 6 + "c0ff"
PIECE = 32768
CDN = "https://us.aws.cdn.hf.co/xet-bridge-us/"


# ---------------------------------------------------------------- netsafe.fetch_range


class Hdrs(dict):
    def get(self, k, d=None):
        return super().get(k, d)


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, Hdrs(headers), body

    def read(self, n=-1):
        out, self.body = (self.body, b"") if n < 0 else (self.body[:n], self.body[n:])
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RangeOpener:
    """huggingface.co redirects to the CDN, which answers byte ranges of `data`."""

    def __init__(self, data, *, status=206, content_range=None, body=None, cdn=CDN + "f?Expires=99"):
        self.data, self.status, self.content_range, self.body, self.cdn = data, status, content_range, body, cdn
        self.seen = []   # (host, Authorization header)

    def open(self, req, timeout):
        host = urllib.parse.urlsplit(req.full_url).hostname
        self.seen.append((host, req.get_header("Authorization")))
        if host == "huggingface.co":
            raise urllib.error.HTTPError(req.full_url, 302, "Found", Hdrs(Location=self.cdn), None)
        lo, hi = (int(x) for x in req.get_header("Range").split("=")[1].split("-"))
        body = self.data[lo:hi + 1] if self.body is None else self.body
        rng = self.content_range or f"bytes {lo}-{hi}/{len(self.data)}"
        return Resp(self.status, {"Content-Range": rng, "Content-Length": str(len(body))}, body)


def test_fetch_range_follows_redirects_and_sends_the_token_only_to_hugging_face(monkeypatch):
    data = bytes(range(256)) * 64
    op = RangeOpener(data)
    monkeypatch.setattr(netsafe, "_OPENER", op)
    got, final = netsafe.fetch_range("https://huggingface.co/org/m/resolve/x/a.bin", 1000, 5000, allowed=HF_HOSTS,
                                     auth=("huggingface.co", "Bearer secret"))
    assert got == data[1000:6000] and final == CDN + "f?Expires=99"
    assert op.seen == [("huggingface.co", "Bearer secret"), ("us.aws.cdn.hf.co", None)]


@pytest.mark.parametrize("kw, match", [
    ({"status": 200}, "ignored the byte range"),                         # a whole file for a range
    ({"content_range": "bytes 0-4999/16384"}, "wrong range"),            # not the bytes we asked for
    ({"body": b"short"}, "expected 5000 bytes"),                         # cut off
])
def test_fetch_range_refuses_anything_but_the_exact_range(monkeypatch, kw, match):
    monkeypatch.setattr(netsafe, "_OPENER", RangeOpener(bytes(16384), **kw))
    with pytest.raises(netsafe.FetchError, match=match):
        netsafe.fetch_range("https://huggingface.co/org/m/resolve/x/a.bin", 1000, 5000, allowed=HF_HOSTS)


def test_fetch_range_redirect_off_the_allowlist_is_refused(monkeypatch):
    monkeypatch.setattr(netsafe, "_OPENER", RangeOpener(bytes(100), cdn="https://evil.example/f"))
    with pytest.raises(netsafe.UnsafeURL, match="allowlist"):
        netsafe.fetch_range("https://huggingface.co/org/m/resolve/x/a.bin", 0, 10, allowed=HF_HOSTS)


def test_fetch_range_reports_retry_after(monkeypatch):
    class Busy:
        def open(self, req, timeout):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", Hdrs({"Retry-After": "30"}), None)

    monkeypatch.setattr(netsafe, "_OPENER", Busy())
    with pytest.raises(netsafe.FetchError) as e:
        netsafe.fetch_range("https://huggingface.co/x", 0, 10, allowed=HF_HOSTS)
    assert e.value.status == 429 and e.value.retry_after == 30


# ---------------------------------------------------------------- netsafe.pin


def test_pinned_requests_leave_from_the_pin_or_not_at_all():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    try:
        assert netsafe._opener() is netsafe._OPENER
        netsafe.pin("127.0.0.1")
        assert netsafe._opener() is netsafe._PINNED_OPENER
        s = netsafe.pinned_connection(("127.0.0.1", port), 5)
        assert s.getsockname()[0] == "127.0.0.1"
        s.close()
        netsafe.pin("192.0.2.77")            # TEST-NET-1: an address this machine doesn't have (VPN down)
        with pytest.raises(OSError):
            netsafe.pinned_connection(("127.0.0.1", port), 5)
        netsafe.pin("nosuchvpn0")            # an interface that isn't up
        with pytest.raises(OSError, match="down"):
            netsafe.pinned_connection(("127.0.0.1", port), 5)
    finally:
        netsafe.pin("")
        srv.close()
    assert netsafe._opener() is netsafe._OPENER


def test_config_round_trips_and_pins_requests(tmp_path):
    cfg = Config()
    cfg.hf_fallback, cfg.hf_gb_per_day, cfg.bind_interface = False, 7.5, "10.8.0.2"
    path = tmp_path / "config.toml"
    path.write_text(render_config(cfg))
    try:
        back = load_config(path)
        assert (back.hf_fallback, back.hf_gb_per_day, back.bind_interface) == (False, 7.5, "10.8.0.2")
        assert netsafe.pinned_to() == "10.8.0.2"
    finally:
        netsafe.pin("")


def test_link_expiry_is_read_from_signed_links():
    assert link_expires(CDN + "abc?Expires=1794792783&Signature=x") == 1794792783
    from datetime import datetime, timezone

    signed = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc).timestamp()
    assert link_expires("https://cdn-lfs.hf.co/x?X-Amz-Date=20261115T120000Z&X-Amz-Expires=3600") == signed + 3600
    assert link_expires("https://huggingface.co/org/m/resolve/x/a.bin") is None


# ---------------------------------------------------------------- a Pirate Face torrent on a real libtorrent


def pf_torrent(tmp_path, sizes):
    """A hybrid torrent like Pirate Face's: v1 + v2, no piece layers, a web seed."""
    src = tmp_path / "src" / "m"
    src.mkdir(parents=True)
    rng = random.Random(5)
    for name, size in sizes.items():
        (src / name).write_bytes(rng.randbytes(size))
    fs = lt.file_storage()
    lt.add_files(fs, str(src))
    ct = lt.create_torrent(fs, PIECE)
    lt.set_piece_hashes(ct, str(src.parent))
    full = ct.generate()
    full[b"url-list"] = [f"https://pirateface.co/api/ws/org/m/{REV}/".encode()]
    return src, lt.bencode({k: v for k, v in full.items() if k != b"piece layers"})


def held_engine(tmp_path, raw, src, wanted, logs, verify_source="huggingface"):
    cfg = Config()
    cfg.folder, cfg.listen_port, cfg.hf_fallback = tmp_path / "seeds", 0, True
    eng = Engine(cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False)
    meta = parse_torrent(raw)
    ih = meta.infohash_v1
    idx = {f.repo_path: f.index for f in meta.files if not f.pad}
    files = {}
    for n in wanted:
        f = meta.files[idx[n]]
        files[str(f.index)] = {"path": n, "tpath": f.path, "size": f.size, "status": "wanted", "prio": 4,
                               "sha256": hashlib.sha256((src / n).read_bytes()).hexdigest()}
    eng.state.holdings[ih] = {"repo": "org/m", "revision": REV, "verify_source": verify_source,
                              "save_path": str(tmp_path / "seeds" / "m"), "files": files}
    eng.torrent_dir.mkdir(parents=True, exist_ok=True)
    (eng.torrent_dir / f"{ih}.torrent").write_bytes(raw)
    prios = [0] * len(meta.files)
    for n in wanted:
        prios[idx[n]] = 4
    h = eng._add(ih, prios)
    end = time.monotonic() + 10
    while time.monotonic() < end and not (h.status().has_metadata and h.get_file_priorities() == prios
                                          and h.status().state == lt.torrent_status.downloading):
        time.sleep(0.05)
    return eng, h, ih, idx, meta


class FakeHF:
    """Stands in for netsafe.fetch_range: Hugging Face redirects to a signed CDN link that serves
    the source files."""

    def __init__(self, src, bad_from=None, error=None):
        self.src, self.bad_from, self.error = src, bad_from, error
        self.calls, self.links = [], {}

    def __call__(self, url, start, length, *, allowed, auth=None, on_chunk=None, timeout=60.0):
        assert allowed == HF_HOSTS
        self.calls.append(url)
        if self.error:
            raise self.error
        parts = urllib.parse.urlsplit(url)
        if parts.hostname == "huggingface.co":
            prefix = f"/org/m/resolve/{REV}/"
            assert parts.path.startswith(prefix)
            path = urllib.parse.unquote(parts.path[len(prefix):])
            final = f"{CDN}{len(self.links)}?Expires={int(time.time()) + 3600}&Signature=x"
            self.links[final] = path
        else:
            path, final = self.links[url], url
        data = (self.src / path).read_bytes()[start:start + length]
        if self.bad_from is not None and start >= self.bad_from:
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        if on_chunk:
            on_chunk(len(data))
        return data, final


def make_stuck(eng, key):
    t = time.time()
    eng.hf.scan(t)
    work = eng.hf.scan(t + hfpieces.STUCK_WINDOW + 60)     # ten minutes on, nothing from peers
    assert key in eng.hf.stuck
    return work


def test_off_until_turned_on_and_off_stops_it(tmp_path, monkeypatch):
    src, raw = pf_torrent(tmp_path, {"a.bin": 4 * PIECE})
    logs = []
    eng, h, ih, idx, meta = held_engine(tmp_path, raw, src, ["a.bin"], logs)
    assert Config().hf_fallback is False
    fake = FakeHF(src)
    monkeypatch.setattr(hfpieces.netsafe, "fetch_range", fake)
    key = (ih, idx["a.bin"])
    eng.cfg.hf_fallback = False
    t = time.time()
    assert eng.hf.scan(t) == [] and eng.hf.scan(t + 700) == [] and not eng.hf.stuck
    eng.cfg.hf_fallback = True                                    # turned on from the dashboard
    make_stuck(eng, key)

    real = fake.__call__
    def switch_off_after_one(*a, **kw):                           # turned off again mid-file
        out = real(*a, **kw)
        eng.cfg.hf_fallback = False
        return out
    monkeypatch.setattr(hfpieces.netsafe, "fetch_range", switch_off_after_one)
    eng.hf.fetch_file(key, time.monotonic() + 60)
    first, last = meta.files[idx["a.bin"]].piece_range(meta.piece_length)
    assert sum(h.have_piece(p) for p in range(first, last + 1)) == 1
    eng.handles = {}
    eng.shutdown()


def test_web_seeds_are_dropped(tmp_path):
    src, raw = pf_torrent(tmp_path, {"a.bin": 3 * PIECE})
    eng, h, *_ = held_engine(tmp_path, raw, src, ["a.bin"], [])
    assert h.url_seeds() == []
    eng.handles = {}
    eng.shutdown()


def test_a_stuck_file_is_fetched_from_hugging_face(tmp_path, monkeypatch):
    src, raw = pf_torrent(tmp_path, {"a.bin": 5 * PIECE + 1000, "b.bin": 2 * PIECE})
    logs = []
    eng, h, ih, idx, meta = held_engine(tmp_path, raw, src, ["a.bin"], logs)
    fake = FakeHF(src)
    monkeypatch.setattr(hfpieces.netsafe, "fetch_range", fake)
    key = (ih, idx["a.bin"])
    assert make_stuck(eng, key) == [key]
    assert any("fetching its missing pieces from Hugging Face" in l for l in logs)

    eng.hf.fetch_file(key, time.monotonic() + 60)
    first, last = meta.files[idx["a.bin"]].piece_range(meta.piece_length)
    assert all(h.have_piece(p) for p in range(first, last + 1))
    b0, b1 = meta.files[idx["b.bin"]].piece_range(meta.piece_length)
    assert not any(h.have_piece(p) for p in range(b0, b1 + 1))       # not wanted: not fetched
    ti = h.torrent_file()
    fetched = sum(ti.piece_size(p) for p in range(first, last + 1))
    assert eng.state.holdings[ih]["hf_bytes"] == fetched
    assert eng.state.data["hf"]["bytes"] == eng.state.data["hf"]["total"] == fetched
    assert eng.hf.info()["total"] == fetched
    # One request to huggingface.co; the signed link it led to served the rest.
    assert sum(1 for u in fake.calls if "huggingface.co" in u) == 1 and len(fake.calls) == last - first + 1

    end = time.monotonic() + 10
    target = tmp_path / "seeds" / "m" / meta.files[idx["a.bin"]].path
    while time.monotonic() < end and not (target.exists() and target.read_bytes() == (src / "a.bin").read_bytes()):
        h.flush_cache()
        time.sleep(0.1)
    assert target.read_bytes() == (src / "a.bin").read_bytes()

    # The history counts it as downloaded (libtorrent's own counters don't see add_piece).
    eng.sample(time.time(), force=True)
    down = eng.history.db.execute("SELECT SUM(down) FROM traffic WHERE res = 60 AND ih = ?", (ih,)).fetchone()[0]
    assert down >= fetched
    assert eng.hf.take_new() == {}
    eng.handles = {}
    eng.shutdown()


def test_bad_bytes_from_hugging_face_never_reach_libtorrent(tmp_path, monkeypatch):
    """Without piece layers libtorrent can only check a v2 file as a whole: one bad last piece
    and it throws away every piece of the file. So a piece that doesn't match the torrent's own
    hash is never handed over, and after two the file stops using Hugging Face."""
    src, raw = pf_torrent(tmp_path, {"a.bin": 4 * PIECE})
    logs = []
    eng, h, ih, idx, meta = held_engine(tmp_path, raw, src, ["a.bin"], logs)
    monkeypatch.setattr(hfpieces.netsafe, "fetch_range", FakeHF(src, bad_from=PIECE))   # all but piece 0 are bad
    key = (ih, idx["a.bin"])
    make_stuck(eng, key)
    eng.hf.fetch_file(key, time.monotonic() + 60)
    first, _ = meta.files[idx["a.bin"]].piece_range(meta.piece_length)
    assert h.have_piece(first) and not h.have_piece(first + 1)
    assert not [a for a in eng.ses.pop_alerts() if isinstance(a, lt.hash_failed_alert)]
    assert key in eng.hf.off and any("failed the torrent's hash check" in l for l in logs)
    assert eng.hf.scan(time.time() + 2000) == []                    # and it isn't tried again
    time.sleep(0.5)
    assert h.have_piece(first)                                      # the good piece was kept
    eng.handles = {}
    eng.shutdown()


@pytest.mark.parametrize("error, kind, words", [
    (netsafe.FetchError("https://huggingface.co/org/m/resolve/x/a.bin", 401, "HTTP 401"), Gated, "flotilla hf connect"),
    (netsafe.FetchError("https://huggingface.co/org/m/resolve/x/a.bin", 403, "HTTP 403",
                        headers={"X-Error-Message": "Access to this resource is disabled."}), Refused, "disabled"),
    (netsafe.FetchError("https://huggingface.co/org/m/resolve/x/a.bin", 404, "HTTP 404"), Refused, "no longer on"),
    (netsafe.FetchError("https://huggingface.co/org/m/resolve/x/a.bin", 429, "HTTP 429", 120.0), Later, "couldn't fetch"),
    (netsafe.FetchError("https://us.aws.cdn.hf.co/x", None, "timed out"), Later, "couldn't fetch"),
    (netsafe.UnsafeURL("host not on allowlist"), Refused, "off the allowlist"),
])
def test_what_hugging_face_refusals_and_trouble_lead_to(monkeypatch, error, kind, words):
    monkeypatch.delenv("FLOTILLA_HF_TOKEN_FILE", raising=False)
    hf = HFPieces(types.SimpleNamespace(log=lambda *_: None, cfg=Config()))
    monkeypatch.setattr(hfpieces.netsafe, "fetch_range", FakeHF(None, error=error))
    with pytest.raises(kind, match=words) as e:
        hf._range({"repo": "org/m", "revision": REV}, "a" * 40, 0, "a.bin", 0, 10)
    if isinstance(error, netsafe.FetchError) and error.retry_after:
        assert e.value.seconds == 120


def test_an_expired_download_link_is_replaced(tmp_path, monkeypatch):
    (tmp_path / "a.bin").write_bytes(bytes(range(100)))
    fake = FakeHF(tmp_path)
    old = CDN + "old?Expires=1"
    real = fake.__call__

    def call(url, *a, **kw):
        if url == old:
            fake.calls.append(url)
            raise netsafe.FetchError(url, 403, "HTTP 403")     # the CDN says the signature has expired
        return real(url, *a, **kw)

    monkeypatch.setattr(hfpieces.netsafe, "fetch_range", call)
    hf = HFPieces(types.SimpleNamespace(log=lambda *_: None, lock=threading.RLock(), handles={}, cfg=Config()))
    hf._links[("a" * 40, 0)] = (old, time.time() + 3600)          # looked valid, wasn't
    assert hf._range({"repo": "org/m", "revision": REV}, "a" * 40, 0, "a.bin", 10, 5) == bytes(range(10, 15))
    assert fake.calls[0] == old and "huggingface.co" in fake.calls[1]
    assert hf._links[("a" * 40, 0)][0].startswith(CDN)


def test_trouble_backs_off_and_says_when(tmp_path, monkeypatch):
    src, raw = pf_torrent(tmp_path, {"a.bin": 2 * PIECE})
    logs = []
    eng, _h, ih, idx, _ = held_engine(tmp_path, raw, src, ["a.bin"], logs)
    busy = netsafe.FetchError("https://huggingface.co/x", 429, "HTTP 429", 120.0)
    monkeypatch.setattr(hfpieces.netsafe, "fetch_range", FakeHF(src, error=busy))
    key = (ih, idx["a.bin"])
    make_stuck(eng, key)
    eng.hf.fetch_file(key, time.monotonic() + 60)
    assert eng.hf._wait_until > time.time() + 110 and any("trying again in 2 min" in l for l in logs)
    assert eng.hf.info()["retry_at"]
    eng.handles = {}
    eng.shutdown()


# ---------------------------------------------------------------- when a file counts as stuck


class SlowHandle:
    def __init__(self, n, rate=0):
        self.progress, self.rate, self.prios = [0] * n, rate, [4] * n

    def status(self):
        return types.SimpleNamespace(download_payload_rate=self.rate,
                                     flags=lt.torrent_flags.upload_mode & lt.torrent_flags.paused)

    def file_progress(self):
        return list(self.progress)

    def get_file_priorities(self):
        return list(self.prios)


def stuck_engine(tmp_path, logs):
    cfg = Config()
    cfg.folder, cfg.listen_port, cfg.hf_fallback = tmp_path / "seeds", 0, True
    eng = Engine(cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False)
    for ih, source in (("a" * 40, "huggingface"), ("b" * 40, "pirateface-only")):
        eng.state.holdings[ih] = {"repo": f"org/{ih[0]}", "revision": REV, "verify_source": source, "files": {
            str(i): {"path": f"f{i}", "size": 10 ** 9, "status": "wanted"} for i in range(2)}}
        eng.handles[ih] = SlowHandle(2)
    return eng


def test_stuck_means_peers_sent_almost_nothing_for_ten_minutes(tmp_path):
    logs = []
    eng = stuck_engine(tmp_path, logs)
    hf, a, b, t = eng.hf, "a" * 40, "b" * 40, 1_000_000.0
    assert hf.scan(t) == []                                          # too soon to tell
    assert hf.scan(t + 700) == [(a, 0), (a, 1)]                      # b isn't on Hugging Face
    assert set(hf.stuck) == {(a, 0), (a, 1), (b, 0), (b, 1)}
    assert any("org/b: no peer is sending it, and Hugging Face doesn't have it" in l for l in logs)

    eng.handles[a].progress[0] += 50_000_000                         # peers deliver ~70 KB/s again
    hf.fetched[(a, 1)] += 50_000_000                                 # bytes from Hugging Face don't count
    eng.handles[a].progress[1] += 50_000_000
    work = hf.scan(t + 1400)
    assert (a, 0) not in hf.stuck and work == [(a, 1)]

    eng.handles[a].rate = int(0.9 * eng.cfg.download_bps)            # the line is full: slow isn't stuck
    eng.handles[a].progress.append(0)
    eng.handles[a].prios.append(4)
    eng.state.holdings[a]["files"]["2"] = {"path": "f2", "size": 10 ** 9, "status": "wanted"}
    hf.scan(t + 1500)
    hf.scan(t + 2200)
    assert (a, 2) not in hf.stuck

    eng.user_paused = True                                           # paused: nothing is stuck
    assert hf.scan(t + 2300) == [] and not hf.stuck
    eng.handles = {}
    eng.shutdown()


def test_daily_limit(tmp_path):
    logs = []
    eng = stuck_engine(tmp_path, logs)
    eng.cfg.hf_gb_per_day = 1.0
    today = hfpieces._today()
    eng.state.data["hf"] = {"day": today, "bytes": 999_000_000, "total": 5}
    assert eng.hf._budget_allows(500_000) and not eng.hf._budget_allows(2_000_000)
    assert any("today's 1 GB is used up" in l for l in logs)
    eng.state.data["hf"]["day"] = "2000-01-01"                       # a new day (UTC)
    assert eng.hf._budget_allows(2_000_000) and eng.state.data["hf"]["bytes"] == 0
    eng.cfg.hf_gb_per_day = 0                                        # no limit
    eng.state.data["hf"]["bytes"] = 10 ** 15
    assert eng.hf._budget_allows(2_000_000)
    eng.handles = {}
    eng.shutdown()


# ---------------------------------------------------------------- setup


def test_setup_asks_and_defaults_to_off(tmp_path, monkeypatch):
    import argparse

    from flotilla import cli, netinfo, sources

    monkeypatch.setattr(netinfo, "internet_route", lambda: None)

    class NoPF:
        def __init__(self, *a, **kw):
            pass

        def licenses(self):
            raise OSError("offline")

    monkeypatch.setattr(sources, "PirateFace", NoPF)
    seen = []

    def run(answers):
        it = iter(answers)

        def ask(prompt=""):
            seen.append(prompt)
            return next(it, "")

        monkeypatch.setattr("builtins.input", ask)
        path = tmp_path / f"c{len(seen)}.toml"
        assert cli.cmd_init(argparse.Namespace(config=str(path))) == 0
        return load_config(path)

    folder = str(tmp_path / "seeds")
    cfg = run([folder])                                          # every other answer left at its default
    assert cfg.hf_fallback is False
    asked = [p for p in seen if "Hugging Face" in p]
    assert asked and asked[0].endswith("[y/N]: ")
    hf_prompt = asked[0]
    answers = [folder] + [""] * 20

    def on(prompt=""):
        seen.append(prompt)
        if prompt == hf_prompt:
            return "y"
        if prompt.startswith("Most to fetch from Hugging Face"):
            return "7"
        return answers.pop(0) if answers else ""

    monkeypatch.setattr("builtins.input", on)
    path = tmp_path / "on.toml"
    assert cli.cmd_init(argparse.Namespace(config=str(path))) == 0
    cfg = load_config(path)
    assert cfg.hf_fallback is True and cfg.hf_gb_per_day == 7
    netsafe.pin("")
