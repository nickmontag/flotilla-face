"""Per-file swarms (flotilla.perfile) and seeding credit (flotilla.pfaccount).

The swarm test runs on 127.0.0.1: a Flotilla node holding verified shards of a hybrid model joins
each shard's own swarm, and a plain libtorrent client holding Pirate Face's copy of that per-file
torrent (no piece layers) downloads the whole shard from it.
"""

import hashlib
import json
import random
import shutil
import time
from pathlib import Path

import pytest

lt = pytest.importorskip("libtorrent")

from flotilla import perfile, pfaccount, v2hash
from flotilla.config import Config, State, load_config, render_config
from flotilla.engine import Engine
from flotilla.netsafe import FetchError
from flotilla.perfile import PerFileError, build
from flotilla.torrentmeta import parse_torrent

PIECE = 32768
PF_TRACKERS = [[b"udp://tracker.pirateface.co:6969/announce"], [b"http://tracker.pirateface.co:6969/announce"]]
KEY = "pfct_" + "k" * 32
MINE = "http://tracker.pirateface.co:6970/s3cr3tt0ken/announce"


# ------------------------------------------------------------------ helpers


def write_files(d: Path, sizes: dict[str, int], seed=5) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    for name, size in sizes.items():
        (d / name).write_bytes(rng.randbytes(size))
    return d


def generate(path: Path, piece: int, v2_only=False) -> dict:
    """A torrent of a file or folder, piece layers and all (libtorrent's own hashing)."""
    fs = lt.file_storage()
    lt.add_files(fs, str(path))
    ct = lt.create_torrent(fs, piece, lt.create_torrent.v2_only) if v2_only else lt.create_torrent(fs, piece)
    lt.set_piece_hashes(ct, str(path.parent))
    return ct.generate()


def pirateface_copy(full: dict) -> bytes:
    """What Pirate Face serves: no piece layers, its trackers, a web seed."""
    d = {k: v for k, v in full.items() if k != b"piece layers"}
    d[b"announce"], d[b"announce-list"] = PF_TRACKERS[0][0], PF_TRACKERS
    d[b"url-list"] = [b"https://pirateface.co/api/ws/org/m/rev/"]
    return lt.bencode(d)


def per_file(tmp_path: Path, src: Path, piece: int) -> tuple[str, str, bytes, dict]:
    """Pirate Face's per-file torrent for one file (its name may differ from the model's):
    (pieces root, v2 infohash, Pirate Face's copy, the API's answer)."""
    d = tmp_path / f"pf-{src.name}-{piece}"
    d.mkdir()
    shutil.copy(src, d / ("renamed-" + src.name))
    full = generate(d / ("renamed-" + src.name), piece, v2_only=True)
    raw = pirateface_copy(full)
    ti = lt.torrent_info(lt.bdecode(raw))
    fs = ti.files()
    i = next(i for i in range(fs.num_files()) if not fs.file_flags(i) & fs.flag_pad_file)
    root, v2 = str(fs.root(i)), str(ti.info_hashes().v2)
    entry = {"root": root, "name": "renamed-" + src.name, "length": src.stat().st_size, "infohashV2": v2,
             "torrent": f"/api/files/{root}.torrent", "seeders": 0, "siblings": [{"infohash": "x"}]}
    return root, v2, raw, entry


class FakePF:
    def __init__(self):
        self.files: dict[str, tuple[dict, bytes]] = {}
        self.calls: list[str] = []
        self.error: FetchError | None = None
        self.tracker_lines = ["udp://tracker.pirateface.co:6969/announce", MINE]
        self.keys: list[str] = []

    def file_swarm(self, root):
        self.calls.append(root)
        if self.error:
            raise self.error
        hit = self.files.get(root)
        return dict(hit[0]) if hit else None

    def file_torrent(self, path):
        return next(raw for e, raw in self.files.values() if e["torrent"] == path)

    def trackers(self, key):
        self.keys.append(key)
        if self.error:
            raise self.error
        return list(self.tracker_lines)


def drive(eng, until, timeout=30.0, others=()):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        eng.ses.wait_for_alert(100)
        eng.tick()
        for s in others:
            s.pop_alerts()
        if until():
            return True
    return False


