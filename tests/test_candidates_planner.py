"""Trust checks on candidates, and the shard planner."""

import hashlib
import math
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
    held.held, held.held_since, held.done = True, 1e9 - 3600, 0.5  # an hour ago, half here
    for f in c.files:
        if f.repo_path == "model-00002.safetensors":
            f.availability = 0
    p = plan([c], params(6), "node-a")  # room for one shard only
    names = {f.repo_path for f in p.picks[c.infohash].files}
    assert "model-00019.safetensors" in names  # forced within min_hold_days
    assert not p.drops


def held_shards(c, names, avail, since=1e9 - 30 * 86400):
    """Mark shards as held a month ago (past min_hold_days), with this many other holders."""
    for f in c.files:
        if f.repo_path in names:
            f.held, f.held_since, f.availability = True, since, avail


def shards(budget_gb, **kw):
    """Planner params that treat every model shard by shard, however small it is."""
    return params(budget_gb, whole_model_under_bytes=0, **kw)


def weights(p, c):
    return {f.repo_path for f in p.picks[c.infohash].files if f.kind == "weight"} if c.infohash in p.picks else set()


def test_between_reorientations_what_is_held_stays():
    c = cand("org/m", n_shards=20)
    for f in c.files:
        f.availability = 0                     # every other shard has no copy at all
    held_shards(c, {"model-00019.safetensors"}, avail=4)
    p = plan([c], shards(6, churn_bytes=0), "node-a")      # room for one shard only
    assert weights(p, c) == {"model-00019.safetensors"} and not p.drops and not p.swap_out
    # ...but the plan still says what it would choose between
    assert [u.label for u in p.lowest] == ["model-00019.safetensors"]
    assert p.top and all(u.availability == 0 and not u.held for u in p.top)


def test_reorienting_swaps_the_lowest_for_what_the_swarm_needs_most():
    c = cand("org/m", n_shards=20)
    for f in c.files:
        f.availability = 0
    held_shards(c, {"model-00019.safetensors"}, avail=4)
    p = plan([c], shards(6, churn_bytes=5 * GB), "node-a")
    assert [u.label for u in p.swap_out] == ["model-00019.safetensors"]
    assert len(p.swap_in) == 1 and p.swap_in[0].availability == 0
    assert weights(p, c) == {p.swap_in[0].label}
    assert [f.repo_path for _, f in p.drops] == ["model-00019.safetensors"]


def test_churn_size_limits_how_much_is_swapped():
    low = cand("org/low", n_shards=4)
    need = cand("org/need", n_shards=10)
    held_shards(low, {f.repo_path for f in low.files}, avail=4)
    for f in need.files:
        f.availability = 0
    budget = 20.1                                           # the four held shards fill it
    p = plan([low, need], shards(budget, churn_bytes=11 * GB), "node-a")
    assert len(p.swap_out) == 2 and len(p.swap_in) == 2     # 10 GB out of the 11 allowed; a third wouldn't fit
    assert sum(u.size for u in p.swap_out) <= 11 * GB and p.total_bytes <= budget * GB
    assert len(weights(p, low)) == 2 and len(weights(p, need)) == 2
    # a churn size smaller than one piece still makes the first swap, and only that one
    p = plan([low, need], shards(budget, churn_bytes=4 * GB), "node-a")
    assert len(p.swap_out) == 1 and len(p.swap_in) == 1
    # nothing to swap: nothing goes
    assert not plan([low, need], shards(budget, churn_bytes=0), "node-a").swap_out
    # no limit: everything worth swapping goes
    p = plan([low, need], shards(budget), "node-a")
    assert len(p.swap_out) == 4 and low.infohash not in p.picks


def test_the_lowest_priority_goes_out_first():
    a = cand("org/a", n_shards=2, priority=0.9)
    b = cand("org/b", n_shards=2, priority=0.2)
    need = cand("org/need", n_shards=10, priority=0.9)
    for c in (a, b):
        held_shards(c, {f.repo_path for f in c.files}, avail=2)
    for f in need.files:
        f.availability = 0
    p = plan([a, b, need], shards(20.1, churn_bytes=10 * GB), "node-a")
    assert {u.cand.repo for u in p.swap_out} == {"org/b"} and len(p.swap_out) == 2
    assert len(weights(p, a)) == 2


