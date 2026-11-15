"""Engine behaviour that doesn't need a swarm."""

import pytest

pytest.importorskip("libtorrent")

from flotilla.config import Config, State
from flotilla.engine import Engine
from flotilla.planner import GB
from flotilla.torrentmeta import parse_torrent


class FakeStatus:
    def __init__(self, finished):
        self.is_finished = finished


class FakeHandle:
    def __init__(self, finished):
        self.finished, self.paused = finished, False

    def status(self):
        return FakeStatus(self.finished)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


def test_disk_guard_pauses_downloads_not_seeds(tmp_path):
    cfg = Config()
    cfg.folder, cfg.listen_port, cfg.reserve_gb = tmp_path / "seeds", 0, 20
    logs = []
    eng = Engine(cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False)
    downloading, seeding = FakeHandle(False), FakeHandle(True)
    eng.handles = {"a" * 40: downloading, "b" * 40: seeding}

    eng.guard_disk(free=int(25 * GB))
    assert not eng.disk_paused and not downloading.paused

    eng.guard_disk(free=int(19 * GB))
    assert eng.disk_paused and downloading.paused and not seeding.paused
    assert "paused 1 download" in logs[-1]

    eng.guard_disk(free=int(21 * GB))          # inside the 2 GB hysteresis band: stay paused
    assert downloading.paused

    eng.guard_disk(free=int(23 * GB))
    assert not eng.disk_paused and not downloading.paused
    eng.handles = {}
    eng.shutdown()


def test_bind_interface_pins_listen_and_outgoing():
    from flotilla.engine import session_settings

    cfg = Config()
    cfg.bind_interface, cfg.port_forwarding = "10.8.0.2", True
    s = session_settings(cfg, port=6881, fingerprint="-FL0100-")
    assert s["listen_interfaces"] == "10.8.0.2:6881"
    assert s["outgoing_interfaces"] == "10.8.0.2"
    assert not s["enable_upnp"] and not s["enable_natpmp"]
    assert not s["enable_lsd"]          # LSD multicasts on every interface, ignoring the pin
    probe = session_settings(cfg, port=0, fingerprint="-FP0100-", probe=True)
    assert probe["listen_interfaces"] == "10.8.0.2:0" and probe["outgoing_interfaces"] == "10.8.0.2"
    plain = session_settings(Config(), port=6881, fingerprint="-FL0100-")
    assert "outgoing_interfaces" not in plain and plain["listen_interfaces"].startswith("0.0.0.0:6881")


def test_timed_run_stops_and_reports(tmp_path):
    import time

    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    logs = []
    eng = Engine(cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False)
    eng.refresh = lambda: None          # no network in this test
    t0 = time.monotonic()
    eng.run(until=time.time() + 1.5)
    assert time.monotonic() - t0 < 10
    assert any("seeding time is up" in l for l in logs)
    assert any(l.startswith("this run:") and "uploaded 0.0 MB" in l for l in logs)


def test_history_keeps_sampling_while_refresh_blocks(tmp_path, monkeypatch):
    """refresh() (lookups, the swarm probe) holds up the main loop for minutes while libtorrent
    keeps seeding. Samples taken on the main loop charted that time as "not seeding"."""
    import threading
    import time

    import flotilla.engine as engine_mod

    monkeypatch.setattr(engine_mod, "SAMPLE_EVERY", 0.02)
    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    eng = Engine(cfg, State(tmp_path / "state"), log=lambda *_: None, listen_host="127.0.0.1", dht=False)
    calls, during = [], threading.Event()
    real_sample = eng.sample

    def sample(now, force=False):
        calls.append(force)
        real_sample(now, force)

    def slow_refresh():                 # a refresh that outlasts several sampler wake-ups
        n = len(calls)
        end = time.monotonic() + 5
        while time.monotonic() < end and len(calls) < n + 3:
            time.sleep(0.01)
        if len(calls) >= n + 3:
            during.set()

    eng.sample, eng.refresh = sample, slow_refresh
    eng.run(until=time.time() + 0.5)
    assert during.is_set()               # sampled while refresh() was still running
    assert calls[-1] is True             # shutdown took the last partial minute
    assert eng._sampler is None          # and the thread was joined, not left running


class PrioHandle:
    def __init__(self, prios, progress=None):
        self.prios = list(prios)
        self.progress = list(progress) if progress is not None else [0] * len(prios)

    def get_file_priorities(self):
        return list(self.prios)

    def file_progress(self):
        return list(self.progress)

    def prioritize_files(self, prios):
        self.prios = list(prios)


