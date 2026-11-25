"""Hostile or failed answers from Pirate Face and Hugging Face: each becomes a rejection (transient
when nothing was learnt), never an exception, a file outside the cache, or a deleted model."""

from __future__ import annotations

import hashlib
import json
import random
import unicodedata

import pytest

from flotilla import netsafe, sources
from flotilla.candidates import Rejection, TorrentCand, build_candidate, gather
from flotilla.config import Config
from flotilla.feed import neutral_feed
from flotilla.magnet import MagnetError, parse_magnet, validate_magnet
from flotilla.netsafe import FetchError, Response
from flotilla.sources import HuggingFace, PirateFace, parse_new_torrents
from flotilla.torrentmeta import MAX_ELEMENTS, TorrentError, bencode, bdecode, make_torrent, parse_torrent

from .fakes import FakeHF, FakePF, FakeRepo, make_repo


def build(r: FakeRepo, pf=None, hf=None, **cfgkw):
    c = Config()
    for k, v in cfgkw.items():
        setattr(c, k, v)
    return build_candidate(r.repo, None, neutral_feed(), None, pf or FakePF([r]), hf or FakeHF([r]), c)


class TreeOnlyHF(HuggingFace):
    """The real license lookup (its cache, its fallbacks); trees from a FakeRepo."""

    def __init__(self, cache_dir, repo: FakeRepo, gone=False):
        super().__init__(("huggingface.co",), cache_dir)
        self.r, self.gone = repo, gone

    def tree(self, repo, revision):
        return None if self.gone else self.r.tree()


def answer(monkeypatch, fn):
    asked = []

    def fake(url, **kw):
        asked.append(url)
        return fn(url)

    monkeypatch.setattr(sources, "fetch_json", fake)
    return asked


def card(lic):
    return {"cardData": {"license": lic}}


def raise_(status):
    def f(url):
        raise FetchError(url, status, f"HTTP {status}")
    return f


# ------------------------------------------------- 1. license lookups that fail or say "gone"


def test_unreachable_license_falls_back_to_a_stale_cached_one(tmp_path, monkeypatch):
    hf = HuggingFace(("huggingface.co",), tmp_path)
    answer(monkeypatch, lambda url: card("mit"))
    assert hf.license_lookup("a/b") == ("mit", "huggingface")
    now = sources.time.time()
    monkeypatch.setattr(sources.time, "time", lambda: now + HuggingFace.LICENSE_TTL + 10)
    for status in (429, 503, None):
        answer(monkeypatch, raise_(status))
        assert hf.license_lookup("a/b") == ("mit", "unreachable")
        assert hf.license("a/b") == "mit"


def test_unreachable_license_never_known_is_a_transient_rejection(tmp_path, monkeypatch):
    r = FakeRepo("a/b", make_repo(2))
    answer(monkeypatch, raise_(503))
    c = build(r, hf=TreeOnlyHF(tmp_path, r))
    assert isinstance(c, Rejection) and c.transient and c.reason.startswith("license unknown")


def test_a_license_survives_hugging_face_taking_the_repo_down(tmp_path, monkeypatch):
    r = FakeRepo("a/b", make_repo(2), hf_backed=False)
    hf = TreeOnlyHF(tmp_path, r)
    answer(monkeypatch, lambda url: card("mit"))
    assert isinstance(build(r, hf=hf), TorrentCand)

    now = sources.time.time()
    monkeypatch.setattr(sources.time, "time", lambda: now + HuggingFace.LICENSE_TTL + 10)
    hf.gone = True
    asked = answer(monkeypatch, raise_(404))
    c = build(r, hf=hf)
    assert isinstance(c, TorrentCand) and c.license == "mit" and c.verify_source == "pirateface-only"
    assert len(asked) == 1
    # The gone answer is cached for a day, and doesn't overwrite the license it had.
    assert hf.license_lookup("a/b") == ("mit", "gone") and len(asked) == 1
    saved = json.loads((tmp_path / "hf-licenses.json").read_text())["a/b"]
    assert saved["kept"] == "mit" and saved["gone"] is True
    monkeypatch.setattr(sources.time, "time", lambda: now + HuggingFace.LICENSE_TTL + HuggingFace.MISSING_TTL + 20)
    assert hf.license_lookup("a/b") == ("mit", "gone") and len(asked) == 2
    # ...and a day later Hugging Face can't be reached: still known.
    monkeypatch.setattr(sources.time, "time", lambda: now + HuggingFace.LICENSE_TTL + 2 * HuggingFace.MISSING_TTL + 30)
    answer(monkeypatch, raise_(502))
    assert hf.license_lookup("a/b") == ("mit", "unreachable")


