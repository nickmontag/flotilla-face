"""Signatures, feed freshness/rollback, URL allowlist, magnets, .torrent parsing."""

import hashlib
import json
import shutil
import subprocess
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from flotilla import netsafe
from flotilla.feed import FeedError, load_feed, parse_feed, verify_feed
from flotilla.magnet import MagnetError, parse_magnet, validate_magnet
from flotilla.signing import PublicKey, SecretKey, SignatureError, verify
from flotilla.torrentmeta import TorrentError, bencode, make_torrent, parse_torrent

from .fakes import FakeRepo, make_repo


def feed_doc(version=3, days=30, **extra):
    now = datetime.now(timezone.utc)
    doc = {
        "schema": 1, "version": version, "issued": now.isoformat(),
        "expires": (now + timedelta(days=days)).isoformat(),
        "weights": {"capability": 1, "cyber": 1, "takedown_risk": 2},
        "models": [{"repo": "zai-org/GLM-5.2", "scores": {"capability": 90, "cyber": 70, "takedown_risk": 50},
                    "infohash": "0bfb146eaa419a7b0589f7301555c990b7dee14b"}],
        "deny": ["bad/model"],
    }
    doc.update(extra)
    return json.dumps(doc).encode()


# ---------------------------------------------------------------- signing


def test_sign_verify_roundtrip(tmp_path):
    sk = SecretKey.generate()
    sk.save(tmp_path / "k")
    assert (tmp_path / "k").stat().st_mode & 0o777 == 0o600
    sk2 = SecretKey.load(tmp_path / "k")
    pub = PublicKey.parse(sk2.public.line())
    sig = sk2.sign(b"hello", trusted_comment="v:1")
    key, tc = verify(b"hello", sig, [pub])
    assert tc == "v:1" and key.key_id == sk.key_id


def test_verify_rejects_tampering():
    sk, other = SecretKey.generate(), SecretKey.generate()
    sig = sk.sign(b"data", trusted_comment="v:1")
    with pytest.raises(SignatureError):
        verify(b"datA", sig, [sk.public])
    with pytest.raises(SignatureError, match="not trusted"):
        verify(b"data", sig, [other.public])
    forged = sig.replace("trusted comment: v:1", "trusted comment: v:9")
    with pytest.raises(SignatureError):
        verify(b"data", forged, [sk.public])
    # same key id, different key: must still fail
    impostor = PublicKey(sk.key_id, other.public.key)
    with pytest.raises(SignatureError):
        verify(b"data", sig, [impostor])


@pytest.mark.skipif(not shutil.which("minisign"), reason="minisign not installed")
def test_minisign_interop(tmp_path):
    sk = SecretKey.generate()
    (tmp_path / "f").write_bytes(b"interop")
    (tmp_path / "f.minisig").write_text(sk.sign(b"interop"))
    subprocess.run(["minisign", "-Vm", str(tmp_path / "f"), "-P", sk.public.line()], check=True)


# ---------------------------------------------------------------- feed


def test_feed_verify_and_priority():
    sk = SecretKey.generate()
    data = feed_doc()
    feed = verify_feed(data, sk.sign(data), [sk.public], min_version=0)
    m = feed.models[0]
    # (90*1 + 70*1 + 50*2) / 100 / (1+1+2) = 0.65
    assert feed.priority(m) == pytest.approx(0.65)
    assert "bad/model" in feed.deny


def test_feed_rollback_and_expiry():
    sk = SecretKey.generate()
    data = feed_doc(version=3)
    with pytest.raises(FeedError, match="rollback"):
        verify_feed(data, sk.sign(data), [sk.public], min_version=4)
    old = feed_doc(days=-1)
    with pytest.raises(FeedError, match="expired"):
        verify_feed(old, sk.sign(old), [sk.public], min_version=0)


