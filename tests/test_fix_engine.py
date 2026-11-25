"""Regressions for the engine's failure handling: disk and I/O errors, state that only grew, limits."""

import errno
import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest

lt = pytest.importorskip("libtorrent")

from flotilla import cli
from flotilla import engine as engine_mod
from flotilla.candidates import Rejection, TorrentCand, build_candidate
from flotilla.config import Config, State
from flotilla.engine import RATE_MAX, Engine, _rate_limit, session_settings, unblock
from flotilla.feed import neutral_feed
from flotilla.perfile import PerFile
from flotilla.planner import Params, plan

from .fakes import FakeHF, FakePF, FakeRepo, make_repo


def make_engine(tmp_path, logs=None, **cfgkw) -> Engine:
    cfg = Config()
    cfg.folder, cfg.listen_port, cfg.whole_model_under_gb, cfg.reserve_gb = tmp_path / "seeds", 0, 0, 0
    for k, v in cfgkw.items():
        setattr(cfg, k, v)
    return Engine(cfg, State(tmp_path / "state"), log=(logs.append if logs is not None else lambda *_: None),
                  listen_host="127.0.0.1", dht=False)


def cand(repo: FakeRepo) -> TorrentCand:
    c = build_candidate(repo.repo, None, neutral_feed(), None, FakePF([repo]), FakeHF([repo]), Config())
    assert isinstance(c, TorrentCand), c
    return c


def hold(eng: Engine, repo: FakeRepo, status: str = "verified", write: bool = True) -> TorrentCand:
    """A model this node holds: its files on disk, its .torrent stored, a holding in the state."""
    c = cand(repo)
    save = eng.save_path(repo.repo, repo.infohash)
    if write:
        repo.write(save)
    eng.torrent_dir.mkdir(parents=True, exist_ok=True)
    (eng.torrent_dir / f"{repo.infohash}.torrent").write_bytes(repo.raw)
    eng.state.holdings[repo.infohash] = {
        "repo": repo.repo, "quant": c.quant, "revision": repo.revision, "save_path": str(save),
        "files": {str(f.index): {"path": f.repo_path, "tpath": c.meta.files[f.index].path, "size": f.size,
                                 "sha256": f.sha256, "git_oid": f.git_oid, "status": status, "since": 1000.0,
                                 "prio": 4} for f in c.files if f.kind != "skip"}}
    return c


def prios_of(eng: Engine, c: TorrentCand) -> list[int]:
    files = eng.state.holdings[c.infohash]["files"]
    return [4 if str(i) in files else 0 for i in range(len(c.meta.files))]