def test_a_repo_gone_before_its_license_was_read_is_kept_not_deleted(tmp_path, monkeypatch):
    r = FakeRepo("a/b", make_repo(2), hf_backed=False)
    answer(monkeypatch, raise_(451))
    c = build(r, hf=TreeOnlyHF(tmp_path, r, gone=True))
    assert isinstance(c, Rejection) and c.transient and "never read" in c.reason


def test_a_repo_hugging_face_has_with_no_license_is_still_refused(tmp_path, monkeypatch):
    r = FakeRepo("a/b", make_repo(2))
    answer(monkeypatch, lambda url: {"cardData": {}})
    c = build(r, hf=TreeOnlyHF(tmp_path, r))
    assert isinstance(c, Rejection) and not c.transient and c.reason == "license unknown"


def test_old_cache_entries_still_read(tmp_path, monkeypatch):
    now = sources.time.time()
    (tmp_path / "hf-licenses.json").write_text(json.dumps({"a/b": ["apache-2.0", now - 9 * 86400],
                                                           "c/d": [None, now]}))
    hf = HuggingFace(("huggingface.co",), tmp_path)
    answer(monkeypatch, raise_(503))
    assert hf.license_lookup("a/b") == ("apache-2.0", "unreachable")
    # An old [None, when] can't say whether the repo was gone or unlicensed: asked again.
    asked = answer(monkeypatch, raise_(404))
    assert hf.license_lookup("c/d") == (None, "gone") and len(asked) == 1


def test_fake_hf_can_fail_or_be_gone():
    r = FakeRepo("a/b", make_repo(2))
    hf = FakeHF([r])
    hf.licenses["a/b"] = (None, "unreachable")
    assert build(r, hf=hf).transient
    hf.licenses["a/b"] = (None, "gone")
    assert build(r, hf=hf).transient
    hf.licenses["a/b"] = (None, "huggingface")
    assert not build(r, hf=hf).transient
    hf.licenses["a/b"] = ("mit", "gone")
    assert isinstance(build(r, hf=hf), TorrentCand)


def test_a_400_from_pirate_face_is_a_failed_lookup_not_a_delisting(monkeypatch):
    pf = PirateFace(("pirateface.co",))
    answer(monkeypatch, raise_(400))
    with pytest.raises(FetchError):
        pf.torrent("a/b")
    answer(monkeypatch, raise_(404))
    assert pf.torrent("a/b") is None
    r = FakeRepo("a/b", make_repo(2))
    fpf = FakePF([r])
    fpf.overrides["a/b"] = None
    c = build(r, pf=fpf)
    assert isinstance(c, Rejection) and c.delisted and not c.transient


# ------------------------------------------------- 2. revision and repo reaching file paths


@pytest.mark.parametrize("rev", ["main?" + "a" * 13 + "=/../../../ledger-seen",
                                 "main?" + "a" * 19 + "=/../../../state",
                                 ("x/" + "../" * 3 + "." + "/" * 17 + "planted")[:40].ljust(40, "d")])