def test_feed_schema_validation():
    for bad in [
        {"models": [{"repo": "../etc/passwd", "scores": {}}]},
        {"models": [{"repo": "a/b", "infohash": "XYZ", "scores": {}}]},
        {"models": [{"repo": "a/b", "scores": {}, "boost": 99}]},
        {"weights": {"x": -1}},
        {"schema": 2},
    ]:
        with pytest.raises(FeedError):
            parse_feed(feed_doc(**bad))


def test_load_feed_ignores_bad_signature_and_uses_cache(tmp_path):
    sk, mallory = SecretKey.generate(), SecretKey.generate()
    good = feed_doc(version=5)
    (tmp_path / "good.json").write_bytes(good)
    (tmp_path / "good.json.minisig").write_text(sk.sign(good))
    evil = feed_doc(version=99, models=[])
    (tmp_path / "evil.json").write_bytes(evil)
    (tmp_path / "evil.json.minisig").write_text(mallory.sign(evil))
    logs = []
    urls = [f"file://{tmp_path}/evil.json", f"file://{tmp_path}/good.json"]
    feed = load_feed(urls, [sk.public], 0, tmp_path / "cache", ("pirateface.co",), log=logs.append)
    assert feed.version == 5 and any("rejected" in l for l in logs)
    # mirrors gone -> cached verified copy is used
    feed2 = load_feed([f"file://{tmp_path}/missing.json"], [sk.public], 0, tmp_path / "cache", (), log=logs.append)
    assert feed2.version == 5 and "cached" in feed2.source
    # no keys -> neutral, never "unsigned but used"
    assert load_feed(urls, [], 0, tmp_path / "c2", (), log=logs.append).version == 0


# ---------------------------------------------------------------- netsafe


@pytest.mark.parametrize("url", [
    "http://pirateface.co/api/rescue",
    "https://evilpirateface.co/x",
    "https://pirateface.co.evil.com/x",
    "https://user:pw@pirateface.co/x",
    "https://pirateface.co:8443/x",
    "file:///etc/passwd",
    "ftp://huggingface.co/x",
])
def test_check_url_rejects(url):
    with pytest.raises(netsafe.UnsafeURL):
        netsafe.check_url(url, netsafe.DEFAULT_ALLOWED_HOSTS)


def test_check_url_accepts_subdomains():
    netsafe.check_url("https://tracker.pirateface.co/x", netsafe.DEFAULT_ALLOWED_HOSTS)
    netsafe.check_url("https://cdn-lfs.huggingface.co/x", netsafe.DEFAULT_ALLOWED_HOSTS)


def test_redirect_off_allowlist_is_refused(monkeypatch):
    class Hdrs(dict):
        def get(self, k, d=None):
            return super().get(k, d)

    class Opener:
        def open(self, req, timeout):
            raise urllib.error.HTTPError(req.full_url, 302, "Found", Hdrs(Location="https://evil.example/x"), None)

    monkeypatch.setattr(netsafe, "_OPENER", Opener())
    with pytest.raises(netsafe.UnsafeURL, match="allowlist"):
        netsafe.fetch("https://pirateface.co/api/rescue", max_bytes=100)


def test_link_header_parsing():
    h = '<https://huggingface.co/api/models/a/b/tree/x?cursor=abc>; rel="next"'
    assert netsafe.parse_link_next(h).endswith("cursor=abc")
    assert netsafe.parse_link_next(None) is None


# ---------------------------------------------------------------- magnets


def test_magnet_accepts_pirateface_format():
    r = FakeRepo("a/b", make_repo(2))
    m = validate_magnet(parse_magnet(r.magnet()))
    assert m.btih == r.infohash and m.trackers[0].startswith("udp://tracker.pirateface.co")


@pytest.mark.parametrize("extra,match", [
    ("&tr=udp%3A%2F%2Fevil.example%3A6969", "tracker"),
    ("&ws=http%3A%2F%2Fpirateface.co%2Fx", "web seed"),
    ("&ws=https%3A%2F%2Fevil.example%2Fx", "web seed"),
    ("&x.pe=1.2.3.4:6881", "not allowed"),
    ("&as=https%3A%2F%2Fevil.example%2Fpayload.exe", "not allowed"),
])
def test_magnet_rejects_foreign_endpoints(extra, match):
    r = FakeRepo("a/b", make_repo(2))
    with pytest.raises(MagnetError, match=match):
        validate_magnet(parse_magnet(r.magnet(extra)))