def drive(eng: Engine, until, timeout=30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        eng.ses.wait_for_alert(100)
        eng.tick()
        if until():
            return True
    return False


def wait_for(cond, timeout=5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ 1. I/O errors while verifying


def test_a_read_error_while_verifying_never_blocks_or_deletes(tmp_path, monkeypatch):
    """A flaky disk (EIO), a permission (EACCES) or too many open files (EMFILE) while hashing a file
    blocklisted the torrent after two quick retries and deleted the whole model, verified shards and
    all. Only a hash mismatch says the torrent is wrong: a read error leaves the file unverified and
    looks again later, backing off."""
    logs: list[str] = []
    eng = make_engine(tmp_path, logs)
    repo = FakeRepo("org/model", make_repo(n_shards=2, shard_size=20_000))
    c = hold(eng, repo)
    files = eng.state.holdings[c.infohash]["files"]
    shard = next(k for k, e in files.items() if e["sha256"])
    files[shard]["status"] = "verifying"
    later: list[tuple] = []
    monkeypatch.setattr(eng, "_verify_later", lambda ih, idx, a: later.append((ih, idx, a)) or 30.0)
    real = engine_mod.sha256_file

    def broken(path):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(engine_mod, "sha256_file", broken)
    for attempt in range(6):                         # well past the old two retries
        eng._verify_one(c.infohash, int(shard), attempt)
    eng._handle_failures()
    assert c.infohash in eng.state.holdings and c.infohash not in eng.state.data["blocklist"]
    assert not eng.failed
    e = eng.state.holdings[c.infohash]["files"][shard]
    assert e["status"] == "verifying" and "Input/output error" in e["io_error"]
    assert [a for _, _, a in later] == [1, 2, 3, 4, 5, 6]          # always looked at again
    assert any("couldn't read it" in m for m in logs)
    assert (Path(eng.state.holdings[c.infohash]["save_path"]) / e["tpath"]).exists()

    monkeypatch.setattr(engine_mod, "sha256_file", real)          # the disk is back
    eng._verify_one(c.infohash, int(shard), 7)
    e = eng.state.holdings[c.infohash]["files"][shard]
    assert e["status"] == "verified" and "io_error" not in e
    eng.shutdown()


def test_backoff_for_unreadable_files_grows_and_caps(tmp_path, monkeypatch):
    eng = make_engine(tmp_path)
    delays = []

    class Timer:
        def __init__(self, delay, fn, args):
            delays.append(delay)
            self.daemon = False

        def start(self):
            assert self.daemon                     # an hour's wait mustn't hold the process up at exit

    monkeypatch.setattr(engine_mod.threading, "Timer", Timer)
    for attempt in range(1, 12):
        eng._verify_later("a" * 40, 0, attempt)
    assert delays[:2] == [2.0, 4.0]                                      # the quick looks first
    assert delays[2:5] == [30.0, 60.0, 120.0] and delays[-1] == 3600.0
    monkeypatch.undo()
    eng.shutdown()


def test_a_hash_mismatch_still_blocks_and_records_what_for(tmp_path, monkeypatch):
    eng = make_engine(tmp_path)
    repo = FakeRepo("org/model", make_repo(n_shards=2, shard_size=20_000))
    c = hold(eng, repo)
    files = eng.state.holdings[c.infohash]["files"]
    shard = next(k for k, e in files.items() if e["sha256"])
    files[shard].update(status="verifying", sha256="0" * 64)
    eng._verify_one(c.infohash, int(shard), engine_mod.VERIFY_QUICK)
    eng._handle_failures()
    assert c.infohash in eng.state.data["blocklist"] and c.infohash not in eng.state.holdings
    assert eng.state.data["blocked"][c.infohash]["repo"] == "org/model"
    assert "SHA-256" in eng.state.data["blocked"][c.infohash]["why"]
    eng.shutdown()


def test_the_verifier_thread_survives_a_surprise(tmp_path, monkeypatch):
    """An exception in the verifier ended its thread silently: every later file stayed "verifying"
    for good, and `run --once` never finished."""
    logs: list[str] = []
    eng = make_engine(tmp_path, logs)
    seen, later = [], []
    monkeypatch.setattr(eng, "_verify_later", lambda ih, idx, a: later.append(a) or 30.0)

    def one(ih, idx, attempt):
        seen.append(idx)
        if idx == 1:
            raise KeyError("size")

    monkeypatch.setattr(eng, "_verify_one", one)
    eng.verify_q.put(("a" * 40, 1, 0))
    eng.verify_q.put(("a" * 40, 2, 0))
    assert wait_for(lambda: seen == [1, 2]), seen
    assert later and any("verify failed" in m for m in logs)
    eng.shutdown()


# ------------------------------------------------------------------ 2. unblocking


def test_unblock_by_infohash_by_model_or_all():
    data = {"blocklist": ["a" * 40, "b" * 40, "c" * 40],
            "blocked": {"a" * 40: {"repo": "Org/One"}, "b" * 40: {"repo": "org/one"}, "c" * 40: {"repo": "x/y"}}}
    assert unblock(data, "org/one") == ["a" * 40, "b" * 40]
    assert data["blocklist"] == ["c" * 40] and list(data["blocked"]) == ["c" * 40]
    assert unblock(data, "d" * 40) == []
    assert unblock(data, ("C" * 40)) == ["c" * 40] and data["blocklist"] == []
    data = {"blocklist": ["e" * 40, "f" * 40]}           # blocked before the reason was kept
    assert unblock(data, None) == ["e" * 40, "f" * 40] and data["blocklist"] == []


def test_flotilla_unblock_edits_the_state_when_no_node_runs(tmp_path, capsys):
    state = State(tmp_path / "state")
    state.data["blocklist"] = ["a" * 40, "b" * 40]
    state.data["blocked"] = {"a" * 40: {"repo": "org/m", "at": 1.0, "why": "SHA-256 mismatch"}}
    state.save()
    base = ["--state", str(tmp_path / "state"), "unblock"]
    assert cli.main(base + ["org/m"]) == 0
    assert "a" * 40 in capsys.readouterr().out
    assert State(tmp_path / "state").data["blocklist"] == ["b" * 40]
    assert cli.main(base + ["org/m"]) == 1                    # nothing left to match
    assert cli.main(base) == 2                                # say what, or --all
    assert cli.main(base + ["--all"]) == 0
    assert State(tmp_path / "state").data["blocklist"] == []


def test_flotilla_unblock_asks_a_running_node(tmp_path, capsys):
    """While the node runs it owns state.json (it saves its own copy over any other change), so the
    CLI asks it through the inbox the out-of-process dashboard uses."""
    from flotilla.remote import take_requests

    logs: list[str] = []
    eng = make_engine(tmp_path, logs)
    eng.state.data["blocklist"] = ["a" * 40]
    eng.state.data["blocked"] = {"a" * 40: {"repo": "org/m"}}
    eng.write_status()
    done = threading.Event()

    def serve():
        while not done.is_set():
            take_requests(eng.state.dir, eng.submit)
            time.sleep(0.05)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        assert cli.main(["--state", str(eng.state.dir), "unblock", "nope/none"]) == 1
        assert cli.main(["--state", str(eng.state.dir), "unblock", "org/m"]) == 0
    finally:
        done.set()
        t.join(5)
    assert "unblocked 1" in capsys.readouterr().out.lower()
    eng.process_commands()                       # the main loop's part
    assert eng.state.data["blocklist"] == [] and not eng.state.data["blocked"]
    assert json.loads((eng.state.dir / "state.json").read_text())["blocklist"] == []
    assert any("unblocked 1 torrent" in m for m in logs)
    eng.shutdown()


# ------------------------------------------------------------------ 3. the main loop carries on


def test_a_full_disk_doesnt_end_the_main_loop(tmp_path, monkeypatch):
    """ENOSPC from state.json, status.json or resume data killed the process without a shutdown:
    no final resume data, no state, the history left open. Now each failing step is logged (once
    per STEP_LOG_EVERY, not every 10 s) and the loop goes on, and shutdown always runs."""
    monkeypatch.setattr(engine_mod, "STATUS_EVERY", 0.05)
    monkeypatch.setattr(engine_mod, "STATUS_FILE_EVERY", 0.05)
    monkeypatch.setattr(engine_mod, "SAVE_EVERY", 0.0)
    logs: list[str] = []
    eng = make_engine(tmp_path, logs)
    eng.refresh = lambda: None
    calls = {"save": 0}

    def full(*a, **k):
        calls["save"] += 1
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(eng.state, "save", full)
    monkeypatch.setattr(eng, "write_status", full)
    eng.run(until=time.time() + 1.0)
    assert calls["save"] > 4                                     # tried again and again
    assert sum("saving state failed" in m for m in logs) == 1, logs
    assert sum("writing status.json failed" in m for m in logs) == 1
    assert any(m.startswith("this run:") for m in logs)          # shutdown ran
    with pytest.raises(sqlite3.ProgrammingError):
        eng.history.db.execute("select 1")                       # and closed the history
    eng.shutdown()                                               # a second call does nothing


def test_run_shuts_down_however_it_ends(tmp_path):
    eng = make_engine(tmp_path)
    eng.refresh = lambda: None

    def boom():
        raise RuntimeError("surprise")

    eng.resume_holdings = boom
    with pytest.raises(RuntimeError):
        eng.run(once=True)
    assert eng._shut and eng.stop.is_set()
    assert (eng.state.dir / "state.json").exists()


def test_one_alert_that_fails_doesnt_lose_the_rest(tmp_path):
    logs: list[str] = []
    eng = make_engine(tmp_path, logs)
    seen = []

    def handle(a):
        seen.append(a)
        if a == 1:
            raise OSError(errno.ENOSPC, "No space left on device")

    eng._handle_alert = handle
    eng._handle_alerts([1, 2, 3])
    assert seen == [1, 2, 3] and any("No space left" in m for m in logs)
    eng.shutdown()


def test_a_log_that_cant_be_written_is_not_fatal(tmp_path):
    def broken(msg):
        raise OSError(errno.ENOSPC, "No space left on device")

    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    eng = Engine(cfg, State(tmp_path / "state"), log=broken, listen_host="127.0.0.1", dht=False)
    eng.log("hello")
    assert eng.log_ring[-1][1] == "hello"
    eng.shutdown()


# ------------------------------------------------------------------ 4. a short .torrent in the state dir


def test_a_truncated_torrent_file_is_written_again_and_one_bad_pick_doesnt_stop_the_plan(tmp_path):
    """A .torrent cut short (ENOSPC, a crash) was never rewritten: _add failed on it at every refresh,
    the plan stopped at that pick, the picks after it were never added and nothing was saved."""
    logs: list[str] = []
    eng = make_engine(tmp_path, logs, budget_gb=1.0)
    eng.budget_bytes = lambda: 10_000_000
    a = cand(FakeRepo("org/a", make_repo(n_shards=2, shard_size=20_000, seed=1)))
    b = cand(FakeRepo("org/b", make_repo(n_shards=2, shard_size=20_000, seed=2)))
    eng.torrent_dir.mkdir(parents=True, exist_ok=True)
    (eng.torrent_dir / f"{a.infohash}.torrent").write_bytes(a.meta.raw[:40])     # cut short
    real = eng._add

    def add(ih, prios, use_resume=True):
        if ih == b.infohash:
            raise RuntimeError("libtorrent refused it")
        return real(ih, prios, use_resume)

    eng._add = add
    eng.replan([a, b], [], probe_seconds=0)
    assert a.infohash in eng.handles, logs
    assert (eng.torrent_dir / f"{a.infohash}.torrent").read_bytes() == a.meta.raw
    assert b.infohash not in eng.state.holdings                  # not kept half-added
    assert any("couldn't apply the plan for org/b" in m for m in logs)
    saved = json.loads((eng.state.dir / "state.json").read_text())
    assert a.infohash in saved["holdings"]                       # and the plan was saved
    eng._add = real
    eng.replan([a, b], [], probe_seconds=0)                      # the next refresh brings it in
    assert b.infohash in eng.handles
    assert not list(eng.torrent_dir.glob("*.tmp"))
    eng.shutdown()


def test_a_plan_is_applied_even_when_state_json_cant_be_written(tmp_path, monkeypatch):
    logs: list[str] = []
    eng = make_engine(tmp_path, logs, budget_gb=1.0)
    eng.budget_bytes = lambda: 10_000_000
    a = cand(FakeRepo("org/a", make_repo(n_shards=2, shard_size=20_000, seed=1)))

    def full():
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(eng.state, "save", full)
    eng.replan([a], [], probe_seconds=0)
    assert eng._plans_applied == 1 and a.infohash in eng.handles      # `run --once` can still finish
    assert eng._dirty                                                  # the main loop saves it later
    assert any("saving state failed" in m for m in logs)
    monkeypatch.undo()
    eng.shutdown()


# ------------------------------------------------------------------ 5. the budget


def test_holdings_kept_through_a_transient_rejection_count_against_the_budget():
    """A held model whose listing failed for now is kept, outside the plan. The plan filled the whole
    budget again on top of it: 400 kB kept plus 400 kB new against a 500 kB budget."""
    repo = FakeRepo("org/new", make_repo(n_shards=4, shard_size=100_000, seed=3))
    c = build_candidate(repo.repo, None, neutral_feed(), None, FakePF([repo]), FakeHF([repo]),
                        Config())
    base = dict(budget_bytes=500_000, upload_bps=1e6, download_bps=1e6, whole_model_under_bytes=0)
    free = plan([c], Params(**base), "node")
    assert free.total_bytes > 300_000
    p = plan([c], Params(**base, kept_bytes=400_000, kept_torrents=1), "node")
    assert p.total_bytes + 400_000 <= 500_000, p.total_bytes
    full = plan([c], Params(**base, kept_bytes=0, kept_torrents=16, max_torrents=16), "node")
    assert not full.picks                                        # no room for another torrent either


def test_the_engine_tells_the_plan_what_it_keeps(tmp_path):
    logs: list[str] = []
    eng = make_engine(tmp_path, logs)
    eng.budget_bytes = lambda: 700_000
    gone = "g" * 40
    eng.state.holdings[gone] = {"repo": "org/gone", "save_path": str(tmp_path / "seeds" / "gone"), "files": {
        "0": {"path": "a.safetensors", "tpath": "a", "size": 400_000, "status": "verified"}}}
    new = cand(FakeRepo("org/new", make_repo(n_shards=4, shard_size=100_000, seed=3)))
    rej = [Rejection("org/gone", "Pirate Face lookup failed: timeout", transient=True)]
    assert eng.params(cands=[new], rejected=rej).kept_bytes == 400_000
    assert eng.params(cands=[new], rejected=[]).kept_bytes == 0       # not kept: not counted
    eng._plan_and_apply([new], rej)
    assert gone in eng.state.holdings                                 # kept as is
    taken = sum(e["size"] for e in eng.state.holdings[new.infohash]["files"].values())
    assert 0 < taken and taken + 400_000 <= 700_000, taken           # was all 700 kB on top of the 400
    eng.shutdown()


def test_the_budget_counts_only_what_the_holdings_take(tmp_path, monkeypatch):
    """Everything in the folder counted as held: the user's own files, a dropped model's leftovers
    and a probe's scratch made the plan think it could use their space too."""
    import shutil
    from collections import namedtuple

    eng = make_engine(tmp_path, budget_gb=1000.0)
    du = namedtuple("du", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: du(10**12, 0, 10_000_000))
    repo = FakeRepo("org/held", make_repo(n_shards=1, shard_size=1_000_000, seed=4))
    hold(eng, repo)
    held = sum(os.stat(p).st_blocks * 512 for p in Path(eng.save_path(repo.repo, repo.infohash)).rglob("*")
               if p.is_file())
    assert eng.budget_bytes() == held + 10_000_000
    (eng.cfg.folder / "my-own-files").mkdir(parents=True)
    (eng.cfg.folder / "my-own-files" / "movie.mkv").write_bytes(os.urandom(2_000_000))
    (eng.cfg.folder / ".probe").mkdir()
    (eng.cfg.folder / ".probe" / "x").write_bytes(os.urandom(500_000))
    assert eng.budget_bytes() == held + 10_000_000          # (the free space they took is already gone)
    eng.shutdown()


# ------------------------------------------------------------------ 6. torrents stopped on an error


class ErrHandle:
    def __init__(self, upload_mode=False, paused=True, broken=False):
        self.calls = []
        self.st = type("S", (), {"upload_mode": upload_mode, "paused": paused})()
        self.broken = broken

    def clear_error(self):
        if self.broken:
            raise RuntimeError("invalid torrent handle used")
        self.calls.append("clear_error")

    def status(self, flags=None):
        return self.st

    def unset_flags(self, f):
        self.calls.append(("unset", int(f)))

    def resume(self):
        self.calls.append("resume")


def test_a_torrent_stopped_on_a_disk_error_is_started_again_with_backoff(tmp_path):
    """Torrents aren't auto-managed, so one libtorrent stopped on a disk error (a full or read-only
    disk) stayed stopped for good, its files unseeded, until a restart."""
    logs: list[str] = []
    eng = make_engine(tmp_path, logs)
    ih = "a" * 40
    eng.state.holdings[ih] = {"repo": "org/m", "files": {"0": {"status": "verified", "size": 1}}}
    h = eng.handles[ih] = ErrHandle(upload_mode=True)
    eng._torrent_failed(ih, "No space left on device", 1000.0)
    eng._torrent_failed(ih, "No space left on device", 1001.0)       # the same fault, said again
    assert sum("libtorrent stopped org/m" in m for m in logs) == 1
    eng._retry_errored(1030.0)
    assert h.calls == []                                             # not yet
    eng._retry_errored(1061.0)
    assert h.calls == ["clear_error", ("unset", int(lt.torrent_flags.upload_mode)), "resume"]
    assert "starting org/m again" in logs[-1]
    eng._retry_errored(1062.0)
    assert len(h.calls) == 3                                         # once per error
    eng._torrent_failed(ih, "No space left on device", 1100.0)       # failed again: twice the wait
    assert "in 2 min (try 2)" in logs[-1]
    eng._retry_errored(1200.0)
    assert len(h.calls) == 3
    eng._retry_errored(1221.0)
    assert len(h.calls) == 6
    eng._retry_errored(1221.0 + 3 * 3600)                            # fine for long: forgotten
    assert ih not in eng._errored
    eng.handles = {}
    eng.shutdown()


def test_a_stopped_download_waits_for_the_disk_guard(tmp_path):
    eng = make_engine(tmp_path)
    ih = "a" * 40
    eng.state.holdings[ih] = {"repo": "org/m", "files": {"0": {"status": "wanted", "size": 1}}}
    h = eng.handles[ih] = ErrHandle()
    eng._torrent_failed(ih, "No space left on device", 1000.0)
    eng.disk_paused = True
    eng._retry_errored(5000.0)
    assert h.calls == []                                             # still below the reserve
    eng.disk_paused = False
    eng._retry_errored(5001.0)
    assert h.calls[0] == "clear_error"
    eng.handles = {}                                                 # gone meanwhile: forgotten
    eng._retry_errored(5002.0)
    assert ih not in eng._errored
    eng.shutdown()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to read-only folders")
def test_a_download_stopped_by_a_read_only_folder_resumes_once_it_can_write(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "TORRENT_RETRY", (0.5, 1.0))
    repo = FakeRepo("org/ro", make_repo(n_shards=2, shard_size=40_000, seed=5), seeders=1)
    src = repo.write(tmp_path / "A")
    seed = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                       "enable_upnp": False, "enable_natpmp": False, "allow_multiple_connections_per_ip": True,
                       "peer_fingerprint": "-XX0100-"})
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(lt.bdecode(repo.raw))
    atp.save_path = str(src)
    atp.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
    sh = seed.add_torrent(atp)
    assert wait_for(lambda: sh.status().is_seeding, 10)

    logs: list[str] = []
    cfg = Config()
    cfg.folder, cfg.listen_port, cfg.whole_model_under_gb, cfg.reserve_gb = tmp_path / "seeds", 0, 0, 0
    cfg.budget_gb = 1.0
    eng = Engine(cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False,
                 connect=[("127.0.0.1", seed.listen_port())])
    eng.budget_bytes = lambda: 10_000_000
    c = cand(repo)
    save = eng.save_path(repo.repo, repo.infohash)
    save.mkdir(parents=True)
    os.chmod(save, stat.S_IRUSR | stat.S_IXUSR)                      # can't write: libtorrent stops it
    try:
        eng.replan([c], [], probe_seconds=0)
        assert drive(eng, lambda: repo.infohash in eng._errored, 20), logs
        os.chmod(save, 0o700)
        assert drive(eng, eng.all_done, 40), logs[-10:]
    finally:
        os.chmod(save, 0o700)
    assert any("starting org/ro again" in m for m in logs)
    eng.shutdown()