def test_a_revision_from_pirate_face_cannot_write_outside_the_cache(tmp_path, monkeypatch, rev):
    """The audit's reproduction: a 40-character revision with a path in it."""
    assert len(rev) == 40
    state = tmp_path / "state"
    state.mkdir()
    (state / "ledger-seen.json").write_text('{"keep": 1}')
    (state / "state.json").write_text('{"keep": 1}')
    r = FakeRepo("evil/model", make_repo(2, 1000))
    listing = r.listing()
    listing["revision"] = rev
    pf = FakePF([r])
    pf.overrides["evil/model"] = listing
    urls = []

    def fetch(url, **kw):
        urls.append(url)
        if "/tree/" in url:
            if "planted" in rev:
                raise netsafe.FetchError(url, 404, "HTTP 404")
            body = json.dumps([{"type": "file", "path": p, "size": len(d), "oid": "0" * 40}
                               for p, d in r.files.items()]).encode()
            return Response(url, 200, {}, body)
        return Response(url, 200, {}, json.dumps(card("mit")).encode())

    monkeypatch.setattr(sources, "fetch", fetch)
    monkeypatch.setattr(sources, "fetch_json", lambda url, **kw: fetch(url).json())
    hf = HuggingFace(("huggingface.co",), state / "cache")
    c = build_candidate("evil/model", None, neutral_feed(), None, pf, hf, Config())
    assert isinstance(c, Rejection) and "revision" in c.reason
    assert not any("/tree/" in u for u in urls)
    assert (state / "ledger-seen.json").read_text() == '{"keep": 1}'
    assert (state / "state.json").read_text() == '{"keep": 1}'
    assert not list(tmp_path.rglob("*.gone"))


def test_hf_tree_refuses_paths_and_quotes_the_revision(tmp_path, monkeypatch):
    urls = []

    def fetch(url, **kw):
        urls.append(url)
        return Response(url, 200, {}, b"[]")

    monkeypatch.setattr(sources, "fetch", fetch)
    hf = HuggingFace(("huggingface.co",), tmp_path)
    for repo, rev in [("a/b", "../x"), ("a/b", "main?x=/../y"), ("a/b", "v1..2"), ("a/b", "a" * 39 + "\n"),
                      ("a/b", ""), ("../a", "main"), ("a/b/c", "main"), ("a b/c", "main"), ("a/b\n", "main")]:
        with pytest.raises(ValueError):
            hf.tree(repo, rev)
    assert urls == []
    assert hf.tree("a/b", "main") == {} and hf.tree("a/b", "c" * 40) == {}
    assert hf.tree("a/b", "v1.0") == {}
    assert urls[-1].endswith("/api/models/a/b/tree/v1.0?recursive=true")
    assert sorted(p.name for p in (tmp_path / "hf-trees").iterdir()) == \
        sorted(["a__b@main.json", f"a__b@{'c' * 40}.json", "a__b@v1.0.json"])


def test_an_upper_case_revision_is_lower_cased(tmp_path):
    r = FakeRepo("a/b", make_repo(2))
    listing = r.listing()
    listing["revision"] = r.revision.upper()
    pf = FakePF([r])
    pf.overrides["a/b"] = listing
    c = build(r, pf=pf)
    assert isinstance(c, TorrentCand) and c.revision == r.revision


def test_a_rescued_repo_id_must_be_a_model_name(monkeypatch):
    r = FakeRepo("a/b", make_repo(2))
    pf = FakePF([r])
    pf.rescue = lambda: {"rescued": [{"repoId": "a b/c"}, {"repoId": "../../x"}, {"repoId": ["x"]}, "junk",
                                     {"repoId": "a/b"}]}
    asked = []
    real = pf.torrent
    pf.torrent = lambda repo, quant=None: asked.append(repo) or real(repo, quant)
    cands, rejected = gather(Config(), neutral_feed(), pf, FakeHF([r]), set())
    assert asked == ["a/b"] and [c.repo for c in cands] == ["a/b"]
    pf.rescue = lambda: ["not", "a", "dict"]
    assert gather(Config(), neutral_feed(), pf, FakeHF([r]), set())[0] == []
    assert isinstance(build_candidate("a b/c", None, neutral_feed(), None, pf, FakeHF([r]), Config()), Rejection)


def test_worth_rescuing_skips_a_revision_that_isnt_a_commit():
    from flotilla.worthrescuing import PageModel, sync

    class PF:
        def torrent(self, repo):
            return {"revision": "main?x=/../../y", "torrent": "/x"}

    class HF:
        def license(self, repo):
            return "mit"

        def tree(self, repo, rev):
            raise AssertionError("never asked")

    doc = {"models": []}
    s = sync(doc, [PageModel("org/m", historic=True)], PF(), HF(), {"mit"})
    assert doc["models"] == [] and "pins no full commit" in s["skipped"][0]


# ------------------------------------------------- 3. file paths that collide on disk


