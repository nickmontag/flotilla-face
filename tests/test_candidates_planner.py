"""Trust checks on candidates, and the shard planner."""

import hashlib
from pathlib import Path

import pytest

from flotilla.candidates import Rejection, TorrentCand, build_candidate, classify
from flotilla.config import Config
from flotilla.feed import FeedModel, neutral_feed
from flotilla.planner import GB, Params, plan
from flotilla.sources import HFFile, lfs_pointer_oid
from flotilla.torrentmeta import make_torrent

from .fakes import FakeHF, FakePF, FakeRepo, make_repo


def cfg(**kw) -> Config:
    c = Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def build(repo: FakeRepo, pf=None, hf=None, entry=None, feed=None, **cfgkw):
    pf = pf or FakePF([repo])
    hf = hf or FakeHF([repo])
    return build_candidate(repo.repo, None, feed or neutral_feed(), entry, pf, hf, cfg(**cfgkw))


# ---------------------------------------------------------------- trust chain


def test_consistent_torrent_accepted():
    r = FakeRepo("good/model", make_repo(4))
    c = build(r)
    assert isinstance(c, TorrentCand), c
    kinds = {f.repo_path: f.kind for f in c.files}
    assert kinds["modeling_test.py"] == "skip"           # code is never auto-seeded
    assert kinds["config.json"] == "meta"
    assert kinds["model-00001-of-00004.safetensors"] == "weight"
    shard = next(f for f in c.files if f.repo_path.endswith(".safetensors"))
    assert shard.sha256 == hashlib.sha256(r.files[shard.repo_path]).hexdigest()
    assert next(f for f in c.files if f.repo_path == "LICENSE").git_oid


def test_extra_file_not_in_hf_repo_rejected():
    files = make_repo(2)
    r = FakeRepo("a/b", files)
    smuggled = FakeRepo("a/b", {**files, "payload.safetensors": b"\x00" * 100})
    pf = FakePF([r])
    pf.raw_overrides[r.infohash] = smuggled.raw
    # infohash of the smuggled torrent differs, so it must fail the infohash check first
    c = build(r, pf=pf)
    assert isinstance(c, Rejection) and "infohash" in c.reason
    # and even if the listing agrees with the smuggled torrent, HF's tree catches it
    pf2 = FakePF([smuggled])
    hf = FakeHF([smuggled])
    hf.tree_overrides["a/b"] = r.tree()
    c2 = build(smuggled, pf=pf2, hf=hf)
    assert isinstance(c2, Rejection) and "not in the HF repo" in c2.reason


def test_size_disagreement_rejected():
    r = FakeRepo("a/b", make_repo(2))
    hf = FakeHF([r])
    t = r.tree()
    p = "model-00001-of-00002.safetensors"
    t[p] = HFFile(t[p].size + 1, t[p].sha256, None)
    hf.tree_overrides["a/b"] = t
    assert "size" in build(r, hf=hf).reason


def _wrong_pf_manifest(r: FakeRepo, path: str) -> FakePF:
    pf = FakePF([r])
    listing = r.listing()
    listing["files"] = [f if f["path"] != path else {"path": path, "sha256": "0" * 64} for f in listing["files"]]
    pf.overrides[r.repo] = listing
    return pf


def test_pf_manifest_wrong_but_hf_self_consistent_prefers_hf():
    """Pirate Face's hash is wrong, Hugging Face agrees with itself."""
    r = FakeRepo("a/b", make_repo(2))
    p = "model-00001-of-00002.safetensors"
    hf = FakeHF([r])
    c = build(r, pf=_wrong_pf_manifest(r, p), hf=hf)
    assert isinstance(c, TorrentCand), c
    f = next(x for x in c.files if x.repo_path == p)
    assert f.sha256 == hashlib.sha256(r.files[p]).hexdigest()       # graded against HF, not PF
    assert f.kind == "weight"
    assert c.manifest_mismatches == [{"path": p, "pirateface": "0" * 64, "huggingface": f.sha256}]
    assert "disagreed" in c.verify_source
    assert (r.repo, "main") in hf.calls


def test_pf_manifest_wrong_and_hf_main_moved_is_refused():
    r = FakeRepo("a/b", make_repo(2))
    p = "model-00001-of-00002.safetensors"
    hf = FakeHF([r])
    moved = r.tree()
    moved[p] = HFFile(moved[p].size, "f" * 64, None)                  # main has a different upload now
    hf.main_overrides[r.repo] = moved
    c = build(r, pf=_wrong_pf_manifest(r, p), hf=hf)
    assert isinstance(c, Rejection) and "ambiguous" in c.reason

    hf2 = FakeHF([r])
    hf2.main_overrides[r.repo] = None                                  # main unreadable
    c2 = build(r, pf=_wrong_pf_manifest(r, p), hf=hf2)
    assert isinstance(c2, Rejection) and "ambiguous" in c2.reason