# ------------------------------------------------------------------ 7. adds while the disk guard is on


def test_a_seed_added_again_below_the_reserve_keeps_seeding(tmp_path):
    """guard_disk pauses only torrents still downloading; a torrent added again meanwhile (a trim,
    a memory reload) was paused whatever it was, finished seeds included."""
    eng = make_engine(tmp_path)
    eng.disk_paused = True
    done = hold(eng, FakeRepo("org/done", make_repo(n_shards=1, shard_size=20_000, seed=6)))
    busy = hold(eng, FakeRepo("org/busy", make_repo(n_shards=1, shard_size=20_000, seed=7)), status="wanted",
                write=False)
    hd = eng._add(done.infohash, prios_of(eng, done))
    hb = eng._add(busy.infohash, prios_of(eng, busy))
    assert wait_for(lambda: bool(hb.flags() & lt.torrent_flags.paused))
    time.sleep(0.3)
    assert not hd.flags() & lt.torrent_flags.paused
    eng.shutdown()


# ------------------------------------------------------------------ 8. _wait_gone and the rest of a batch


def test_waiting_for_a_deletion_handles_the_whole_batch(tmp_path, monkeypatch):
    eng = make_engine(tmp_path)
    ih = "a" * 40

    class Deleted:
        def __init__(self, ih):
            self.info_hashes = type("I", (), {"v1": ih})()

    batch = [object(), Deleted(ih), "file_completed for another torrent"]

    class Ses:
        def pop_alerts(self):
            return list(batch)

    handled = []
    monkeypatch.setattr(engine_mod.lt, "torrent_deleted_alert", Deleted)
    monkeypatch.setattr(eng, "_handle_alerts", lambda alerts: handled.extend(alerts))
    real, eng.ses = eng.ses, Ses()
    try:
        eng._wait_gone(ih, want_deleted=True, timeout=2)
    finally:
        eng.ses = real
    assert handled == batch
    monkeypatch.undo()
    eng.shutdown()