def _torrent(files: list[dict], name="lab__model", top_extra=None, info_extra=None) -> bytes:
    data = b"".join(b"x" * f["length"] for f in files)
    pl = 16384
    pieces = b"".join(hashlib.sha1(data[i:i + pl]).digest() for i in range(0, len(data), pl))
    info = {"name": name, "piece length": pl, "pieces": pieces, "files": files, **(info_extra or {})}
    return bencode({"info": info, **(top_extra or {})})


@pytest.mark.parametrize("paths", [
    (["model.safetensors"], ["model.safetensors"]),
    (["Model.safetensors"], ["model.SAFETENSORS"]),
    ([unicodedata.normalize("NFC", "é.json")], [unicodedata.normalize("NFD", "é.json")]),
    (["a"], ["a", "b"]),
    (["d", "a"], ["D", "a", "b"]),
])
def test_paths_that_would_be_one_file_on_disk_are_refused(paths):
    with pytest.raises(TorrentError):
        parse_torrent(_torrent([{"length": 20000, "path": list(p)} for p in paths]))


def test_duplicate_shard_with_other_bytes_is_rejected_end_to_end():
    """The audit's reproduction: libtorrent renames the second copy; we'd have hashed the first."""
    good, evil = b"G" * 20000, b"E" * 20000
    raw = make_torrent([("model.safetensors", good), ("model.safetensors", evil)], "lab__model")
    with pytest.raises(TorrentError):
        parse_torrent(raw)
    r = FakeRepo("lab/model", {"model.safetensors": good})
    pf = FakePF([r])
    pf.raw_overrides[r.infohash] = raw
    c = build(r, pf=pf)
    assert isinstance(c, Rejection) and ".torrent" in c.reason


def test_pad_files_may_share_a_name():
    raw = _torrent([{"length": 100, "path": ["a"]}, {"length": 16284, "path": [".pad", "16284"], "attr": b"p"},
                    {"length": 100, "path": ["b"]}, {"length": 16284, "path": [".pad", "16284"], "attr": b"p"},
                    {"length": 100, "path": ["c"]}])
    assert [f.repo_path for f in parse_torrent(raw).real_files()] == ["a", "b", "c"]


@pytest.mark.parametrize("entry", [
    {"length": 0, "path": ["b"], "attr": b"l", "symlink path": ["a"]},
    {"length": 0, "path": ["b"], "attr": b"l"},
    {"length": 0, "path": ["b"], "symlink path": ["..", "..", "etc"]},
])
def test_symlinks_are_refused(entry):
    with pytest.raises(TorrentError, match="symlink"):
        parse_torrent(_torrent([{"length": 100, "path": ["a"]}, entry]))


@pytest.mark.parametrize("files,info_extra", [
    ([{"length": 10, "path": ["real"], "path.utf-8": ["other"]}], None),      # libtorrent writes "other"
    ([{"length": 10, "path": ["x"]}], {"name.utf-8": "elsewhere"}),
    ([{"length": 10, "path": ["a\x01b"]}], None),                              # libtorrent writes a_b
    ([{"length": 10, "path": ["a\u202eb"]}], None),                            # libtorrent writes ab
    ([{"length": 10, "path": ["a\u200bb"]}], None),
    ([{"length": 10, "path": ["a" * 241]}], None),                             # libtorrent truncates
])
def test_names_libtorrent_would_write_elsewhere_are_refused(files, info_extra):
    with pytest.raises(TorrentError):
        parse_torrent(_torrent(files, info_extra=info_extra))


def test_matching_utf8_names_and_long_ones_up_to_the_limit_are_fine():
    raw = _torrent([{"length": 10, "path": ["a" * 240], "path.utf-8": ["a" * 240]}],
                   info_extra={"name.utf-8": "lab__model"})
    assert parse_torrent(raw).files[0].repo_path == "a" * 240


# ------------------------------------------------- 4. hostile shapes and bencode


GOOD = make_torrent([("a.json", b"x" * 100)], "n")
INFO = bdecode(GOOD)[b"info"]


def _with(info_changes=None, top_changes=None, file_changes=None) -> bytes:
    info = dict(INFO)
    if file_changes is not None:
        info[b"files"] = [{**info[b"files"][0], **file_changes}]
    info.update(info_changes or {})
    return bencode({b"info": info, **(top_changes or {})})


