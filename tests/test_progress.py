"""What a running refresh reports: the dashboard's setup card and `flotilla status`'s line."""

import json

import pytest

from flotilla import cli
from flotilla.candidates import Rejection, TorrentCand, gather
from flotilla.config import Config
from flotilla.feed import neutral_feed
from flotilla.progress import PLAN_GUESS, STEPS, RefreshProgress, skip_kind

from .fakes import FakeHF, FakePF, FakeRepo, make_repo


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_a_refresh_reports_each_step_with_times_and_counts():
    clock = Clock()
    p = RefreshProgress(clock)
    assert p.view() is None and not p.running
    p.begin(first=True, probe_seconds=90)
    p.step("list")
    clock.t += 2
    p.note("list", version=8, models=647, signed=True)
    p.step("index", enabled=True)
    clock.t += 30
    p.note("index", models=58754)
    p.step("lookup", done=0, total=0, usable=0, skipped=0)
    p.looked_up(0, 4)
    assert p.view()["eta"] is None                      # no rate yet
    clock.t += 10
    p.looked_up(1, 4, object())                         # a candidate
    p.looked_up(2, 4, Rejection("a/b", ".torrent download failed: https://x/t.torrent: HTTP 404", transient=True))
    v = p.view()
    assert v["first"] and v["step"] == "lookup" and (v["index"], v["of"]) == (3, len(STEPS))
    assert v["steps"]["list"] == {"took": 2, "done_step": True, "version": 8, "models": 647, "signed": True}
    assert v["steps"]["index"]["took"] == 30 and v["steps"]["index"]["models"] == 58754
    lk = v["steps"]["lookup"]
    assert (lk["done"], lk["total"], lk["usable"], lk["skipped"]) == (2, 4, 1, 1) and not lk["done_step"]
    assert lk["kinds"] == {"no .torrent on Pirate Face": 1}
    assert v["eta"] == pytest.approx(5 * 2 + 90 + PLAN_GUESS)   # 5 s a model, 2 to go, then the probe
    p.step("probe", swarms=3, seconds=90, ends_at=clock.t + 90)
    clock.t += 60
    v = p.view()
    assert v["steps"]["probe"]["left"] == 30 and v["eta"] == pytest.approx(30 + PLAN_GUESS)
    assert v["steps"]["lookup"]["done_step"] and "ends_at" not in v["steps"]["probe"]
    json.dumps(v)                                       # it goes into status.json and the snapshot
    p.step("plan", budget=50 * 10**9)
    assert p.view()["eta"] == pytest.approx(PLAN_GUESS)
    p.end()
    assert p.view() is None and not p.running


def test_a_step_outside_a_refresh_starts_one_that_isnt_a_first_run():
    p = RefreshProgress(Clock())
    p.looked_up(1, 2, object())                         # nothing running: ignored
    assert p.view() is None
    p.step("probe", swarms=1, seconds=5, ends_at=1005.0)
    assert p.view()["first"] is False and p.view()["step"] == "probe"


@pytest.mark.parametrize("reason, kind", [
    (".torrent download failed: https://pirateface.co/api/torrents/ab.torrent: HTTP 404", "no .torrent on Pirate Face"),
    (".torrent download failed: timed out", "couldn't fetch its .torrent"),
    ("no magnet listed (no torrent yet, or terms must be accepted first)", "not on Pirate Face yet"),
    ("license unknown: Hugging Face no longer has it, and its license was never read here", "license unknown"),
    ("license 'other' not in policy.licenses", "license not allowed"),
    ("on the feed's deny list", "on the deny list"),
    ("size of 'README.md' differs from the hash ledger", "doesn't match its hashes"),
    ("Pirate Face lookup failed: HTTP 503", "lookup failed"),
    ("something new", "other"),
])
def test_skip_reasons_are_grouped(reason, kind):
    assert skip_kind(reason) == kind


def test_gather_reports_each_lookup():
    repos = [FakeRepo(f"org/m{i}", make_repo(n_shards=1, shard_size=1000, seed=i)) for i in range(3)]
    feed = neutral_feed()
    from flotilla.feed import FeedModel

    feed.models = [FeedModel(r.repo, {"risk": 50}) for r in repos] + [FeedModel("org/missing", {"risk": 50})]
    calls = []
    cfg = Config()
    cfg.include_unlisted = False
    cands, rejected = gather(cfg, feed, FakePF(repos), FakeHF(repos), set(), log=lambda *_: None,
                             progress=lambda d, t, r: calls.append((d, t, type(r).__name__)))
    assert calls[0] == (0, 4, "NoneType") and [c[:2] for c in calls[1:]] == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert sum(1 for c in calls if c[2] == "TorrentCand") == len(cands) == 3
    assert sum(1 for c in calls if c[2] == "Rejection") == len(rejected) == 1