def test_magnet_base32():
    hexih = "0bfb146eaa419a7b0589f7301555c990b7dee14b"
    import base64

    b32 = base64.b32encode(bytes.fromhex(hexih)).decode()
    assert parse_magnet(f"magnet:?xt=urn:btih:{b32}").btih == hexih


# ---------------------------------------------------------------- torrents


def test_infohash_matches_libtorrent():
    lt = pytest.importorskip("libtorrent")
    r = FakeRepo("a/b", make_repo(3))
    ti = lt.torrent_info(lt.bdecode(r.raw))
    assert str(ti.info_hashes().v1) == r.infohash
    assert [f.repo_path for f in r.meta.files] == sorted(r.files)
    assert r.meta.files[1].offset == len(r.files[sorted(r.files)[0]])


def test_unsafe_paths_rejected():
    for bad in ["../evil", "a/../../b", "/abs", "a\\b", "c:x"]:
        raw = make_torrent([(bad, b"x")], "name")
        with pytest.raises(TorrentError):
            parse_torrent(raw)


def test_garbage_torrent_rejected():
    for raw in [b"", b"d4:infoi1e", b"i01e", bencode({"info": {"name": "x"}}), b"d" * 100]:
        with pytest.raises(TorrentError):
            parse_torrent(raw)


def test_torrent_trackers_extracted():
    info = bencode({"name": "x", "piece length": 16384, "pieces": hashlib.sha1(b"a").digest(), "length": 1})
    raw = b"d8:announce" + bencode("udp://evil.example:1/announce") + b"4:info" + info + b"e"
    meta = parse_torrent(raw)
    assert meta.trackers == ["udp://evil.example:1/announce"]


# ---------------------------------------------------------------- published list and your own changes


def test_shipped_list_verifies_with_the_project_key(tmp_path):
    from flotilla.config import BUILTIN_EDITOR_KEYS
    from flotilla.feed import BUNDLED_FEED

    keys = [PublicKey.parse(k) for k in BUILTIN_EDITOR_KEYS]
    feed = load_feed([], keys, 0, tmp_path, (), log=lambda *_: None, bundled=BUNDLED_FEED)
    assert feed.version >= 1 and feed.source == "bundled with this release" and len(feed.models) >= 1
    assert all(m.revision for m in feed.models)


def test_bundled_list_ignores_expiry_but_not_signature_or_rollback(tmp_path):
    sk, other = SecretKey.generate(), SecretKey.generate()
    old = feed_doc(version=3, days=-30)                       # long expired
    (tmp_path / "b.json").write_bytes(old)
    (tmp_path / "b.json.minisig").write_text(sk.sign(old))
    feed = load_feed([], [sk.public], 0, tmp_path / "c", (), log=lambda *_: None, bundled=tmp_path / "b.json")
    assert feed.version == 3                                   # still used: it ships with the code
    # a newer remote copy wins over the bundled one
    newer = feed_doc(version=4)
    (tmp_path / "r.json").write_bytes(newer)
    (tmp_path / "r.json.minisig").write_text(sk.sign(newer))
    feed = load_feed([f"file://{tmp_path}/r.json"], [sk.public], 0, tmp_path / "c2", (), log=lambda *_: None,
                     bundled=tmp_path / "b.json")
    assert feed.version == 4
    # wrong key: refused even when bundled
    assert load_feed([], [other.public], 0, tmp_path / "c3", (), log=lambda *_: None,
                     bundled=tmp_path / "b.json").version == 0
    # rollback below what was already seen: refused
    assert load_feed([], [sk.public], 9, tmp_path / "c4", (), log=lambda *_: None,
                     bundled=tmp_path / "b.json").version == 0