@pytest.mark.parametrize("raw", [
    b"d4:infoi1ee",
    b"d4:info4:abcde",
    b"le",
    _with({b"name": 5}),
    _with({b"name": [b"x"]}),
    _with({b"pieces": 5}),
    _with({b"pieces": [b"x" * 20]}),
    _with({b"files": 5}),
    _with({b"files": [5]}),
    _with({b"files": []}),
    _with({b"files": b"abc"}),
    _with(file_changes={b"attr": 5}),
    _with(file_changes={b"attr": [b"p"]}),
    _with(file_changes={b"path": b"a.json"}),
    _with(file_changes={b"path": [5]}),
    _with(file_changes={b"path": [[b"a"]]}),
    _with(file_changes={b"length": b"100"}),
    _with(file_changes={b"length": -1}),
    _with(top_changes={b"announce-list": 5}),
    _with(top_changes={b"announce-list": [5]}),
    _with(top_changes={b"announce-list": [[5]]}),
    _with(top_changes={b"announce-list": [[b"\xff\xfe"]]}),
    _with(top_changes={b"announce": 5}),
    _with(top_changes={b"url-list": 5}),
    _with(top_changes={b"url-list": [5]}),
    _with(top_changes={b"url-list": {b"a": b"b"}}),
])
def test_wrong_shapes_raise_only_torrent_error(raw):
    with pytest.raises(TorrentError):
        parse_torrent(raw)


@pytest.mark.parametrize("raw", [b"i 5e", b"i1_0e", b"i+5e", b"i-0e", b"i05e", b"i-05e", b"ie", b"i-e",
                                 b"i5 e", b"01:a", b"+1:a", b" 1:a", b"1_0:aaaaaaaaaa", b"i99999999999999999999e"])
def test_non_canonical_bencode_is_refused(raw):
    with pytest.raises(TorrentError):
        bdecode(raw)


def test_canonical_bencode_still_reads():
    assert bdecode(b"li0ei-5ei42e0:3:abce") == [0, -5, 42, b"", b"abc"]


def test_element_count_is_capped():
    bomb = b"d1:al" + b"le" * (MAX_ELEMENTS + 10) + b"ee"
    with pytest.raises(TorrentError, match="elements"):
        parse_torrent(bomb)
    bdecode(b"l" + b"le" * (MAX_ELEMENTS - 10) + b"e")   # under the cap: fine


def test_mutated_torrents_raise_only_torrent_error():
    raw = make_torrent(sorted(make_repo(3, 5000).items()), "lab__model")
    rng = random.Random(1)
    for _ in range(3000):
        b = bytearray(raw)
        for _ in range(rng.randint(1, 4)):
            op = rng.random()
            i = rng.randrange(len(b))
            if op < 0.5:
                b[i] = rng.choice(b"0123456789ilde:-x ")
            elif op < 0.75:
                del b[i:i + rng.randint(1, 8)]
            else:
                b[i:i] = bytes(rng.choice(b"0123456789ilde:") for _ in range(rng.randint(1, 4)))
        try:
            parse_torrent(bytes(b))
        except TorrentError:
            pass


def test_a_torrent_of_the_wrong_shape_is_a_rejection_not_a_crash():
    r = FakeRepo("a/b", make_repo(2))
    pf = FakePF([r])
    pf.raw_overrides[r.infohash] = b"d4:infoi1ee"
    c = build(r, pf=pf)
    assert isinstance(c, Rejection) and ".torrent rejected" in c.reason


# ------------------------------------------------- 5. magnet infohash


def test_magnet_infohash_must_be_exactly_hex_or_base32():
    ih = "ab" * 20
    for bad in (f"+{ih[:38]}+", f" {ih[:38]} ", f"{ih[:39]}\n", "ab" * 19 + "a ", "%20" + ih[:38]):
        with pytest.raises(MagnetError):
            parse_magnet(f"magnet:?xt=urn:btih:{bad}")
    assert parse_magnet(f"magnet:?xt=urn:btih:{ih.upper()}").btih == ih
    import base64
    b32 = base64.b32encode(bytes.fromhex(ih)).decode()
    assert parse_magnet(f"magnet:?xt=urn:btih:{b32}").btih == ih
    with pytest.raises(MagnetError):
        parse_magnet(f"magnet:?xt=urn:btih:{b32[:30]}+1")
    with pytest.raises(MagnetError):
        parse_magnet(5)