def held_model(tmp_path, sizes, logs, monkeypatch, piece=PIECE):
    """A Flotilla node holding a hybrid model like Pirate Face's (no piece layers), every file verified."""
    monkeypatch.setattr(perfile, "MIN_SIZE", 1)
    src = write_files(tmp_path / "src" / "m", sizes)
    raw = pirateface_copy(generate(src, piece))
    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    cfg.path = tmp_path / "cfg" / "config.toml"
    eng = Engine(cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False)
    meta = parse_torrent(raw)
    ih = meta.infohash_v1
    save = tmp_path / "seeds" / "m"
    shutil.copytree(src, save / "m")
    files = {str(f.index): {"path": f.repo_path, "tpath": f.path, "size": f.size, "status": "wanted", "prio": 4,
                            "sha256": hashlib.sha256((src / f.repo_path).read_bytes()).hexdigest()}
             for f in meta.real_files()}
    eng.state.holdings[ih] = {"repo": "org/m", "revision": "r", "save_path": str(save), "files": files}
    eng.torrent_dir.mkdir(parents=True, exist_ok=True)
    (eng.torrent_dir / f"{ih}.torrent").write_bytes(raw)
    prios = [4 if str(i) in files else 0 for i in range(len(meta.files))]
    eng._add(ih, prios)
    assert drive(eng, eng.all_done), logs
    assert all(e["status"] == "verified" for e in files.values()), files
    return eng, ih, src, meta


def downloader(tmp_path, raw, name="dl"):
    ses = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                      "enable_upnp": False, "enable_natpmp": False, "allow_multiple_connections_per_ip": True,
                      "peer_fingerprint": "-XX0100-"})
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(lt.bdecode(raw))
    atp.save_path = str(tmp_path / name)
    atp.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
    return ses, ses.add_torrent(atp)


# ------------------------------------------------------------------ hash trees


@pytest.mark.parametrize("size", [5 * PIECE + 1234, 3 * PIECE, PIECE, 20_000, 900])
def test_hashing_a_file_matches_libtorrents_piece_layer_and_root(tmp_path, size):
    f = write_files(tmp_path / "d", {"x.bin": size}) / "x.bin"
    full = generate(f, PIECE, v2_only=True)
    fs = lt.torrent_info(full).files()
    want_root = fs.root(0).to_bytes()
    root, layer = v2hash.hash_file(f, size, PIECE)
    assert root == want_root
    if size > PIECE:
        assert layer == full[b"piece layers"][want_root]
        assert v2hash.root_from_layer(layer, PIECE) == want_root
    else:
        assert layer == b""


def test_a_short_file_or_a_stop_is_an_error(tmp_path):
    f = write_files(tmp_path / "d", {"x.bin": 3 * PIECE}) / "x.bin"
    with pytest.raises(OSError, match="shorter"):
        v2hash.hash_file(f, 4 * PIECE, PIECE)
    import threading

    stop = threading.Event()
    stop.set()
    with pytest.raises(OSError, match="stopped"):
        v2hash.hash_file(f, 3 * PIECE, PIECE, stop)


# ------------------------------------------------------------------ checking Pirate Face's per-file torrent


def test_build_adds_the_piece_layer_keeps_allowlisted_trackers_and_drops_web_seeds(tmp_path):
    src = write_files(tmp_path / "d", {"s.bin": 5 * PIECE + 7}) / "s.bin"
    root, v2, raw, _ = per_file(tmp_path, src, PIECE)
    d = lt.bdecode(raw)
    d[b"announce-list"] = [[b"udp://evil.example:6969/announce"], *PF_TRACKERS]
    d[b"announce"] = b"udp://evil.example:6969/announce"
    raw = lt.bencode(d)
    out = lt.bdecode(build(raw, root, src.stat().st_size, v2, lambda plen: v2hash.hash_file(src, src.stat().st_size,
                                                                                            plen)[1]))
    assert out[b"announce-list"] == PF_TRACKERS and out[b"announce"] == PF_TRACKERS[0][0]
    assert b"url-list" not in out
    assert out[b"piece layers"][bytes.fromhex(root)] == v2hash.hash_file(src, src.stat().st_size, PIECE)[1]
    assert str(lt.torrent_info(out).info_hashes().v2) == v2