def test_agreeing_manifest_does_not_touch_main():
    r = FakeRepo("a/b", make_repo(2))
    hf = FakeHF([r])
    c = build(r, hf=hf)
    assert isinstance(c, TorrentCand) and not c.manifest_mismatches and c.verify_source == "huggingface"
    assert (r.repo, "main") not in hf.calls


def test_hf_lists_file_without_hash_falls_back_to_pf():
    r = FakeRepo("a/b", make_repo(2))
    p = "model-00002-of-00002.safetensors"
    hf = FakeHF([r])
    t = r.tree()
    t[p] = HFFile(t[p].size, None, None)
    hf.tree_overrides[r.repo] = t
    c = build(r, hf=hf)
    assert isinstance(c, TorrentCand)
    f = next(x for x in c.files if x.repo_path == p)
    assert f.sha256 == hashlib.sha256(r.files[p]).hexdigest() and f.kind == "weight"


def test_lfs_pointer_oid_matches_hugging_face():
    # A real gated file: Hugging Face lists only the pointer ID; this SHA-256 and size reproduce it.
    sha = "141e200c1950c59bc42ae28744f50db8f6ea4c66ec3312b55f1ccad2b60af858"
    assert lfs_pointer_oid(sha, 3969317448) == "3e0e59e98c36be26f9cafb30d338af335a7ada18"
    assert lfs_pointer_oid(sha.upper(), 3969317448) == "3e0e59e98c36be26f9cafb30d338af335a7ada18"


def _gated_tree(r: FakeRepo) -> dict:
    """What Hugging Face shows anonymous clients for a gated repo: LFS SHA-256s hidden, pointer IDs listed."""
    t = r.tree()
    for p, f in t.items():
        if f.sha256:
            t[p] = HFFile(f.size, None, None, lfs_pointer_oid(f.sha256, f.size))
    return t


def test_gated_repo_hidden_hash_checked_via_lfs_pointer():
    r = FakeRepo("a/b", make_repo(2))
    hf = FakeHF([r])
    hf.tree_overrides[r.repo] = _gated_tree(r)
    c = build(r, hf=hf)
    assert isinstance(c, TorrentCand), c
    assert not c.manifest_mismatches and c.verify_source == "huggingface"
    for f in c.files:
        if f.repo_path.endswith(".safetensors"):
            assert f.sha256 == hashlib.sha256(r.files[f.repo_path]).hexdigest() and f.kind == "weight"


def test_gated_repo_wrong_pf_hash_rejected():
    r = FakeRepo("a/b", make_repo(2))
    p = "model-00001-of-00002.safetensors"
    hf = FakeHF([r])
    hf.tree_overrides[r.repo] = _gated_tree(r)
    c = build(r, pf=_wrong_pf_manifest(r, p), hf=hf)
    assert isinstance(c, Rejection) and "doesn't match" in c.reason


def test_hf_tree_treats_masked_hash_as_hidden(tmp_path, monkeypatch):
    from flotilla import netsafe, sources

    sha = "141e200c1950c59bc42ae28744f50db8f6ea4c66ec3312b55f1ccad2b60af858"
    ptr = "3e0e59e98c36be26f9cafb30d338af335a7ada18"
    page = [{"type": "file", "path": "model.safetensors", "size": 3969317448, "oid": ptr, "lfs": {"oid": "*" * 64}},
            {"type": "file", "path": "config.json", "size": 10, "oid": "a" * 40}]
    import json as _json
    monkeypatch.setattr(sources, "fetch", lambda url, **kw: netsafe.Response(url, 200, {}, _json.dumps(page).encode()))
    rev = "b" * 40
    # A tree cached before this fix, holding the mask as if it were a hash, is refetched.
    stale = tmp_path / "hf-trees" / f"x__y@{rev}.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(_json.dumps({"model.safetensors": {"size": 3969317448, "sha256": "*" * 64, "git_oid": None}}))
    tree = sources.HuggingFace(("huggingface.co",), tmp_path).tree("x/y", rev)
    m = tree["model.safetensors"]
    assert m.sha256 is None and m.git_oid is None and m.pointer_oid == ptr
    assert lfs_pointer_oid(sha, m.size) == m.pointer_oid
    assert tree["config.json"].git_oid == "a" * 40
    again = sources.HuggingFace(("huggingface.co",), tmp_path).tree("x/y", rev)   # from the new cache
    assert again["model.safetensors"].pointer_oid == ptr