# ------------------------------------------------------------------ 9. _carry_over under the lock


def test_carrying_files_over_holds_the_lock(tmp_path):
    from .test_replacements import holding_engine, twin

    logs: list[str] = []
    files = make_repo(3, shard_size=40_000)
    files.pop("modeling_test.py")
    old = FakeRepo("a/b", files)
    eng = holding_engine(tmp_path, old, logs)
    owned = []
    for name in ("_take_out", "_remove"):
        real = getattr(eng, name)
        setattr(eng, name, lambda *a, _real=real, **k: owned.append(eng.lock._is_owned()) or _real(*a, **k))
    new = twin(old)
    c = cand(new)
    eng.mark_held([c])
    assert eng._carry_over([c])
    assert owned and all(owned), owned
    eng.shutdown()


# ------------------------------------------------------------------ 10. limits and numbers


def test_rate_limits_never_wrap_round():
    assert _rate_limit(2**31) == RATE_MAX                        # was 0: unlimited
    assert _rate_limit(4_294_968_296) == RATE_MAX                # was 1000 B/s
    assert _rate_limit(0) == 1 and _rate_limit(12_500_000) == 12_500_000
    assert _rate_limit(float("nan")) == RATE_MAX and _rate_limit(float("inf")) == RATE_MAX
    cfg = Config()
    cfg.upload_mbps, cfg.download_mbps, cfg.upload_share, cfg.download_share = 1e5, 1e5, 1.0, 1.0
    s = session_settings(cfg, port=0, fingerprint="-FL0100-")
    assert s["upload_rate_limit"] == s["download_rate_limit"] == RATE_MAX