def test_malformed_tracker_and_web_seed_urls_are_magnet_errors():
    for extra in ("&tr=http%3A%2F%2F%5B%3A%3A1", "&tr=udp%3A%2F%2Ftracker.pirateface.co%3A99999%2Fa",
                  "&ws=https%3A%2F%2F%5Bpirateface.co", "&ws=https%3A%2F%2Fpirateface.co%3Axx%2F"):
        with pytest.raises(MagnetError):
            validate_magnet(parse_magnet("magnet:?xt=urn:btih:" + "ab" * 20 + extra))


def test_a_whitespace_infohash_in_a_listing_is_a_rejection():
    r = FakeRepo("a/b", make_repo(2))
    listing = r.listing()
    listing["magnet"] = r.magnet().replace(r.infohash, "+" + r.infohash[:38] + "+")
    listing["infohash"] = " " + r.infohash[:38] + " "
    pf = FakePF([r])
    pf.overrides["a/b"] = listing
    c = build(r, pf=pf)
    assert isinstance(c, Rejection) and "magnet rejected" in c.reason


# ------------------------------------------------- 6. other hostile Pirate Face replies


@pytest.mark.parametrize("change", [
    {"magnet": 5}, {"magnet": ["magnet:?"]}, {"infohash": 5}, {"revision": 5}, {"revision": ["x"]},
])
def test_listing_fields_of_the_wrong_type_are_rejections(change):
    r = FakeRepo("a/b", make_repo(2))
    pf = FakePF([r])
    pf.overrides["a/b"] = {**r.listing(), **change}
    assert isinstance(build(r, pf=pf), Rejection)


def test_listing_extras_of_the_wrong_type_are_ignored():
    r = FakeRepo("a/b", make_repo(2))
    pf = FakePF([r])
    pf.overrides["a/b"] = {**r.listing(), "seeders": "lots", "leechers": [1], "files": 5, "quant": {"x": 1}}
    c = build(r, pf=pf)
    assert isinstance(c, TorrentCand) and c.seeders == 0 and c.leechers == 0 and c.quant is None
    pf.overrides["a/b"] = {**r.listing(), "files": ["junk", {"path": 5}, {"path": "x", "sha256": 7}]}
    assert isinstance(build(r, pf=pf), TorrentCand)
    pf.overrides["a/b"] = ["not", "a", "listing"]
    assert isinstance(build(r, pf=pf), Rejection)


def test_pirate_face_answers_of_the_wrong_shape_are_fetch_errors(monkeypatch):
    pf = PirateFace(("pirateface.co",))
    for doc in ([], "x", 5, None):
        answer(monkeypatch, lambda url, doc=doc: doc)
        for call in (pf.licenses, pf.rescue, lambda: pf.torrent("a/b")):
            with pytest.raises(FetchError):
                call()
    answer(monkeypatch, lambda url: {"licenses": 5})
    assert pf.licenses() == []
    answer(monkeypatch, lambda url: {"licenses": [5, {"id": 3}, {"id": "mit"}, {"id": "x", "label": "X"}]})
    assert pf.licenses() == [{"id": "mit", "label": "mit"}, {"id": "x", "label": "X"}]


def test_new_torrents_feed_skips_malformed_sizes():
    rss = (b'<?xml version="1.0"?><rss><channel>'
           b'<item><title>a/b</title><guid>' + b"1" * 40 + b'</guid><description>1.2.3 GB, 1 seeders</description></item>'
           b'<item><title>c/d</title><guid>' + b"2" * 40 + b'</guid><description>. GB, 0 seeders</description></item>'
           b'<item><title>e/f</title><guid>' + b"3" * 40 + b'</guid><description>2.5 GB, 4 seeders</description></item>'
           b'<item><title>g/h\n</title><guid>' + b"4" * 40 + b'</guid></item>'
           b'</channel></rss>')
    assert parse_new_torrents(rss) == [{"repo": "e/f", "infohash": "3" * 40, "gb": 2.5, "seeders": 4},
                                       {"repo": "g/h", "infohash": "4" * 40, "gb": None, "seeders": None}]