def test_models_you_chose_are_never_swapped_out():
    mine = cand("org/mine", n_shards=4, priority=0.1)
    need = cand("org/need", n_shards=10, priority=0.9)
    held_shards(mine, {f.repo_path for f in mine.files}, avail=4)
    for f in need.files:
        f.availability = 0
    p = plan([mine, need], shards(20.1, manual=frozenset({"org/mine"})), "node-a")   # no churn limit at all
    assert not p.swap_out and not p.drops and not p.lowest and len(weights(p, mine)) == 4
    # set aside space for it instead: the same
    p = plan([mine, need], shards(20.1, allot={"org/mine": 21 * GB}, manual=frozenset({"org/mine"})), "node-a")
    assert not p.drops and len(weights(p, mine)) == 4


def test_a_model_you_add_comes_in_without_waiting_for_a_reorientation():
    low = cand("org/low", n_shards=4, priority=0.2)
    mine = cand("org/mine", n_shards=2, priority=1.0)
    held_shards(low, {f.repo_path for f in low.files}, avail=4)
    for f in mine.files:
        f.availability = 1
    p = plan([low, mine], shards(20.1, churn_bytes=0), "node-a")
    assert mine.infohash not in p.picks and not p.drops                # just another model: it waits
    p = plan([low, mine], shards(20.1, churn_bytes=0, manual=frozenset({"org/mine"})), "node-a")
    assert len(weights(p, mine)) == 2 and len(weights(p, low)) == 2    # yours: room is made now
    assert {u.cand.repo for u in p.swap_out} == {"org/low"}


def _spare_fleet(n_nodes, others, since):
    """Each node holds one shard that `others` other peers also hold, and is full; another shard has no copy."""
    out = []
    for i in range(n_nodes):
        c = cand("org/m", n_shards=2)
        held = c.files[-1]
        held.held, held.held_since, held.done, held.availability = True, since, 1.0, others
        for f in c.files[:-1]:
            f.availability = 0
        p = plan([c], shards(5.5, churn_bytes=5 * GB), f"node-{i}")
        out.append(held.repo_path in weights(p, c))
    return out


def test_holders_of_a_spare_copy_never_all_drop_it():
    # 40 holders, target 5: each keeps its copy with chance 5/40, so about 5 keep it, not none
    kept = _spare_fleet(40, others=39, since=1e9 - 30 * 86400)
    assert 1 <= sum(kept) <= 12
    # below its target every holder keeps it: nothing better to swap for
    c = cand("org/m", n_shards=2)
    held_shards(c, {c.files[-1].repo_path}, avail=4)
    for f in c.files[:-1]:
        f.availability = 4
    assert c.files[-1].repo_path in weights(plan([c], shards(5.5, churn_bytes=5 * GB), "node-0"), c)


def test_a_spare_copy_is_not_held_for_min_hold_days():
    # picked an hour ago (everyone piled on at once): the spare copies can go at the first reorientation
    kept = _spare_fleet(40, others=39, since=1e9 - 3600)
    assert 1 <= sum(kept) <= 12
    # one the swarm still needs is held as before
    c = cand("org/m", n_shards=2)
    held = c.files[-1]
    held.held, held.held_since, held.done, held.availability = True, 1e9 - 3600, 1.0, 2
    for f in c.files[:-1]:
        f.availability = 0
    assert held.repo_path in weights(plan([c], shards(5.5, churn_bytes=5 * GB), "node-0"), c)


def test_a_file_bigger_than_the_churn_size_can_still_come_in():
    low = cand("org/low", n_shards=6, shard=5 * GB)
    big = cand("org/big", n_shards=2, shard=30 * GB)
    held_shards(low, {f.repo_path for f in low.files}, avail=4)
    for f in big.files:
        f.availability = 0
    p = plan([low, big], shards(30.1, churn_bytes=5 * GB), "node-a")     # full, and 5 GB a time
    assert len(weights(p, big)) == 1 and not weights(p, low)            # the first swap may go past it
    assert p.total_bytes <= 30.1 * GB