def test_the_dashboard_takes_only_finite_numbers(tmp_path):
    eng = make_engine(tmp_path)
    for bad in (float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            eng.submit("allot", {"repo": "org/m", "gb": bad})
        with pytest.raises(ValueError):
            eng.submit("settings", {"budget_gb": bad})
        with pytest.raises(ValueError):
            eng.submit("add", {"ref": "org/m", "priority": bad})
    assert eng.submit("allot", {"repo": "org/m", "gb": 1}) == "saved"
    snap = eng.snapshot()
    json.loads(json.dumps(snap, allow_nan=False))                # nothing a browser's JSON refuses
    eng.shutdown()


# ------------------------------------------------------------------ 11. state that only grew


def test_per_file_unlisted_marks_go_with_the_files(tmp_path):
    eng = make_engine(tmp_path)
    pf: PerFile = eng.perfile
    root = "ab" * 32
    pf.unlisted[root] = time.time()
    pf.retry[root] = time.time() + 3600
    pf._check()
    assert root not in pf.unlisted and pf.info()["unlisted"] == 0
    eng.shutdown()


def test_removing_a_model_forgets_its_bookkeeping(tmp_path):
    eng = make_engine(tmp_path)
    ih = "a" * 40
    eng.state.holdings[ih] = {"repo": "org/m", "save_path": str(tmp_path / "seeds" / "m"), "files": {}}
    eng.state.data["ledger"][ih] = {"repo": "org/m", "seed_hours": 3.0, "gb_hours": 1.0, "points_est": 2.5}
    eng.state.data["ledger"]["b" * 40] = {"repo": "org/n", "seed_hours": 1.0, "gb_hours": 1.0, "points_est": 1.0}
    eng._v2_size[ih], eng._compacted[ih] = 10, (1.0, 2)
    eng._remove(ih, delete=True)
    assert ih not in eng._v2_size and ih not in eng._compacted
    assert ih not in eng.state.data["ledger"]
    assert eng.points_view([])["est"] == pytest.approx(3.5)   # its points stay in the total
    eng.shutdown()


# ------------------------------------------------------------------ 12. resume data is private


def test_resume_data_is_written_for_this_user_only(tmp_path):
    eng = make_engine(tmp_path)
    c = hold(eng, FakeRepo("org/m", make_repo(n_shards=1, shard_size=20_000, seed=8)))
    h = eng._add(c.infohash, prios_of(eng, c))
    assert eng._save_resume_now(c.infohash, h)
    rf = eng.resume_dir / f"{c.infohash}.fastresume"
    assert stat.S_IMODE(rf.stat().st_mode) == 0o600
    assert stat.S_IMODE(eng.resume_dir.stat().st_mode) == 0o700
    eng.shutdown()


def test_resume_data_from_older_versions_is_made_private(tmp_path):
    rd = tmp_path / "state" / "resume"
    rd.mkdir(parents=True)
    os.chmod(rd, 0o755)
    (rd / "x.fastresume").write_bytes(b"d4:infod")
    os.chmod(rd / "x.fastresume", 0o644)
    eng = make_engine(tmp_path)
    assert stat.S_IMODE(rd.stat().st_mode) == 0o700
    assert stat.S_IMODE((rd / "x.fastresume").stat().st_mode) == 0o600
    eng.shutdown()


def test_a_model_pirate_face_stops_listing_is_kept_for_a_grace_period(tmp_path):
    """One odd "no torrent listed" answer deleted a held model at once. It's now kept for
    DELIST_GRACE refreshes in a row, counted against the budget meanwhile, and the count starts
    over when the model is listed again."""
    logs: list[str] = []
    eng = make_engine(tmp_path, logs)
    eng.budget_bytes = lambda: 10_000_000
    repo = FakeRepo("org/quiet", make_repo(n_shards=2, shard_size=50_000, seed=7))
    c = hold(eng, repo)
    gone = [Rejection(repo.repo, "no magnet listed", delisted=True)]
    for n in range(1, engine_mod.DELIST_GRACE):
        assert eng.params(cands=[], rejected=gone).kept_torrents == 1
        eng._plan_and_apply([], gone)
        assert repo.infohash in eng.state.holdings, n
        assert eng.state.holdings[repo.infohash]["delisted"] == n
    eng._plan_and_apply([c], [])                                   # listed again: the count starts over
    assert "delisted" not in eng.state.holdings[repo.infohash]
    for _ in range(engine_mod.DELIST_GRACE):
        eng._plan_and_apply([], gone)
    assert repo.infohash not in eng.state.holdings                 # gone for good after the grace period
    assert any("refreshes before it's dropped" in line for line in logs)
    eng.shutdown()


def test_the_deny_list_still_drops_a_held_model_at_once(tmp_path):
    eng = make_engine(tmp_path)
    eng.budget_bytes = lambda: 10_000_000
    repo = FakeRepo("org/denied", make_repo(n_shards=1, shard_size=50_000, seed=8))
    hold(eng, repo)
    eng._plan_and_apply([], [Rejection(repo.repo, "on the feed's deny list")])
    assert repo.infohash not in eng.state.holdings
    eng.shutdown()


def test_the_disk_guard_pauses_the_magnet_index_download_too(tmp_path):
    eng = make_engine(tmp_path, reserve_gb=1.0)

    class H:
        paused = False

        def pause(self):
            self.paused = True

        def resume(self):
            self.paused = False

    h = H()
    eng.index.handles = lambda: [("index:" + "e" * 40, "e" * 40, h)]
    eng.status = lambda ih, handle, fresh=False: type("S", (), {"is_finished": False})()
    eng.guard_disk(free=0)
    assert h.paused and eng.disk_paused
    eng.guard_disk(free=10 * 2**30)
    assert not h.paused and not eng.disk_paused
    eng.shutdown()