def test_the_engine_reports_a_first_refresh_then_clears_it(tmp_path, monkeypatch):
    pytest.importorskip("libtorrent")
    from flotilla import engine as engine_mod
    from flotilla.config import State
    from flotilla.engine import Engine

    cfg = Config()
    cfg.folder, cfg.listen_port, cfg.probe_seconds = tmp_path / "seeds", 0, 7
    eng = Engine(cfg, State(tmp_path / "state"), log=lambda *_: None, listen_host="127.0.0.1", dht=False)
    seen = {}
    monkeypatch.setattr(engine_mod, "load_feed", lambda *a, **k: neutral_feed())
    monkeypatch.setattr(engine_mod, "load_ledger", lambda *a, **k: None)
    monkeypatch.setattr(eng.index, "sync", lambda pf: None)

    def fake_gather(*a, progress=None, **k):
        seen["gather"] = eng.progress.view()
        progress(0, 2, None)
        progress(1, 2, Rejection("a/b", "no magnet listed"))
        progress(2, 2, Rejection("c/d", "no magnet listed"))
        seen["looked"] = eng.progress.view()
        return [], [Rejection("a/b", "no magnet listed"), Rejection("c/d", "no magnet listed")]

    def fake_probe(cands, cfg, *a, **k):
        seen["probe"] = eng.progress.view()
        return {}

    monkeypatch.setattr(engine_mod, "gather", fake_gather)
    monkeypatch.setattr(engine_mod, "probe", fake_probe)
    eng.refresh()                                        # on the main thread: plans and applies at once
    assert seen["gather"]["first"] and seen["gather"]["step"] == "lookup"
    assert seen["gather"]["steps"]["list"]["done_step"] and seen["gather"]["steps"]["index"]["done_step"]
    assert seen["looked"]["steps"]["lookup"]["kinds"] == {"not on Pirate Face yet": 2}
    assert seen["probe"]["step"] == "probe" and seen["probe"]["steps"]["probe"]["seconds"] == 7
    assert eng.progress.view() is None                  # applied: the refresh is over
    assert eng.status_doc()["refresh"] is None
    eng.shutdown()


def test_a_refresh_that_fails_clears_its_progress(tmp_path, monkeypatch):
    pytest.importorskip("libtorrent")
    from flotilla import engine as engine_mod
    from flotilla.config import State
    from flotilla.engine import Engine

    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    eng = Engine(cfg, State(tmp_path / "state"), log=lambda *_: None, listen_host="127.0.0.1", dht=False)

    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(engine_mod, "load_feed", boom)
    with pytest.raises(RuntimeError):
        eng.refresh()
    assert eng.progress.view() is None
    eng.shutdown()


def test_flotilla_status_says_what_a_refresh_is_doing():
    p = RefreshProgress(Clock())
    p.begin(first=True, probe_seconds=90)
    p.step("lookup", done=0, total=0, usable=0, skipped=0)
    p.looked_up(0, 312)
    assert cli.refresh_line(p.view()) == "setting up: looking up models 0/312, then 90 s watching the swarms"
    p.looked_up(1, 312, Rejection("a/b", "no magnet listed"))
    assert "(1 skipped)" in cli.refresh_line(p.view()) and "min left" in cli.refresh_line(p.view())
    p.step("probe", swarms=93, seconds=90, ends_at=1040.0)
    assert cli.refresh_line(p.view()).startswith("setting up: watching 93 swarm(s), 40 s left, then choosing")
    assert cli.refresh_line(None) == ""
    q = RefreshProgress(Clock())
    q.step("plan")
    assert cli.refresh_line(q.view()).startswith("refreshing: choosing what to carry")


def test_status_prints_the_refresh_line(tmp_path, capsys):
    import argparse
    from datetime import datetime, timezone

    p = RefreshProgress(Clock())
    p.begin(first=True, probe_seconds=90)
    p.step("index", enabled=True)
    doc = {"at": datetime.now(timezone.utc).isoformat(), "node": "n", "listen_port": 1, "torrents": [],
           "refresh": p.view()}
    (tmp_path / "status.json").write_text(json.dumps(doc))
    cli.cmd_status(argparse.Namespace(state=str(tmp_path)))
    assert "setting up: updating Pirate Face's magnet index" in capsys.readouterr().out


def test_a_candidate_counts_as_usable():
    p = RefreshProgress(Clock())
    p.begin(first=False, probe_seconds=1)
    p.step("lookup", done=0, total=0, usable=0, skipped=0)
    c = TorrentCand.__new__(TorrentCand)                # no reason attribute: a candidate
    p.looked_up(1, 1, c)
    assert p.view()["steps"]["lookup"]["usable"] == 1