def test_a_swap_has_to_be_clearly_better():
    c = cand("org/m", n_shards=4)
    for f in c.files:
        f.availability = 1
    held_shards(c, {"model-00000.safetensors"}, avail=1.2)   # a little better covered than the rest
    p = plan([c], shards(6), "node-a")
    assert not p.swap_out and weights(p, c) == {"model-00000.safetensors"}     # not worth a re-download
    p = plan([c], shards(6, hold_bonus=1.0), "node-a")
    assert [u.label for u in p.swap_out] == ["model-00000.safetensors"]


def test_less_space_drops_the_least_valuable_first():
    c = cand("org/m", n_shards=4)
    held_shards(c, {f.repo_path for f in c.files}, avail=1)
    held_shards(c, {"model-00002.safetensors"}, avail=8)
    p = plan([c], shards(15.1, churn_bytes=0), "node-a")     # was 20 GB; one shard has to go
    assert [f.repo_path for _, f in p.drops] == ["model-00002.safetensors"] and not p.swap_out


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


def test_reachable_nodes_lean_toward_the_last_copies():
    from flotilla.planner import REACHABLE_RARE, build_units

    c = cand("org/m", n_shards=40)
    for i, f in enumerate(c.files):
        f.availability = 0 if i % 2 else 3
    rates = {u.files[0].repo_path: u.rate for u in build_units([c], params(11), "n")}
    reach = {u.files[0].repo_path: u.rate for u in build_units([c], params(11, reachable=True), "n")}
    rare = next(f.repo_path for f in c.files if f.kind == "weight" and f.availability == 0)
    common = next(f.repo_path for f in c.files if f.kind == "weight" and f.availability == 3)
    assert reach[rare] == pytest.approx(rates[rare] * REACHABLE_RARE)
    assert reach[common] == rates[common]

    def rare_picks(reachable):
        n = 0
        for node in range(60):
            p = plan([c], params(5.1 * 10, reachable=reachable), f"node-{node}")
            n += sum(1 for f in p.picks[c.infohash].files if f.kind == "weight" and f.availability == 0)
        return n

    assert rare_picks(True) > rare_picks(False)


def test_demand_counts_real_downloaders_and_falls_back_on_the_tracker():
    from flotilla.planner import demand

    c = cand("org/m", n_shards=4, seeders=0)
    c.leechers, c.downloaders = 40, None
    assert demand(c) == pytest.approx(min(1.0, math.log2(21) / math.log2(33)))   # tracker leechers, halved
    c.downloaders = 0                       # the probe looked: those leechers were Flotilla nodes
    assert demand(c) == 0
    c.downloaders = 32
    assert demand(c) == 1.0
    c.seeders = 32                          # already well served: half the pull
    assert demand(c) == pytest.approx(0.5)


def _busy_and_rare():
    busy = cand("org/busy", n_shards=4, seeders=3)
    busy.downloaders = 60
    for f in busy.files:
        f.availability = 3
    rare = cand("org/rare", n_shards=4, seeders=0)
    rare.downloaders = 0
    for f in rare.files:
        f.availability = 0
    return busy, rare


@pytest.mark.parametrize("focus,want", [("at-risk", "org/rare"), ("in-demand", "org/busy")])
def test_focus_picks_rare_or_busy(focus, want):
    busy, rare = _busy_and_rare()
    p = plan([busy, rare], params(21, spread=10, focus=focus), "node-a")
    assert [c.repo for c in (busy, rare) if c.infohash in p.picks] == [want]


def test_balanced_sits_between():
    from flotilla.planner import build_units

    busy, rare = _busy_and_rare()

    def ratio(focus):
        rates = {u.cand.repo: u.rate for u in build_units([busy, rare], params(21, focus=focus), "n")}
        return rates["org/busy"] / rates["org/rare"]

    assert ratio("at-risk") < ratio("balanced") < ratio("in-demand")


def test_focus_is_checked_in_the_config(tmp_path):
    from flotilla.config import load_config, render_config

    path = tmp_path / "config.toml"
    path.write_text(render_config(cfg(focus="balanced")))
    assert load_config(path).focus == "balanced"
    path.write_text(render_config(cfg()).replace('focus = "at-risk"', 'focus = "popular"'))
    with pytest.raises(ValueError, match="policy.focus"):
        load_config(path)