def test_memory_budget_pauses_and_resumes_downloads(tmp_path):
    cfg = Config()
    cfg.folder, cfg.listen_port, cfg.memory_gb = tmp_path / "seeds", 0, 2
    logs = []
    eng = Engine(cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False)
    A, B, C = "a" * 40, "b" * 40, "c" * 40
    for ih, repo, prio in ((A, "org/top", 0.9), (B, "org/mid", 0.6), (C, "org/low", 0.3)):
        eng.state.holdings[ih] = {"repo": repo, "files": {
            "0": {"status": "verified", "prio": 4, "size": 1},       # held: keeps seeding
            "1": {"status": "wanted", "prio": 7, "size": 1}}}         # still downloading
        eng.cand_info[ih] = {"repo": repo, "priority": prio}
    eng.handles = {ih: PrioHandle([4, 7]) for ih in (A, B, C)}
    GB = 1024 ** 3

    eng.govern_memory(1000, used=int(2.5 * GB))          # over: the least important stops downloading
    assert eng.handles[C].prios == [4, 0] and eng.handles[B].prios == [4, 7]
    eng.govern_memory(1010, used=int(2.4 * GB))          # still over: the next one
    assert eng.handles[B].prios == [4, 0] and eng.handles[A].prios == [4, 7]
    eng.govern_memory(1020, used=int(1.0 * GB))          # well under: the most important paused one resumes
    assert eng.handles[B].prios == [4, 7] and eng.handles[C].prios == [4, 0]
    eng.govern_memory(1030, used=int(1.0 * GB))          # but not twice within two minutes
    assert eng.handles[C].prios == [4, 0]
    eng.govern_memory(1200, used=int(1.0 * GB))
    assert eng.handles[C].prios == [4, 7]
    assert any("org/low starts no new files" in m for m in logs)
    assert not eng._parked
    eng.govern_memory(1300, used=None)                   # can't measure (macOS): nothing changes
    assert all(h.prios == [4, 7] for h in eng.handles.values())
    eng.handles = {}
    eng.shutdown()


def test_memory_budget_follows_a_container_limit(monkeypatch):
    from flotilla import config as c

    cfg = Config()
    cfg.memory_gb = 1.5
    assert c.memory_budget(cfg) == int(1.5 * 1024 ** 3)
    cfg.memory_gb = 0
    monkeypatch.setattr(c, "container_memory_limit", lambda: 3 * 1024 ** 3)
    assert c.memory_budget(cfg) <= int(0.7 * 3 * 1024 ** 3)
    monkeypatch.setattr(c, "container_memory_limit", lambda: None)
    assert c.memory_budget(cfg) >= 512 * 1024 ** 2