def test_local_changes_override_add_deny_and_replace(tmp_path):
    from flotilla.feed import apply_local

    sk = SecretKey.generate()
    data = feed_doc()
    feed = verify_feed(data, sk.sign(data), [sk.public], 0)
    local = tmp_path / "my.json"
    local.write_text(json.dumps({
        "models": [
            {"repo": "zai-org/GLM-5.2", "scores": {"capability": 10, "cyber": 10, "takedown_risk": 10}},
            {"repo": "me/my-favourite", "scores": {"capability": 90, "cyber": 90, "takedown_risk": 90}},
            {"repo": "author/model", "scores": {}},          # the template placeholder is ignored
        ],
        "deny": ["Some/Model"],
    }))
    f = apply_local(feed, local)
    by = {m.repo: m for m in f.models}
    assert set(by) == {"zai-org/GLM-5.2", "me/my-favourite"}
    assert by["zai-org/GLM-5.2"].local and f.priority(by["zai-org/GLM-5.2"]) == pytest.approx(0.10)
    assert "some/model" in f.deny and "bad/model" in f.deny and f.local_source == str(local)

    data = feed_doc()
    feed = verify_feed(data, sk.sign(data), [sk.public], 0)
    local.write_text(json.dumps({"replace_published": True, "models": [{"repo": "me/only", "scores": {}}]}))
    assert [m.repo for m in apply_local(feed, local).models] == ["me/only"]


def test_broken_local_file_is_ignored_not_fatal(tmp_path):
    from flotilla.feed import apply_local, neutral_feed

    local = tmp_path / "my.json"
    local.write_text("{ not json")
    logs = []
    f = apply_local(neutral_feed(), local, log=logs.append)
    assert f.models == [] and not f.local_source and "ignoring" in logs[0]
    local.write_text(json.dumps({"models": [{"repo": "../../etc", "scores": {}}]}))
    assert apply_local(neutral_feed(), local, log=logs.append).models == []


def test_non_json_reply_is_a_fetch_error(monkeypatch):
    class Resp:
        status = 200
        headers = {"Content-Type": "text/html"}

        def read(self, n=-1):
            out, self.headers["_done"] = (b"" if self.headers.get("_done") else b"<!DOCTYPE html><html>"), True
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, req, timeout):
            return Resp()

    monkeypatch.setattr(netsafe, "_OPENER", Opener())
    with pytest.raises(netsafe.FetchError, match="expected JSON"):
        netsafe.fetch_json("https://pirateface.co/api/models")


# ---------------------------------------------------------------- picks by link


@pytest.mark.parametrize("text,repo,rev", [
    ("https://huggingface.co/dealignai/GLM-5.3-UNCENSORED-NVFP4", "dealignai/GLM-5.3-UNCENSORED-NVFP4", None),
    ("https://huggingface.co/zai-org/GLM-5.2/tree/cf457fa734ab149ffef225f80893eb38c6ff5cdc", "zai-org/GLM-5.2",
     "cf457fa734ab149ffef225f80893eb38c6ff5cdc"),
    ("https://huggingface.co/zai-org/GLM-5.2/tree/main", "zai-org/GLM-5.2", None),
    ("https://hf.co/a/b?x=1#frag", "a/b", None),
    ("https://pirateface.co/zai-org/GLM-5.3-Flash", "zai-org/GLM-5.3-Flash", None),
    ("  zai-org/GLM-5.2  ", "zai-org/GLM-5.2", None),
    ("magnet:?xt=urn:btih:4d9ec92c4a942fda13cdeaed1ea6f596a9392163&ws=https%3A%2F%2Fpirateface.co%2Fapi%2Fws%2F"
     "zai-org%2FGLM-5.3-Flash%2Feb9eb208eb0d988989d07a6a12d0fdeb5f52574a%2F", "zai-org/GLM-5.3-Flash",
     "eb9eb208eb0d988989d07a6a12d0fdeb5f52574a"),
])
def test_parse_model_ref(text, repo, rev):
    from flotilla.feed import parse_model_ref

    assert parse_model_ref(text) == (repo, rev)