def _young_download(done, stuck=False):
    """One shard picked an hour ago, `done` of it here, and a much needier shard not held."""
    c = cand("org/m", n_shards=4)
    for f in c.files:
        f.availability = 3
    held = next(f for f in c.files if f.repo_path == "model-00003.safetensors")
    held.held, held.held_since, held.done = True, 1e9 - 3600, done
    if stuck:
        held.availability = 0                  # rare as can be, and nobody can send it
    need = next(f for f in c.files if f.repo_path == "model-00001.safetensors")
    need.availability = 0 if not stuck else 2
    return c, held


def test_the_hold_starts_once_a_file_is_coming_in():
    for done, kept in [(0.0, False), (0.05, False), (0.1, True), (1.0, True)]:
        c, held = _young_download(done)
        p = plan([c], shards(6, hold_bonus=1.0, churn_bytes=None), "node-a")   # room for one shard
        names = weights(p, c)
        assert (held.repo_path in names) is kept, (done, names)


def test_a_download_nobody_can_send_holds_no_space():
    c, held = _young_download(0.0, stuck=True)
    # Not stuck: the held shard is the rarest there is, so it stays even at a reorientation.
    p = plan([c], shards(6, churn_bytes=None), "node-a")
    assert held.repo_path in weights(p, c)
    # Stuck (no peer sends it, Hugging Face won't): worth nothing, so it's dropped even between
    # reorientations (nothing has arrived to lose), and its space goes to a shard that can come.
    p = plan([c], shards(6, churn_bytes=0, stuck=frozenset({c.infohash})), "node-a")
    names = weights(p, c)
    assert held.repo_path not in names and len(names) == 1
    assert [f.repo_path for _, f in p.drops] == [held.repo_path]
    # ...and it isn't picked again while the torrent is still stuck.
    held.held, held.held_since = False, None
    assert held.repo_path not in weights(p := plan([c], shards(6, stuck=frozenset({c.infohash})), "node-a"), c)
    # A file of a stuck torrent that's already here keeps its worth.
    held.done = 1.0
    p = plan([c], shards(6, churn_bytes=None, stuck=frozenset({c.infohash})), "node-a")
    assert held.repo_path in weights(p, c)


def test_demand_is_counted_per_file_where_the_probe_split_it():
    from flotilla.planner import demand

    # One torrent, many quantizations: three people fetching the Q4 file, nobody the others.
    c = cand("org/gguf", n_shards=6, seeders=0)
    c.downloaders = 3
    for f in c.files:
        f.availability = 0
        f.downloaders = 3 if f.repo_path == "model-00002.safetensors" else 0
    q4 = next(f for f in c.files if f.repo_path == "model-00002.safetensors")
    other = next(f for f in c.files if f.repo_path == "model-00004.safetensors")
    assert demand(c, q4) == demand(c) > 0 and demand(c, other) == 0
    q4.availability = 3                                 # three peers hold all of Q4: half the pull
    assert demand(c, q4) == pytest.approx(demand(c) / 2)
    other.downloaders = None                            # not split (an old probe): the torrent's count
    assert demand(c, other) == demand(c)

    q4.availability, other.downloaders = 0, 0
    p = plan([c], shards(6, focus="in-demand", spread=10), "node-a")      # room for one file
    assert weights(p, c) == {"model-00002.safetensors"}


def test_a_file_swapped_out_lately_isnt_picked_again():
    c = cand("org/m", n_shards=4)
    for f in c.files:
        f.availability = 0
    p = plan([c], shards(6, spread=10), "node-a")              # room for one shard
    first = weights(p, c)
    assert first                                              # it picks something
    cooling = frozenset((c.infohash, path) for path in first)
    p = plan([c], shards(6, spread=10, cooling=cooling), "node-a")
    assert weights(p, c) and weights(p, c).isdisjoint(first)  # something else, not that one again
    held_shards(c, first, avail=0)                            # held files aren't affected by it
    p = plan([c], shards(6, spread=10, cooling=cooling), "node-a")
    assert weights(p, c) == first