def test_signed_pin_mismatch_rejected():
    r = FakeRepo("a/b", make_repo(2))
    entry = FeedModel("a/b", {"capability": 90}, infohash="f" * 40)
    c = build(r, entry=entry)
    assert isinstance(c, Rejection) and "pin" in c.reason
    ok = build(r, entry=FeedModel("a/b", {"capability": 90}, infohash=r.infohash))
    assert isinstance(ok, TorrentCand) and ok.pinned


def test_foreign_tracker_inside_torrent_rejected():
    r = FakeRepo("a/b", make_repo(2))
    # Same info dict (same infohash) but an announce URL pointing elsewhere.
    from flotilla.torrentmeta import bencode

    evil_raw = b"d8:announce" + bencode("udp://evil.example:1/announce") + r.raw[1:]
    pf = FakePF([r])
    pf.raw_overrides[r.infohash] = evil_raw
    c = build(r, pf=pf)
    assert isinstance(c, Rejection) and "tracker" in c.reason


def test_license_policy_and_deny_list():
    r = FakeRepo("a/b", make_repo(2))
    assert "license" in build(r, licenses=["apache-2.0"]).reason
    feed = neutral_feed()
    feed.deny = {"a/b"}
    assert "deny" in build(r, feed=feed).reason


def test_hf_gone_falls_back_to_pirateface_hashes():
    r = FakeRepo("a/b", make_repo(2), hf_backed=False)
    c = build(r, hf=FakeHF([r], gone={"a/b"}))
    assert isinstance(c, TorrentCand)
    assert c.hf_backed is False and c.verify_source == "pirateface-only"
    assert all(f.sha256 for f in c.files if f.kind != "skip")


def test_classify_policy():
    c = cfg()
    assert classify("x.bin", 10, c)[0] == "skip"
    assert classify("x.bin", 10, cfg(allow_pickle=True))[0] == "weight"
    for name in ("model.h5", "tf_model.h5", "model.keras", "weights.npz", "arr.npy", "model.pt"):
        kind, why = classify(name, 10, cfg())
        assert kind == "skip" and "run code" in why, name
    assert classify("model.h5", 10, cfg(allow_pickle=True))[0] == "weight"
    assert classify("model.safetensors", 10, cfg())[0] == "weight"
    assert classify("run.sh", 10, c)[0] == "skip"
    assert classify("weird.xyz", 10, c)[0] == "skip"
    assert classify("tokenizer.json", 10, c)[0] == "meta"
    assert classify("NOTICE", 10, c)[0] == "meta"


# ---------------------------------------------------------------- planner


def cand(name, n_shards=10, shard=5 * GB, seeders=0, hf_backed=True, priority=0.5, piece=16 * 1024 * 1024):
    """A TorrentCand with fake (unverifiable) metadata, for planner-only tests."""
    files = {"config.json": b"{}", "LICENSE": b"MIT"}
    meta_files = sorted(files.items())
    # Build a tiny torrent just for structure, then patch sizes.
    raw = make_torrent(meta_files + [(f"model-{i:05d}.safetensors", b"x") for i in range(n_shards)], name.replace("/", "__"), 16384)
    from flotilla.torrentmeta import parse_torrent
    from flotilla.candidates import FileCand

    meta = parse_torrent(raw)
    off = 0
    meta.piece_length = piece
    for tf in meta.files:
        tf.size = shard if tf.repo_path.endswith(".safetensors") else 1000
        tf.offset = off
        off += tf.size
    meta.num_pieces = (off + piece - 1) // piece
    fcs = []
    for tf in meta.files:
        first, last = tf.piece_range(piece)
        kind = "weight" if tf.repo_path.endswith(".safetensors") else "meta"
        fcs.append(FileCand(tf.index, tf.repo_path, tf.size, kind, first, last, sha256="0" * 64))
    ih = hashlib.sha1(name.encode()).hexdigest()
    return TorrentCand(name, None, ih, "0" * 40, "", "mit", priority, 5, hf_backed, seeders, 0, meta, fcs)


def params(budget_gb, **kw):
    return Params(int(budget_gb * GB), upload_bps=2.5e6, download_bps=25e6, now=1e9, **kw)


def test_budget_respected_and_meta_bundled():
    c = cand("org/big", n_shards=40)
    p = plan([c], params(52), "node-a")
    pick = p.picks[c.infohash]
    assert p.total_bytes <= 52 * GB
    assert len([f for f in pick.files if f.kind == "weight"]) == 10
    assert {"config.json", "LICENSE"} <= {f.repo_path for f in pick.files}


def test_scarce_files_win():
    c = cand("org/m", n_shards=20)
    for f in c.files:
        f.availability = 0 if f.repo_path in ("model-00003.safetensors", "model-00011.safetensors") else 6
    p = plan([c], params(11), "node-a")
    chosen = {f.repo_path for f in p.picks[c.infohash].files if f.kind == "weight"}
    assert chosen == {"model-00003.safetensors", "model-00011.safetensors"}