def _mask(bits, n):
    m = bytearray((n + 7) // 8)
    for i in bits:
        m[i >> 3] |= 0x80 >> (i & 7)
    return bytes(m)


def test_compact_trees_keeps_only_piece_layers():
    """Peers send piece layers for files we don't download, and libtorrent holds each in a
    full-size tree. Cut to the piece layer, the saved tree loads at 32 bytes a piece."""
    import libtorrent as lt

    from flotilla.engine import _compact_trees

    # 160 KiB files in 32 KiB pieces: 10 blocks, a 16-leaf tree of 31 nodes, pieces at nodes 7-11.
    size, plen = 5 * 32768, 32768
    node = [bytes([i]) * 32 for i in range(31)]
    layer = b"".join(node[7:12])
    kept = {b"hashes": b"w" * 64, b"mask": _mask([0, 1], 31)}                  # a wanted file's tree
    known = {b"hashes": b"".join(node[0:12]), b"mask": _mask(range(12), 31),    # root down to pieces
             b"verified": b"\x00\x00"}
    partial = {b"hashes": b"".join(node[7:10]), b"mask": _mask(range(7, 10), 31)}
    whole = {b"hashes": b"".join(node)}                                         # no mask: every node
    raw = lt.bencode({b"file-version": 2, b"file_priority": [4, 0, 0, 0],
                      b"trees": [dict(kept), dict(known), dict(partial), dict(whole)]})
    out = lt.bdecode(_compact_trees(raw, [4, 0, 0, 0], [size] * 4, plen))
    piece_only, empty = {b"hashes": layer, b"mask": _mask(range(7, 12), 12)}, {b"hashes": b""}
    assert out[b"trees"] == [kept, piece_only, empty, piece_only]
    assert out[b"file_priority"] == [4, 0, 0, 0]                                 # nothing else touched
    assert _compact_trees(raw, [4, 4, 4, 4], [size] * 4, plen) == raw
    old = lt.bencode({b"file-version": 1, b"trees": [dict(known)]})              # '0'/'1' masks: drop
    assert lt.bdecode(_compact_trees(old, [0], [size], plen))[b"trees"] == [empty]
    small = lt.bencode({b"file-version": 2, b"trees": [dict(known)]})            # one piece: no layer
    assert lt.bdecode(_compact_trees(small, [0], [plen], plen))[b"trees"] == [empty]
    # A bare {} would make libtorrent stop reading every tree after it.
    bare = lt.bencode({b"file-version": 2, b"trees": [{}, dict(kept)]})
    assert lt.bdecode(_compact_trees(bare, [0, 4], [size] * 2, plen))[b"trees"] == [empty, kept]
    plain = lt.bencode({b"file_priority": [0]})
    assert _compact_trees(plain, [0], [size], plen) == plain
    assert _compact_trees(b"not bencode", [0], [size], plen) == b"not bencode"


def test_piece_layer_trees_load_compact_and_survive_a_reload(tmp_path, monkeypatch):
    """Real libtorrent: resume data shaped like what it saves after peers sent piece layers for
    files we don't want (root, layer, layer padding) is cut to the layer, libtorrent accepts it
    (checked against each file's root hash) and keeps it compact through a reload."""
    import hashlib
    import os
    import time

    import libtorrent as lt

    src = tmp_path / "src" / "m"
    src.mkdir(parents=True)
    for name in ("a.bin", "b.bin", "c.bin"):
        (src / name).write_bytes(os.urandom(5 * 32768))           # 5 pieces of 32 KiB, 10 blocks
    fs = lt.file_storage()
    lt.add_files(fs, str(src))
    ct = lt.create_torrent(fs, 32768)
    lt.set_piece_hashes(ct, str(src.parent))
    full = ct.generate()
    layers = full[b"piece layers"]
    raw = lt.bencode({k: v for k, v in full.items() if k != b"piece layers"})   # like Pirate Face's
    idx = {f.path.rsplit("/", 1)[-1]: i for i, f in enumerate(parse_torrent(raw).files)}   # v2 sorts them
    ti = lt.torrent_info(lt.bdecode(raw))
    ih = str(ti.info_hashes().v1)
    tree = full[b"info"][b"file tree"]

    pad = hashlib.sha256(b"\0" * 64).digest()                    # a piece of two all-zero blocks
    received = {}
    for name in ("b.bin", "c.bin"):
        root = tree[name.encode()][b""][b"pieces root"]
        received[name] = {b"hashes": root + layers[root] + pad * 3,       # nodes 0, 7-11, 12-14
                          b"mask": _mask([0, *range(7, 15)], 31)}
    prios = [0] * len(idx)
    prios[idx["a.bin"]] = 4

    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    eng = Engine(cfg, State(tmp_path / "state"), log=lambda *_: None, listen_host="127.0.0.1", dht=False)
    save = tmp_path / "seeds" / "m"
    eng.state.holdings[ih] = {"repo": "org/m", "save_path": str(save), "files": {
        str(idx["a.bin"]): {"status": "wanted", "prio": 4, "size": 5 * 32768}}}
    for d in (eng.torrent_dir, eng.resume_dir):
        d.mkdir(parents=True, exist_ok=True)
    (eng.torrent_dir / f"{ih}.torrent").write_bytes(raw)
    atp = lt.add_torrent_params()
    atp.ti, atp.save_path, atp.info_hashes, atp.file_priorities = ti, str(save), ti.info_hashes(), prios
    rd = lt.bdecode(lt.write_resume_data_buf(atp))
    rd[b"trees"] = [received.get(n, {}) for n in sorted(idx, key=idx.get)]     # a.bin: a bare {}
    (eng.resume_dir / f"{ih}.fastresume").write_bytes(lt.bencode(rd))

    def saved_trees(h):
        h.save_resume_data(lt.torrent_handle.save_info_dict)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            for a in eng.ses.pop_alerts():
                if isinstance(a, lt.save_resume_data_alert):
                    return lt.bdecode(lt.write_resume_data_buf(a.params))[b"trees"]
            time.sleep(0.05)
        raise AssertionError("no resume data")

    def compact(trees):
        for name in ("b.bin", "c.bin"):
            t = trees[idx[name]]
            root = tree[name.encode()][b""][b"pieces root"]
            assert t[b"hashes"] == layers[root], name                 # kept: it matched the root
            assert t[b"mask"].rstrip(b"\x00") == _mask(range(7, 12), 12).rstrip(b"\x00"), name

    h = eng._add(ih, prios)
    compact(saved_trees(h))
    monkeypatch.setattr(Engine, "COMPACT_MIN", 0)
    eng._do_compact_trees()
    assert ih in eng._compacted and eng.handles[ih] is not h
    compact(saved_trees(eng.handles[ih]))
    eng.shutdown()


class LateHandle(PrioHandle):
    """libtorrent applies new priorities later: until then, reads return the old ones."""

    def __init__(self, prios, progress=None):
        super().__init__(prios, progress)
        self.pending = []

    def prioritize_files(self, prios):
        self.pending.append(list(prios))

    def settle(self):
        for p in self.pending:
            self.prios = p
        self.pending = []


def test_a_paused_download_stays_paused_through_a_replan(tmp_path):
    """On a live node, a re-plan set a paused model's priorities back to 4 and then re-paused it
    by reading them back, which still returned the old (paused) values. The pause was skipped
    and the download went on above the memory budget."""
    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    eng = Engine(cfg, State(tmp_path / "state"), log=lambda *_: None, listen_host="127.0.0.1", dht=False)
    A = "a" * 40
    eng.state.holdings[A] = {"repo": "org/a", "files": {
        "0": {"status": "verified", "prio": 4, "size": 1},
        "1": {"status": "wanted", "prio": 7, "size": 1}}}
    h = eng.handles[A] = LateHandle([4, 0, 0])          # paused earlier; file 2 isn't ours
    eng._parked.add(A)
    h.prioritize_files([4, 7, 0])                       # what a re-plan used to set first
    eng._set_downloading(A, False)
    h.settle()
    assert h.prios == [4, 0, 0]
    eng._set_downloading(A, True)
    h.settle()
    assert h.prios == [4, 7, 0]
    eng.handles = {}
    eng.shutdown()


def test_memory_budget_lets_started_files_finish(tmp_path):
    """libtorrent keeps a half-downloaded file's full hash tree (4-8 MB per GB) until the file is
    done, paused or not. So the budget holds back only files that haven't started: pausing the
    started ones saved nothing, memory never fell, and the paused models never resumed."""
    cfg = Config()
    cfg.folder, cfg.listen_port, cfg.memory_gb = tmp_path / "seeds", 0, 1
    logs = []
    eng = Engine(cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False)
    A, B = "a" * 40, "b" * 40
    eng.state.holdings[A] = {"repo": "org/a", "files": {
        "0": {"status": "verified", "prio": 4, "size": 10},
        "1": {"status": "wanted", "prio": 4, "size": 10},          # half downloaded
        "2": {"status": "wanted", "prio": 4, "size": 10}}}         # not started
    eng.state.holdings[B] = {"repo": "org/b", "files": {
        "0": {"status": "wanted", "prio": 4, "size": 10}}}         # under way, nothing new to start
    eng.cand_info[A] = {"repo": "org/a", "priority": 0.9}
    eng.cand_info[B] = {"repo": "org/b", "priority": 0.1}
    eng.handles[A] = PrioHandle([4, 4, 4], progress=[10, 5, 0])
    eng.handles[B] = PrioHandle([4], progress=[3])
    GB = 1024 ** 3

    # Over budget: B is less important, but holding it back would save nothing.
    eng.govern_memory(1000, used=2 * GB)
    assert eng._parked == {A}
    assert eng.handles[A].prios == [4, 4, 0]                   # file 1 finishes, file 2 waits
    assert eng.handles[B].prios == [4]
    assert "org/a starts no new files" in logs[-1]

    eng.govern_memory(1005, used=2 * GB)                       # still over, nothing else to hold back
    assert eng._parked == {A} and eng.handles[A].prios == [4, 4, 0]

    eng.handles[A].progress = [10, 10, 0]                      # file 1 done: memory falls
    eng.state.holdings[A]["files"]["1"]["status"] = "verified"
    eng.govern_memory(1200, used=int(0.5 * GB))
    assert not eng._parked and eng.handles[A].prios == [4, 4, 4]
    assert "org/a again" in logs[-1]
    eng.handles = {}
    eng.shutdown()


def test_saved_piece_priorities_dont_override_file_priorities(tmp_path):
    """A model paused for memory got piece priority 0 on its unfinished files, saved in its resume
    data. On load libtorrent applies those piece priorities over the file priorities we pass, and a
    later prioritize_files() with unchanged file priorities doesn't touch them: the shards stayed
    at 0 through every restart and re-plan, wanted but never downloading."""
    import time

    import libtorrent as lt

    src = tmp_path / "src" / "m"
    src.mkdir(parents=True)
    for name in ("a.bin", "b.bin"):
        (src / name).write_bytes(bytes(range(256)) * 128)            # 32 KiB: two pieces each
    fs = lt.file_storage()
    lt.add_files(fs, str(src))
    ct = lt.create_torrent(fs, 16384)
    lt.set_piece_hashes(ct, str(src.parent))
    raw = lt.bencode(ct.generate())
    ti = lt.torrent_info(lt.bdecode(raw))
    ih = str(ti.info_hashes().v1)
    files = parse_torrent(raw).files                                 # in the torrent's (sorted) order
    a, b = (next(i for i, f in enumerate(files) if f.path.endswith(n)) for n in ("a.bin", "b.bin"))

    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    eng = Engine(cfg, State(tmp_path / "state"), log=lambda *_: None, listen_host="127.0.0.1", dht=False)
    save = tmp_path / "seeds" / "m"
    eng.state.holdings[ih] = {"repo": "org/m", "save_path": str(save), "files": {
        str(a): {"status": "wanted", "prio": 4, "size": 32768},
        str(b): {"status": "wanted", "prio": 4, "size": 32768}}}
    eng.torrent_dir.mkdir(parents=True, exist_ok=True)
    eng.resume_dir.mkdir(parents=True, exist_ok=True)
    (eng.torrent_dir / f"{ih}.torrent").write_bytes(raw)
    atp = lt.add_torrent_params()
    atp.ti, atp.save_path, atp.info_hashes = ti, str(save), ti.info_hashes()
    stuck = [4] * ti.num_pieces()
    first_b = sum(f.size for f in files[:b]) // 16384
    stuck[first_b:first_b + 2] = [0, 0]                              # b.bin's pieces, from the old pause
    atp.file_priorities = [4] * ti.num_files()
    atp.piece_priorities = stuck
    (eng.resume_dir / f"{ih}.fastresume").write_bytes(lt.write_resume_data_buf(atp))

    h = eng._add(ih, [4] * ti.num_files())
    end = time.monotonic() + 10
    while time.monotonic() < end and not h.status().has_metadata:
        time.sleep(0.05)
    time.sleep(0.5)
    assert h.status().total_wanted == 65536                          # both files, not just a.bin
    eng.handles = {}
    eng.shutdown()


def test_main_loop_keeps_going_while_a_refresh_runs(tmp_path):
    """The memory budget's reloads run on the main loop. A refresh there (lookups, a 90-second
    swarm probe) held them up for minutes, and a node died at its memory cap meanwhile."""
    import threading
    import time

    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    eng = Engine(cfg, State(tmp_path / "state"), log=lambda *_: None, listen_host="127.0.0.1", dht=False)
    ran, during = threading.Event(), []
    eng._do_ping = lambda body: ran.set()

    def slow_refresh():                  # queues a command and waits for the main loop to run it
        eng.commands.put(("ping", {}))
        during.append(ran.wait(5))

    eng.refresh = slow_refresh
    eng.run(until=time.time() + 1.5)
    assert during == [True]
    eng.handles = {}


def test_replan_off_the_main_loop_hands_the_apply_back(tmp_path):
    import threading

    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    eng = Engine(cfg, State(tmp_path / "state"), log=lambda *_: None, listen_host="127.0.0.1", dht=False)
    out = []
    t = threading.Thread(target=lambda: out.append(eng.replan([], [], probe_seconds=0)))
    t.start()
    t.join(30)
    assert out == [None] and eng._plans_applied == 0          # nothing applied off the main loop
    eng.process_commands()
    assert eng._plans_applied == 1 and eng.last_plan is not None
    eng.shutdown()