def test_build_refuses_what_isnt_exactly_this_file(tmp_path):
    srcs = write_files(tmp_path / "d", {"s.bin": 5 * PIECE + 7, "t.bin": 5 * PIECE + 7})
    src = srcs / "s.bin"
    size = src.stat().st_size
    root, v2, raw, _ = per_file(tmp_path, src, PIECE)
    layer = lambda plen: v2hash.hash_file(src, size, plen)[1]   # noqa: E731
    other_root, other_v2, other_raw, _ = per_file(tmp_path, srcs / "t.bin", PIECE)
    with pytest.raises(PerFileError, match="exactly this one file"):
        build(other_raw, root, size, other_v2, layer)
    with pytest.raises(PerFileError, match="infohash"):
        build(raw, root, size, other_v2, layer)
    with pytest.raises(PerFileError, match="exactly this one file"):
        build(raw, root, size + 1, v2, layer)
    with pytest.raises(PerFileError, match="hash up"):
        build(raw, root, size, v2, lambda plen: v2hash.hash_file(srcs / "t.bin", size, plen)[1])
    d = lt.bdecode(raw)
    d[b"announce-list"], d[b"announce"] = [[b"udp://evil.example:6969/announce"]], b"udp://evil.example:6969/announce"
    with pytest.raises(PerFileError, match="no tracker"):
        build(lt.bencode(d), root, size, v2, layer)
    with pytest.raises(PerFileError, match="pure BitTorrent v2"):
        build(lt.bencode(generate(src, PIECE)), root, size, v2, layer)      # a hybrid torrent


# ------------------------------------------------------------------ the node