def test_hf_tree_entries_of_the_wrong_shape_are_left_out(tmp_path, monkeypatch):
    page = [5, "x", {"type": "file", "path": 5, "size": 1}, {"type": "file", "path": "a", "size": "big"},
            {"type": "file", "path": "b", "size": 3, "lfs": 5}, {"type": "file", "path": "ok", "size": 3,
                                                                 "oid": "A" * 40}]
    monkeypatch.setattr(sources, "fetch", lambda url, **kw: Response(url, 200, {}, json.dumps(page).encode()))
    tree = HuggingFace(("huggingface.co",), tmp_path).tree("a/b", "c" * 40)
    assert list(tree) == ["ok"] and tree["ok"].git_oid == "a" * 40


def test_a_hostile_model_card_has_no_license():
    from flotilla.sources import license_from_card

    assert license_from_card({"cardData": [1], "tags": 5}) is None
    assert license_from_card({"cardData": "x", "tags": ["license:mit"]}) == "mit"


# ------------------------------------------------- 7. a feed in another encoding


def test_a_utf16_feed_cannot_hide_its_doctype():
    ents = "".join(f'<!ENTITY l{i} "{"&l%d;" % (i - 1) * 10}">' for i in range(1, 9))
    doc = (f'<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY l0 "aaaaaaaaaa">{ents}]>'
           f'<rss><channel><item><title>&l8;</title></item></channel></rss>')
    for body in (doc.encode("utf-16"), doc.encode("utf-16-le"), doc.encode("utf-16-be"), doc.encode("utf-32")):
        with pytest.raises(FetchError):
            parse_new_torrents(body)
    # A DOCTYPE far into the document, behind a long comment, is still found.
    late = ('<?xml version="1.0"?><!--' + "x" * 8000 + '--><!DOCTYPE rss SYSTEM "http://x/y.dtd">'
            '<rss><channel></channel></rss>').encode()
    with pytest.raises(FetchError):
        parse_new_torrents(late)


def test_a_utf8_feed_declaring_another_encoding_is_read_as_utf8():
    rss = ('<?xml version="1.0" encoding="ISO-8859-1"?><rss><channel><item><title>a/b</title><guid>'
           + "1" * 40 + "</guid><description>é 1 GB, 2 seeders</description></item></channel></rss>").encode()
    assert parse_new_torrents(rss) == [{"repo": "a/b", "infohash": "1" * 40, "gb": 1.0, "seeders": 2}]
    assert parse_new_torrents(b"\xef\xbb\xbf" + rss)[0]["repo"] == "a/b"   # a UTF-8 byte-order mark


# ------------------------------------------------- 8. Pirate Face's manifest hashes


def test_manifest_hashes_are_lower_cased_and_checked():
    r = FakeRepo("a/b", make_repo(2), hf_backed=False)
    listing = r.listing()
    for f in listing["files"]:
        f["sha256"] = f["sha256"].upper()
    listing["files"].append({"path": "x", "sha256": "Z" * 64})
    pf = FakePF([r])
    pf.overrides["a/b"] = listing
    c = build(r, pf=pf, hf=FakeHF([r], gone={"a/b"}))
    assert isinstance(c, TorrentCand) and c.verify_source == "pirateface-only"
    shas = [f.sha256 for f in c.files if f.sha256]
    assert shas and all(s == s.lower() for s in shas)
    # HF lists a file with no hash: Pirate Face's (lower-cased) is used.
    r2 = FakeRepo("c/d", make_repo(2))
    listing = r2.listing()
    for f in listing["files"]:
        f["sha256"] = f["sha256"].upper()
    pf = FakePF([r2])
    pf.overrides["c/d"] = listing
    hf = FakeHF([r2])
    hf.tree_overrides["c/d"] = {p: sources.HFFile(v.size, None, None) for p, v in r2.tree().items()}
    c = build(r2, pf=pf, hf=hf)
    assert isinstance(c, TorrentCand)
    assert all(f.sha256 == f.sha256.lower() for f in c.files if f.sha256)
