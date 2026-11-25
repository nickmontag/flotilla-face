"""The catalog sweep: least-seeded models of Pirate Face's catalog, once the list is full."""

from __future__ import annotations

import hashlib
import threading
from types import SimpleNamespace

import pytest

from flotilla import sweep
from flotilla.candidates import gather
from flotilla.config import Config, load_config
from flotilla.engine import Engine
from flotilla.feed import FeedModel, neutral_feed
from flotilla.magnetindex import Entry
from flotilla.planner import list_full
from flotilla.torrentmeta import bencode

from .fakes import FakeHF, FakePF, FakeRepo, make_repo
from .test_candidates_planner import _listed, cand


def ih(i: int) -> str:
    return hashlib.sha1(str(i).encode()).hexdigest()


def index(n: int) -> dict[str, Entry]:
    return {f"org/m{i}": Entry("magnet:?", ih(i), "a" * 40, "2026-11-23") for i in range(n)}


def test_each_node_gets_its_own_slice_and_it_moves_weekly():
    entries = index(2000)
    a = [r for r, _ in sweep.slice_of(entries, "node-a", 100, n=50)]
    assert len(a) == 50 and a == [r for r, _ in sweep.slice_of(entries, "node-a", 100, n=50)]   # stable
    b = [r for r, _ in sweep.slice_of(entries, "node-b", 100, n=50)]
    later = [r for r, _ in sweep.slice_of(entries, "node-a", 101, n=50)]
    assert len(set(a) & set(b)) < 10 and len(set(a) & set(later)) < 10
    skipped = [r for r, _ in sweep.slice_of(entries, "node-a", 100, n=50, skip={a[0].lower()})]
    assert a[0] not in skipped


def scrape_reply(counts: dict[str, tuple[int, int]]) -> bytes:
    return bencode({b"files": {bytes.fromhex(h): {b"complete": c, b"incomplete": i, b"downloaded": 0}
                               for h, (c, i) in counts.items()}})


def test_scrape_asks_in_batches_and_unknown_hashes_have_no_seeders():
    hashes = [ih(i) for i in range(sweep.BATCH + 5)]
    asked = []

    def get(path):
        asked.append(path)
        return scrape_reply({hashes[0]: (3, 1), hashes[-1]: (1, 0)})

    got = sweep.scrape(hashes, get=get)
    assert len(asked) == 2 and asked[0].count("info_hash=") == sweep.BATCH and asked[1].count("info_hash=") == 5
    assert got[hashes[0]] == (3, 1) and got[hashes[-1]] == (1, 0) and got[hashes[1]] == (0, 0)
    with pytest.raises(OSError, match="isn't bencode"):
        sweep.scrape(hashes[:2], get=lambda path: b"<html>")
    with pytest.raises(OSError, match="no files"):
        sweep.scrape(hashes[:2], get=lambda path: bencode({b"failure reason": b"no"}))


def test_pick_takes_the_least_seeded_of_the_slice():
    entries = index(300)

    def counts(hashes):
        return {h: (int(h[0], 16) % 4, 0) for h in hashes}   # some with 0..3 seeders

    picks = sweep.pick(entries, "node-a", now=sweep.WEEK * 100, k=10, scrape_fn=counts, n=60)
    assert len(picks) == 10 and [p["seeders"] for p in picks] == sorted(p["seeders"] for p in picks)
    assert picks[0]["seeders"] == 0


def test_sweep_picks_are_gathered_last_unlisted_and_held_ones_keep_the_sweep_terms():
    repos = [FakeRepo(f"cat/m{i}", make_repo(1, 1000, seed=40 + i)) for i in range(3)]
    pf, hf = FakePF(repos), FakeHF(repos)
    feed = neutral_feed()
    feed.deny = {"cat/m2"}
    c = Config()
    c.include_unlisted = False
    cands, _ = gather(c, feed, pf, hf, {"cat/m1"}, log=lambda *_: None, sweep=["cat/m0", "cat/m2"],
                      swept={"cat/m1"})
    by = {x.repo: x for x in cands}
    assert set(by) == {"cat/m0", "cat/m1"}                     # the denied pick isn't looked at
    for x in by.values():
        assert x.priority == pytest.approx(sweep.PRIORITY) and x.target_replicas == sweep.TARGET and not x.listed


def test_the_list_is_full_only_when_spare_entries_have_their_copies_too():
    at = _listed("org/at", avail=5)
    spare = _listed("org/spare", avail=0, size=50 * 10**9)
    spare.spare = True
    assert list_full([at]) is True and list_full([at, spare]) is False
    for f in spare.files:
        f.availability = 5
    swept = cand("cat/m0", n_shards=2)                         # not listed: never counts
    assert list_full([at, spare, swept]) is True


def test_config_checks_the_sweep_settings(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[policy]\ncatalog_sweep = false\nsweep_candidates = 5\n")
    cfg = load_config(p)
    assert cfg.catalog_sweep is False and cfg.sweep_candidates == 5
    p.write_text("[policy]\nsweep_candidates = 9999\n")
    with pytest.raises(ValueError, match="sweep_candidates"):
        load_config(p)


def fake_engine(full=True, entries=None):
    logs = []
    return SimpleNamespace(
        cfg=Config(), state=SimpleNamespace(data={"list_full": full}, node_id="node-a"),
        index=SimpleNamespace(enabled=True, entries=entries if entries is not None else index(100)),
        feed=SimpleNamespace(models=[FeedModel("org/m1", {})]), lock=threading.RLock(), log=logs.append, logs=logs)


def test_the_engine_scrapes_at_most_daily_and_only_once_the_list_is_full(monkeypatch):
    calls = []

    def fake_pick(entries, node_id, now, k, skip):
        calls.append(skip)
        return [{"repo": "org/m7", "seeders": 0, "leechers": 0}]

    monkeypatch.setattr(sweep, "pick", fake_pick)
    eng = fake_engine(full=False)
    assert Engine._sweep_picks(eng, set()) == [] and not calls
    eng = fake_engine()
    assert Engine._sweep_picks(eng, {"org/m3"}) == ["org/m7"]
    assert calls[0] >= {"org/m1", "org/m3"}                     # the list and what's held are skipped
    assert Engine._sweep_picks(eng, set()) == ["org/m7"] and len(calls) == 1   # kept for the day
    assert eng.state.data["sweep"]["picked"] == ["org/m7"]

    def down(*a, **k):
        raise OSError("tracker down")

    eng.state.data["sweep"]["at"] = 0
    monkeypatch.setattr(sweep, "pick", down)
    assert Engine._sweep_picks(eng, set()) == ["org/m7"]          # yesterday's picks while it's down
    assert any("tracker" in line for line in eng.logs)


def test_models_per_node_default_high_and_can_be_lowered(tmp_path):
    assert Config().max_torrents == Config().max_models == 1000
    p = tmp_path / "config.toml"
    p.write_text("[network]\nupload_mbps = 10\nmax_models = 40\n")
    assert load_config(p).max_torrents == 40              # not tied to upload speed any more
    p.write_text("[network]\nmax_models = 0\n")
    with pytest.raises(ValueError, match="max_models"):
        load_config(p)