def test_held_shards_seed_in_their_own_swarms_to_a_client_without_piece_layers(tmp_path, monkeypatch):
    logs: list[str] = []
    sizes = {"model-1.safetensors": 6 * PIECE + 99, "model-2.safetensors": 4 * PIECE + 5, "config.json": 500}
    eng, ih, src, meta = held_model(tmp_path, sizes, logs, monkeypatch)
    monkeypatch.setattr(perfile, "FRESH_WAIT", 0)
    hashed: list[str] = []
    real = perfile.hash_file
    monkeypatch.setattr(perfile, "hash_file", lambda p, *a, **k: hashed.append(p.name) or real(p, *a, **k))
    pf = FakePF()
    eng.perfile.client = lambda: pf
    # Shard 1's swarm uses the model's piece length, shard 2's a different one; config.json has none.
    r1, v1, raw1, e1 = per_file(tmp_path, src / "model-1.safetensors", PIECE)
    r2, v2_, raw2, e2 = per_file(tmp_path, src / "model-2.safetensors", PIECE // 2)
    pf.files = {r1: (e1, raw1), r2: (e2, raw2)}
    eng._save_resume_now(ih, eng.handles[ih])

    eng.perfile.one_pass()
    assert set(pf.calls) == {r1, r2, *[w.root for w in eng.perfile.wanted() if w.path.name == "config.json"]}
    eng.tick()
    assert set(eng.perfile.joined) == {r1, r2}, logs
    assert any("seeding 2 file(s) of org/m in their own swarms" in m for m in logs), logs
    assert not hashed      # both from the block hashes libtorrent saved, whatever the piece length
    # The config file isn't listed: looked up once, then left for a day.
    cfg_root = next(w.root for w in eng.perfile.wanted() if w.path.name == "config.json")
    assert cfg_root in eng.perfile.unlisted and eng.perfile.retry[cfg_root] > time.time() + 3600

    # A peer holding Pirate Face's copy (no piece layers) gets the whole file from us, hashes and all.
    ses, h = downloader(tmp_path, raw1)
    h.connect_peer(("127.0.0.1", eng.ses.listen_port()))
    assert drive(eng, lambda: h.status().is_seeding, others=[ses]), (h.status().progress, logs)
    # Seeding means every piece passed its hash; the writes can still be on their way to disk.
    got, want = tmp_path / "dl" / e1["name"], (src / "model-1.safetensors").read_bytes()
    h.flush_cache()
    assert drive(eng, lambda: got.is_file() and got.read_bytes() == want, others=[ses]), logs
    # ...without our copy changing, or anything written next to it.
    seeds = Path(eng.state.holdings[ih]["save_path"]) / "m"
    assert (seeds / "model-1.safetensors").read_bytes() == (src / "model-1.safetensors").read_bytes()
    assert not (eng.perfile.dir / "unused").exists()
    eng.write_status()

    # Its upload counts as the model's.
    eng.sample(time.time() + 120, force=True)
    eng._account_upload()
    assert eng.state.data["upload"]["bytes"] >= sizes["model-1.safetensors"]

    # A restart brings them back from the stored torrents, without asking Pirate Face again.
    eng.shutdown()
    del ses
    calls = len(pf.calls)
    eng2 = Engine(eng.cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False)
    eng2.perfile.client = lambda: pf
    eng2.resume_holdings()
    eng2.tick()
    assert set(eng2.perfile.joined) == {r1, r2} and len(pf.calls) == calls

    # Dropping a file takes its per-file swarm out first; dropping the model takes them all.
    i2 = next(f.index for f in meta.files if f.repo_path == "model-2.safetensors")
    prios = [4 if str(i) in eng2.state.holdings[ih]["files"] else 0 for i in range(len(meta.files))]
    prios[i2] = 0
    eng2._evict_files(ih, {i2}, prios)
    assert set(eng2.perfile.joined) == {r1} and r2 not in eng2.perfile.saved
    assert not (eng2.perfile.dir / f"{r2}.torrent").exists()
    eng2._remove(ih, delete=True)
    assert not eng2.perfile.joined and not eng2.perfile.saved
    eng2.shutdown()


def test_resume_data_supplies_the_piece_layer_when_libtorrent_has_it(tmp_path, monkeypatch):
    """A file libtorrent downloaded and checked against v2 peers' hashes: its saved tree holds the
    piece layer, so the file isn't read again."""
    logs: list[str] = []
    sizes = {"model-1.safetensors": 6 * PIECE + 99}
    src = write_files(tmp_path / "src" / "m", sizes)
    full = generate(src, PIECE)
    root = lt.torrent_info(full).files().root(0).to_bytes()
    layer = full[b"piece layers"][root]
    # libtorrent's saved tree for that file, as a full tree (every node) like it writes.
    pieces = [layer[i:i + 32] for i in range(0, len(layer), 32)]
    width = 8
    levels = [pieces + [v2hash.pad_hash(PIECE)] * (width - len(pieces))]
    while len(levels[-1]) > 1:
        prev = levels[-1]
        levels.append([hashlib.sha256(prev[i] + prev[i + 1]).digest() for i in range(0, len(prev), 2)])
    tree = b"".join(b"".join(lv) for lv in reversed(levels))     # root first, piece layer last
    resume = lt.bencode({b"file-version": 2, b"trees": [{b"hashes": tree}]})
    assert v2hash.layer_from_resume(resume, 0, sizes["model-1.safetensors"], PIECE, root) == layer
    assert v2hash.layer_from_resume(resume, 0, sizes["model-1.safetensors"], PIECE, bytes(32)) is None
    assert v2hash.layer_from_resume(lt.bencode({b"file-version": 1, b"trees": []}), 0, 1, PIECE, root) is None


def test_without_saved_hashes_the_file_is_hashed_from_disk(tmp_path, monkeypatch):
    logs: list[str] = []
    eng, ih, src, meta = held_model(tmp_path, {"model-1.safetensors": 6 * PIECE + 99}, logs, monkeypatch)
    monkeypatch.setattr(perfile, "FRESH_WAIT", 0)
    hashed: list[str] = []
    real = perfile.hash_file
    monkeypatch.setattr(perfile, "hash_file", lambda p, *a, **k: hashed.append(p.name) or real(p, *a, **k))
    (eng.resume_dir / f"{ih}.fastresume").unlink(missing_ok=True)
    pf = FakePF()
    eng.perfile.client = lambda: pf
    r1, _, raw1, e1 = per_file(tmp_path, src / "model-1.safetensors", PIECE // 2)
    pf.files = {r1: (e1, raw1)}
    eng.perfile.one_pass()
    eng.tick()
    assert hashed == ["model-1.safetensors"] and set(eng.perfile.joined) == {r1}
    ses, h = downloader(tmp_path, raw1)
    h.connect_peer(("127.0.0.1", eng.ses.listen_port()))
    assert drive(eng, lambda: h.status().is_seeding, others=[ses]), logs
    eng.shutdown()


def test_saved_block_hashes_give_the_piece_layer_for_any_piece_length(tmp_path):
    size = 13 * 16384 + 5
    f = write_files(tmp_path / "d", {"x.bin": size}) / "x.bin"
    data = f.read_bytes()
    leaves = [hashlib.sha256(data[i:i + 16384]).digest() for i in range(0, size, 16384)]
    width = 16                                       # leaves rounded up to a power of two
    start = width - 1
    mask = bytearray((2 * width - 1 + 7) // 8)
    for i in range(start, start + len(leaves)):
        mask[i >> 3] |= 0x80 >> (i & 7)
    resume = lt.bencode({b"file-version": 2, b"trees": [{b"hashes": b"".join(leaves), b"mask": bytes(mask)}]})
    for plen in (16384 * 2, 16384 * 4, 16384 * 8):
        root, layer = v2hash.hash_file(f, size, plen)
        assert v2hash.layer_from_resume(resume, 0, size, plen, root, saved_piece_length=16384 * 4) == layer


def test_a_file_verified_moments_ago_waits_for_the_saved_tree_before_being_hashed(tmp_path, monkeypatch):
    logs: list[str] = []
    eng, ih, src, meta = held_model(tmp_path, {"model-1.safetensors": 6 * PIECE + 99}, logs, monkeypatch)
    monkeypatch.setattr(perfile, "hash_file", lambda *a, **k: pytest.fail("hashed a fresh file"))
    monkeypatch.setattr(perfile, "layer_from_resume", lambda *a: None)
    pf = FakePF()
    eng.perfile.client = lambda: pf
    r1, v1, raw1, e1 = per_file(tmp_path, src / "model-1.safetensors", PIECE)
    pf.files = {r1: (e1, raw1)}
    eng.perfile.one_pass()
    eng.tick()
    assert not eng.perfile.joined and 1700 < eng.perfile.retry[r1] - time.time() <= 1800
    eng.shutdown()


def test_pirateface_down_ends_the_pass_and_off_takes_them_out(tmp_path, monkeypatch):
    logs: list[str] = []
    sizes = {"model-1.safetensors": 6 * PIECE + 99, "model-2.safetensors": 4 * PIECE + 5}
    eng, ih, src, meta = held_model(tmp_path, sizes, logs, monkeypatch)
    monkeypatch.setattr(perfile, "FRESH_WAIT", 0)
    pf = FakePF()
    pf.error = FetchError("https://pirateface.co/api/files/x", 503, "HTTP 503")
    eng.perfile.client = lambda: pf
    eng.perfile.one_pass()
    assert len(pf.calls) == 1 and any("couldn't reach Pirate Face" in m for m in logs)
    pf.error = None
    r1, _, raw1, e1 = per_file(tmp_path, src / "model-1.safetensors", PIECE)
    pf.files = {r1: (e1, raw1)}
    eng.perfile.retry.clear()
    eng.perfile.one_pass()
    eng.tick()
    assert set(eng.perfile.joined) == {r1}
    eng.cfg.per_file_swarms = False
    eng.perfile._last_check = 0
    eng.tick()
    assert not eng.perfile.joined and r1 in eng.perfile.saved     # kept for when it's on again
    eng.shutdown()


def test_config_switch_round_trips(tmp_path):
    cfg = Config()
    cfg.per_file_swarms = False
    p = tmp_path / "config.toml"
    p.write_text(render_config(cfg))
    assert load_config(p).per_file_swarms is False
    assert Config().per_file_swarms is True


# ------------------------------------------------------------------ seeding credit


def test_keys_and_seeding_trackers_are_checked():
    assert pfaccount.clean_key(f"  {KEY}\n") == KEY
    for bad in ("hf_abc", "pfct_", "pfct_short", KEY + " extra"):
        with pytest.raises(pfaccount.PFKeyError):
            pfaccount.clean_key(bad)
    assert pfaccount.seeding_tracker(["udp://tracker.pirateface.co:6969/announce", MINE]) == MINE
    with pytest.raises(pfaccount.PFKeyError, match="no seeding tracker"):
        pfaccount.seeding_tracker(["udp://tracker.pirateface.co:6969/announce"])
    with pytest.raises(pfaccount.PFKeyError, match="isn't a tracker Flotilla announces to"):
        pfaccount.seeding_tracker(["http://tracker.evil.example:6970/tok/announce"])
    assert pfaccount.tracker_host(MINE) == "tracker.pirateface.co:6970"


def test_account_follows_the_key_file_and_keeps_its_tracker_through_an_outage(tmp_path):
    logs: list[str] = []
    pf = FakePF()
    path = tmp_path / pfaccount.KEY_FILE
    acct = pfaccount.Account(path, lambda: pf, log=logs.append)
    acct.poll(1000.0)
    assert acct.tracker() == (0, None) and acct.info()["state"] == "none" and not pf.keys
    path.write_text(KEY + "\n")
    acct.poll(1001.0)
    gen, url = acct.tracker()
    assert url == MINE and gen == 1 and pf.keys == [KEY]
    assert not any("s3cr3t" in m for m in logs), logs          # the token never reaches the log
    assert any("tracker.pirateface.co:6970" in m for m in logs)
    acct.poll(1002.0)
    assert len(pf.keys) == 1                                   # not again until it's due
    pf.error = FetchError("https://pirateface.co/api/trackers", None, "timed out")
    acct.poll(1001.0 + pfaccount.REFETCH_EVERY)
    assert acct.tracker() == (1, MINE)                         # Pirate Face down: keep counting
    pf.error = FetchError("https://pirateface.co/api/trackers", 401, "HTTP 401")
    acct.poll(1001.0 + 2 * pfaccount.REFETCH_EVERY)
    assert acct.tracker() == (2, None) and acct.info()["state"] == "refused"
    path.unlink()
    acct.poll(1001.0 + 3 * pfaccount.REFETCH_EVERY)
    assert acct.info()["state"] == "none"
    assert pfaccount.key_path(tmp_path / "config.toml") == path


def test_the_seeding_tracker_is_added_to_every_torrent_and_taken_off_again(tmp_path, monkeypatch):
    logs: list[str] = []
    eng, ih, src, meta = held_model(tmp_path, {"model-1.safetensors": 6 * PIECE + 99}, logs, monkeypatch)
    monkeypatch.setattr(perfile, "FRESH_WAIT", 0)
    pf = FakePF()
    eng.perfile.client = lambda: pf
    r1, _, raw1, e1 = per_file(tmp_path, src / "model-1.safetensors", PIECE)
    pf.files = {r1: (e1, raw1)}
    eng.pf_account.pf_factory = lambda: pf
    pfaccount.key_path(eng.cfg.path).parent.mkdir(parents=True, exist_ok=True)
    pfaccount.key_path(eng.cfg.path).write_text(KEY)
    eng.pf_account.poll()
    eng.tick()

    def urls(h):
        return [t["url"] for t in h.trackers()]

    assert urls(eng.handles[ih])[-1] == MINE
    assert eng.ses.get_settings()["announce_to_all_tiers"] is True
    eng.perfile.one_pass()
    eng.tick()                                       # joined after the tracker: gets it too
    assert urls(eng.perfile.joined[r1].handle)[-1] == MINE
    assert "udp://tracker.pirateface.co:6969/announce" in urls(eng.perfile.joined[r1].handle)
    status = eng.status_doc()
    assert status["pirateface"]["tracker"] == "tracker.pirateface.co:6970" and "s3cr3t" not in str(status)
    assert status["per_file"]["joined"] == 1 and status["torrents"][0]["per_file"] == 1

    pfaccount.key_path(eng.cfg.path).unlink()
    eng.pf_account.poll()
    eng.tick()
    assert MINE not in urls(eng.handles[ih]) and MINE not in urls(eng.perfile.joined[r1].handle)
    assert eng.ses.get_settings()["announce_to_all_tiers"] is False
    eng.shutdown()


# ------------------------------------------------------------------ the command line


def run_cli(tmp_path, *argv):
    from flotilla import cli

    return cli.main(["--config", str(tmp_path / "config.toml"), "--state", str(tmp_path / "state"), *argv])


def test_points_connect_checks_then_saves_and_never_prints_the_key(tmp_path, monkeypatch, capsys):
    import stat

    from flotilla import sources

    pf = FakePF()
    monkeypatch.setattr(sources.PirateFace, "trackers", lambda self, key: pf.trackers(key))
    keyfile = tmp_path / "key.txt"
    keyfile.write_text(KEY + "\n")
    pf.error = FetchError("https://pirateface.co/api/trackers", 401, "HTTP 401")
    assert run_cli(tmp_path, "points", "connect", "--from-file", str(keyfile)) == 1
    assert not (tmp_path / "pirateface-key").exists() and "didn't accept this key" in capsys.readouterr().out

    pf.error = None
    assert run_cli(tmp_path, "points", "connect", "--from-file", str(keyfile)) == 0
    assert pfaccount.read_key(tmp_path / "pirateface-key") == KEY
    assert stat.S_IMODE((tmp_path / "pirateface-key").stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "tracker.pirateface.co:6970" in out and KEY not in out and "s3cr3t" not in out

    assert run_cli(tmp_path, "points") == 0
    out = capsys.readouterr().out
    assert "Connected: seeding counts toward your account's points" in out and "s3cr3t" not in out
    assert run_cli(tmp_path, "points", "disconnect") == 0
    assert not (tmp_path / "pirateface-key").exists()
    assert run_cli(tmp_path, "points") == 0
    assert "No Pirate Face account connected" in capsys.readouterr().out


def test_setup_asks_for_a_tracker_or_key_and_enter_skips(tmp_path, monkeypatch, capsys):
    import argparse
    import stat

    from flotilla import cli, netinfo, sources

    monkeypatch.setattr(netinfo, "internet_route", lambda: None)
    pf = FakePF()

    class OfflineLicenses:
        def __init__(self, *a, **kw):
            pass

        def licenses(self):
            raise OSError("offline")

        def trackers(self, key):
            return pf.trackers(key)

    monkeypatch.setattr(sources, "PirateFace", OfflineLicenses)
    prompts: list[str] = []
    pasted: list[str] = []

    def run(paste, answers=()):
        it = iter([str(tmp_path / "seeds"), *answers])
        prompts.clear()

        def ask(prompt=""):
            prompts.append(prompt)
            return next(it, "")

        def secret(prompt=""):
            prompts.append(prompt)
            return pasted.pop(0) if pasted else ""

        pasted[:] = [paste]
        monkeypatch.setattr("builtins.input", ask)
        monkeypatch.setattr("getpass.getpass", secret)
        assert cli.cmd_init(argparse.Namespace(config=str(tmp_path / "config.toml"))) == 0

    # Enter skips: nothing saved, nothing asked of Pirate Face.
    run("")
    assert any("Paste it" in p and "Enter to skip" in p for p in prompts), prompts
    assert not (tmp_path / "pirateface-key").exists() and not pf.keys
    assert "Skipped: seeding earns no points" in capsys.readouterr().out

    # The tracker address from the settings page is saved as it is, without asking Pirate Face.
    run(MINE)
    assert pfaccount.read_key(tmp_path / "pirateface-key") == MINE and not pf.keys
    assert stat.S_IMODE((tmp_path / "pirateface-key").stat().st_mode) == 0o600
    assert "s3cr3t" not in capsys.readouterr().out

    # Run again: it offers to keep the connected account, and asks for nothing new.
    run("")
    assert any("Keep earning points with it" in p for p in prompts)
    assert not any("Paste it" in p for p in prompts)
    assert pfaccount.read_key(tmp_path / "pirateface-key") == MINE

    # A key instead, after letting the old one go: checked with Pirate Face, then saved.
    (tmp_path / "pirateface-key").unlink()
    run(KEY)
    assert pfaccount.read_key(tmp_path / "pirateface-key") == KEY and pf.keys == [KEY]


def test_a_pasted_tracker_address_is_used_as_it_is(tmp_path, capsys, monkeypatch):
    from flotilla import sources

    assert pfaccount.clean_key(f"  {MINE}\n") == MINE and pfaccount.kind_of(MINE) == "tracker"
    for bad in ("http://tracker.pirateface.co:6969/s3cr3t/announce",          # the public tracker's port
                "http://tracker.evil.example:6970/s3cr3t/announce",          # someone else's
                "http://tracker.pirateface.co:6970/s3cr3t",                  # cut short
                "https://pirateface.co/settings"):
        with pytest.raises(pfaccount.PFKeyError) as e:
            pfaccount.clean_key(bad)
        assert "s3cr3t" not in str(e.value)                                    # a typo'd secret isn't repeated
    # The running node: no Pirate Face call, counted at once, never asked again.
    pf = FakePF()
    logs: list[str] = []
    path = tmp_path / pfaccount.KEY_FILE
    path.write_text(MINE + "\n")
    acct = pfaccount.Account(path, lambda: pf, log=logs.append)
    acct.poll(1000.0)
    assert acct.tracker() == (1, MINE) and acct.info()["state"] == "ok" and acct.info()["kind"] == "tracker"
    acct.poll(1000.0 + 3 * pfaccount.REFETCH_EVERY)
    assert not pf.keys and acct.tracker() == (1, MINE)
    assert any("tracker.pirateface.co:6970" in m for m in logs) and not any("s3cr3t" in m for m in logs)
    path.unlink()
    acct.poll(2000.0 + 3 * pfaccount.REFETCH_EVERY)
    assert acct.tracker() == (2, None) and acct.info()["state"] == "none"
    # The command line takes it too, and says it can't be confirmed.
    monkeypatch.setattr(sources.PirateFace, "trackers", lambda self, key: pf.trackers(key))
    f = tmp_path / "t.txt"
    f.write_text(MINE)
    assert run_cli(tmp_path, "points", "connect", "--from-file", str(f)) == 0
    out = capsys.readouterr().out
    assert "can't confirm a tracker address" in out and "s3cr3t" not in out and not pf.keys
    assert run_cli(tmp_path, "points") == 0
    out = capsys.readouterr().out
    assert "a seeding tracker address, used as it is" in out and "s3cr3t" not in out and not pf.keys
    empty = tmp_path / "empty.txt"
    empty.write_text("\n")
    assert run_cli(tmp_path, "points", "connect", "--from-file", str(empty)) == 1
    assert "nothing to connect" in capsys.readouterr().out


def test_the_dashboard_connects_and_disconnects_the_account(tmp_path, monkeypatch):
    import stat

    from flotilla import remote

    logs: list[str] = []
    eng, ih, src, meta = held_model(tmp_path, {"model-1.safetensors": 2 * PIECE}, logs, monkeypatch)
    kpath = pfaccount.key_path(eng.cfg.path)
    for junk in ("", "hello", "http://tracker.evil.example:6970/s3cr3t/announce", 5, "x" * 2000):
        with pytest.raises(ValueError) as e:
            eng.submit("points_connect", {"secret": junk})
        assert "s3cr3t" not in str(e.value)
    assert not kpath.exists()

    assert eng.submit("points_connect", {"secret": MINE}) == "connected"
    assert pfaccount.read_key(kpath) == MINE and stat.S_IMODE(kpath.stat().st_mode) == 0o600
    eng.pf_account.poll()
    eng.tick()
    assert [t["url"] for t in eng.handles[ih].trackers()][-1] == MINE
    doc = eng.status_doc()
    assert doc["pirateface"]["state"] == "ok" and doc["pirateface"]["kind"] == "tracker"
    assert "s3cr3t" not in json.dumps(doc) and not any("s3cr3t" in m for m in logs)

    # A key replaces it, and is checked with Pirate Face by the points thread.
    pf = FakePF()
    eng.pf_account.pf_factory = lambda: pf
    assert eng.submit("points_connect", {"secret": KEY}) == "checking" and pfaccount.read_key(kpath) == KEY
    eng.pf_account.poll()
    assert pf.keys == [KEY] and eng.pf_account.info()["kind"] == "key"

    assert eng.submit("points_disconnect", {}) == "disconnected" and not kpath.exists()
    assert eng.submit("points_disconnect", {}) == "not connected"
    eng.pf_account.poll()
    eng.tick()
    assert MINE not in [t["url"] for t in eng.handles[ih].trackers()]

    # Set by FLOTILLA_PF_KEY_FILE (a Docker secret): the dashboard doesn't write there.
    monkeypatch.setenv("FLOTILLA_PF_KEY_FILE", str(tmp_path / "secret"))
    with pytest.raises(ValueError, match="change it there"):
        eng.submit("points_connect", {"secret": MINE})
    assert not (tmp_path / "secret").exists()
    monkeypatch.delenv("FLOTILLA_PF_KEY_FILE")
    eng.shutdown()

    # A dashboard in another container hands it over in a file only this user can read.
    seen: list[int] = []

    def answer(*a, **kw):
        for f in (tmp_path / "rstate" / "inbox").glob("*.json"):
            seen.append(stat.S_IMODE(f.stat().st_mode))
        raise ValueError("stop")

    monkeypatch.setattr(remote.time, "sleep", answer)
    with pytest.raises(ValueError):
        remote.RemoteEngine(tmp_path / "rstate").submit("points_connect", {"secret": MINE})
    assert seen and set(seen) == {0o600}