def test_lost_upstream_beats_hf_backed():
    gone = cand("org/gone", n_shards=4, hf_backed=False)
    safe = cand("org/safe", n_shards=4, hf_backed=True)
    p = plan([safe, gone], params(21, spread=10), "node-a")
    assert gone.infohash in p.picks and safe.infohash not in p.picks


def test_priority_steers():
    hi = cand("org/hi", n_shards=4, priority=0.9)
    lo = cand("org/lo", n_shards=4, priority=0.1)
    p = plan([lo, hi], params(21, spread=10), "node-a")
    assert list(p.picks) == [hi.infohash]


def test_different_nodes_spread_across_shards():
    c = cand("org/huge", n_shards=200)
    picks = []
    for node in ("n1", "n2", "n3", "n4"):
        p = plan([c], params(5.1 * 20), node)
        picks.append({f.repo_path for f in p.picks[c.infohash].files if f.kind == "weight"})
    union = set().union(*picks)
    # 4 nodes × 20 shards each from 200 equal shards: expected union ≈ 69, never ~20
    assert len(union) >= 55
    # ...and each node's draw is stable across runs (no churn)
    again = plan([c], params(5.1 * 20), "n1")
    assert {f.repo_path for f in again.picks[c.infohash].files if f.kind == "weight"} == picks[0]


def test_whole_small_model():
    small = cand("org/small", n_shards=2, shard=3 * GB)
    p = plan([small], params(100, whole_model_under_bytes=25 * GB), "node-a")
    pick = p.picks[small.infohash]
    assert pick.whole and len(pick.files) == len(small.files)


def test_held_files_are_sticky_and_forced():
    c = cand("org/m", n_shards=20)
    for f in c.files:
        f.availability = 3
    held = [f for f in c.files if f.repo_path == "model-00019.safetensors"][0]
    held.held, held.held_since = True, 1e9 - 3600  # an hour ago
    for f in c.files:
        if f.repo_path == "model-00002.safetensors":
            f.availability = 0
    p = plan([c], params(6), "node-a")  # room for one shard only
    names = {f.repo_path for f in p.picks[c.infohash].files}
    assert "model-00019.safetensors" in names  # forced within min_hold_days
    assert not p.drops


def test_max_torrents():
    cs = [cand(f"org/m{i}", n_shards=2, shard=1 * GB) for i in range(10)]
    p = plan(cs, params(1000, max_torrents=3, whole_model_under_bytes=0), "node-a")
    assert len(p.picks) == 3


def test_allotment_reserves_space_for_a_lower_priority_model():
    hi = cand("org/hi", n_shards=10, priority=0.9)
    lo = cand("org/lo", n_shards=10, priority=0.1)
    # 31 GB: without an allotment, all of it goes to the high-priority model
    p = plan([hi, lo], params(31, spread=10), "node-a")
    assert lo.infohash not in p.picks
    # set aside 11 GB for the low one: it gets two 5 GB shards, the high one the other ~21 GB (four)
    p = plan([hi, lo], params(31, spread=10, allot={"org/lo": 11 * GB}), "node-a")
    lo_bytes = sum(f.size for f in p.picks[lo.infohash].files)
    hi_w = [f for f in p.picks[hi.infohash].files if f.kind == "weight"]
    assert 10 * GB <= lo_bytes <= 11 * GB and len(hi_w) == 4


def test_allotment_caps_a_model():
    big = cand("org/big", n_shards=40, priority=0.9)
    other = cand("org/other", n_shards=10, priority=0.2)
    p = plan([big, other], params(100, spread=10, allot={"org/big": 16 * GB}), "node-a")
    big_bytes = sum(f.size for f in p.picks[big.infohash].files)
    assert big_bytes <= 16 * GB and len([f for f in p.picks[big.infohash].files if f.kind == "weight"]) == 3
    assert other.infohash in p.picks          # the rest of the space went elsewhere


def test_ranking_sets_priority():
    import json

    from flotilla.feed import apply_local, neutral_feed

    path = Path(__import__("tempfile").mkdtemp()) / "my.json"
    path.write_text(json.dumps({"order": ["c/c", "a/a", "b/b"], "allot_gb": {"a/a": 12, "b/b": 0}}))
    f = apply_local(neutral_feed(), path)
    prio = {m.repo: f.priority(m) for m in f.models}
    assert prio == {"c/c": 1.0, "a/a": pytest.approx(0.65), "b/b": pytest.approx(0.3)}
    assert f.order == ["c/c", "a/a", "b/b"] and f.allot_gb == {"a/a": 12.0}