@pytest.mark.parametrize("text", [
    "", "https://huggingface.co/datasets/a/b", "https://huggingface.co/spaces/a/b", "https://huggingface.co/zai-org",
    "https://evil.example/a/b", "https://pirateface.co.evil.example/a/b", "../../etc/passwd", "a/b/c d",
    "magnet:?xt=urn:btih:4d9ec92c4a942fda13cdeaed1ea6f596a9392163",
])
def test_parse_model_ref_refuses(text):
    from flotilla.feed import FeedError, parse_model_ref

    with pytest.raises(FeedError):
        parse_model_ref(text)


def test_add_and_remove_picks(tmp_path):
    from flotilla.feed import add_pick, apply_local, list_picks, neutral_feed, remove_pick

    path = tmp_path / "my-priorities.json"
    assert add_pick(path, "https://huggingface.co/a/b", 80) == ("a/b", True)
    assert add_pick(path, "a/b", 90) == ("a/b", False)            # updating, not duplicating
    assert add_pick(path, "https://pirateface.co/c/d")[0] == "c/d"
    assert [m["repo"] for m in list_picks(path)] == ["a/b", "c/d"]
    f = apply_local(neutral_feed(), path)
    assert {m.repo for m in f.models} == {"a/b", "c/d"} and all(m.local for m in f.models)
    assert f.priority(next(m for m in f.models if m.repo == "a/b")) == pytest.approx(0.90)
    assert remove_pick(path, "https://huggingface.co/a/b") == "a/b"
    assert remove_pick(path, "a/b") is None
    assert [m["repo"] for m in list_picks(path)] == ["c/d"]
    with pytest.raises(ValueError):
        add_pick(path, "a/b", 150)


# ---------------------------------------------------------------- being gentle with Pirate Face


def test_torrent_cache_and_backoff(tmp_path, monkeypatch):
    from flotilla import sources

    r = FakeRepo("a/b", make_repo(2))
    calls = []

    def fake_fetch(url, **kw):
        calls.append(url)
        return netsafe.Response(url, 200, {}, r.raw)

    monkeypatch.setattr(sources, "fetch", fake_fetch)
    pf = sources.PirateFace(("pirateface.co",), cache_dir=tmp_path)
    assert pf.torrent_file(r.infohash) == r.raw and len(calls) == 1
    assert pf.torrent_file(r.infohash) == r.raw and len(calls) == 1          # served from the cache
    (tmp_path / "torrents" / f"{r.infohash}.torrent").write_bytes(b"tampered")
    assert pf.torrent_file(r.infohash) == r.raw and len(calls) == 2          # bad cache entry refetched

    def failing(status, retry_after=None):
        def f(url, **kw):
            calls.append(url)
            raise netsafe.FetchError(url, status, f"HTTP {status}", retry_after)
        return f

    # three server errors in a row: no more requests for the rest of this refresh
    monkeypatch.setattr(sources, "fetch_json", failing(500))
    for _ in range(3):
        with pytest.raises(netsafe.FetchError):
            pf.torrent("x/y")
    n = len(calls)
    with pytest.raises(netsafe.FetchError, match="no more requests"):
        pf.torrent("x/y")
    assert len(calls) == n                                                    # backed off: no request sent

    # a 429 or 503 pauses every client in this process for as long as Retry-After says
    monkeypatch.setattr(sources, "fetch_json", failing(429, retry_after=120))
    fresh = sources.PirateFace(("pirateface.co",), cache_dir=tmp_path)
    with pytest.raises(netsafe.FetchError, match="HTTP 429"):
        fresh.torrent("x/y")
    n = len(calls)
    again = sources.PirateFace(("pirateface.co",), cache_dir=tmp_path)     # the next refresh's client too
    with pytest.raises(netsafe.FetchError, match="asked for a pause"):
        again.torrent("x/y")
    assert len(calls) == n and 110 < sources._pf_not_before - __import__("time").time() <= 120
