"""End to end on 127.0.0.1: a full seeder, a Flotilla node, and an observer.

No internet: trackers/DHT are off and peers are connected by hand.
"""

import time
from pathlib import Path

import pytest

lt = pytest.importorskip("libtorrent")

from flotilla.candidates import TorrentCand, build_candidate
from flotilla.config import Config, State
from flotilla.engine import Engine, probe
from flotilla.feed import neutral_feed

from .fakes import FakeHF, FakePF, FakeRepo, make_repo

pytestmark = pytest.mark.swarm


def seeder(repo: FakeRepo, base: Path):
    src = repo.write(base)
    ses = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                      "enable_upnp": False, "enable_natpmp": False, "allow_multiple_connections_per_ip": True,
                      "peer_fingerprint": "-XX0100-"})
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(lt.bdecode(repo.raw))
    atp.save_path = str(src)
    atp.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
    h = ses.add_torrent(atp)
    for _ in range(100):
        if h.status().is_seeding:
            break
        time.sleep(0.05)
    assert h.status().is_seeding
    return ses, h


def make_cfg(tmp_path: Path, budget_bytes: int) -> Config:
    c = Config()
    c.folder = tmp_path / "fl-seeds"
    c.budget_gb = budget_bytes / 1e9
    c.reserve_gb = 0
    c.listen_port = 0
    c.whole_model_under_gb = 0          # force shard mode on a tiny repo
    c.upload_mbps = c.download_mbps = 100
    c.probe_seconds = 3
    return c


def drive(eng: Engine, until, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        eng.ses.wait_for_alert(200)
        eng.tick()
        if until():
            return True
    return False


def candidate(repo, pf=None, hf=None, cfg=None) -> TorrentCand:
    c = build_candidate(repo.repo, None, neutral_feed(), None, pf or FakePF([repo]), hf or FakeHF([repo]),
                        cfg or Config())
    assert isinstance(c, TorrentCand), c
    return c


def test_partial_seed_probe_verify_resume(tmp_path):
    files = make_repo(n_shards=8, shard_size=150_000)
    repo = FakeRepo("test/model", files, seeders=1)
    A, _ = seeder(repo, tmp_path / "A")
    a_addr = ("127.0.0.1", A.listen_port())

    shard = 150_000 + 1000 + 2 * 16384
    cfg = make_cfg(tmp_path, budget_bytes=4 * shard)
    state = State(tmp_path / "state")
    logs: list[str] = []
    eng = Engine(cfg, state, log=logs.append, listen_host="127.0.0.1", dht=False, connect=[a_addr])

    c = candidate(repo)
    plan = eng.replan([c], [], probe_seconds=6)

    # The probe saw A as a complete seeder: every file available once.
    assert all(f.availability == 1 for f in c.files), [(f.repo_path, f.availability) for f in c.files]
    pick = plan.picks[c.infohash]
    weights = [f for f in pick.files if f.kind == "weight"]
    assert 2 <= len(weights) <= 4, [f.repo_path for f in pick.files]
    assert {"LICENSE", "config.json"} <= {f.repo_path for f in pick.files}
    assert "modeling_test.py" not in {f.repo_path for f in pick.files}

    assert drive(eng, eng.all_done), logs
    assert c.infohash in state.holdings, logs
    hold = state.holdings[c.infohash]
    assert all(e["status"] == "verified" for e in hold["files"].values()), hold
    base = Path(hold["save_path"])
    picked = {f.repo_path for f in pick.files}
    for f in c.files:
        p = base / c.meta.files[f.index].path
        if f.repo_path in picked:
            assert p.read_bytes() == files[f.repo_path]
        elif f.kind == "weight":
            assert not p.exists(), f"{f.repo_path} should not be on disk"   # true partial seed
    assert not (base / repo.name / "modeling_test.py").exists()

    # Someone else downloading the whole model, slowly (another client: it counts as demand).
    L = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                    "enable_upnp": False, "enable_natpmp": False, "allow_multiple_connections_per_ip": True,
                    "peer_fingerprint": "-qB4650-", "download_rate_limit": 1024,
                    "ignore_limits_on_local_network": False})
    latp = lt.add_torrent_params()
    latp.ti, latp.save_path = lt.torrent_info(lt.bdecode(repo.raw)), str(tmp_path / "L")
    latp.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
    L.add_torrent(latp).connect_peer(a_addr)

    # A third node probing the swarm sees exactly which shards we hold, and one real downloader: the
    # Flotilla node, a partial seeder the tracker would call a leecher, isn't demand.
    # (10 s, so a peer that connects late still has time to send its piece map: on a busy machine
    # one connection can take seconds.)
    obs = probe([candidate(repo)], cfg, tmp_path / "observer", 10, b"-FL0100-ffffff", log=lambda *_: None,
                listen_host="127.0.0.1", dht=False,
                connect=[a_addr, ("127.0.0.1", eng.ses.listen_port()), ("127.0.0.1", L.listen_port())])
    del L
    r = obs[c.infohash]
    assert r["seeds"] == 1, (r, [str(x) for x in eng.handles[c.infohash].get_peer_info()])
    assert r["downloaders"] == 1, r
    for f in c.files:
        if f.kind == "weight":
            assert r["partial"][f.index] == (1 if f.repo_path in picked else 0), (f.repo_path, r)

    # Restart: resume data brings the seed back without re-downloading.
    eng.shutdown()
    del eng
    state2 = State(tmp_path / "state")
    eng2 = Engine(cfg, state2, log=logs.append, listen_host="127.0.0.1", dht=False)
    eng2.resume_holdings()
    assert c.infohash in eng2.handles, logs[-5:]
    h = eng2.handles[c.infohash]
    assert drive(eng2, lambda: h.status().is_finished, timeout=20)
    assert h.status().total_payload_download == 0
    assert all(e["status"] == "verified" for e in state2.holdings[c.infohash]["files"].values())
    eng2.shutdown()


def test_hash_mismatch_blocks_torrent_and_deletes_data(tmp_path):
    files = make_repo(n_shards=4, shard_size=100_000, seed=11)
    repo = FakeRepo("evil/model", files, seeders=1)
    A, _ = seeder(repo, tmp_path / "A")
    a_addr = ("127.0.0.1", A.listen_port())

    # The torrent is internally consistent (libtorrent's piece hashes pass) but
    # does not match what Hugging Face publishes for the repo.
    pf = FakePF([repo])
    listing = repo.listing()
    listing["files"] = []
    pf.overrides[repo.repo] = listing
    hf = FakeHF([repo])
    tree = repo.tree()
    for p, f in tree.items():
        if f.sha256:
            tree[p] = type(f)(f.size, "0" * 64, None)
    hf.tree_overrides[repo.repo] = tree

    cfg = make_cfg(tmp_path, budget_bytes=10_000_000)
    state = State(tmp_path / "state")
    logs: list[str] = []
    eng = Engine(cfg, state, log=logs.append, listen_host="127.0.0.1", dht=False, connect=[a_addr])
    c = candidate(repo, pf=pf, hf=hf)
    eng.replan([c], [], probe_seconds=0)
    save_path = Path(state.holdings[c.infohash]["save_path"])

    assert drive(eng, lambda: c.infohash in state.data["blocklist"]), logs
    assert c.infohash not in state.holdings
    assert c.infohash not in eng.handles
    assert not save_path.exists()
    assert any("VERIFY FAILED" in l for l in logs)
    eng.shutdown()


def test_ledger_catches_tampered_bytes_pirateface_vouched_for(tmp_path):
    """Hugging Face has dropped the model and a compromised Pirate Face serves altered shards
    with matching hashes. Without the ledger the node takes them; once a ledger recorded
    from Hugging Face arrives, the node re-checks, finds the swap, deletes and blocks it."""
    from flotilla.ledger import add_entry, capture, load_ledger
    from flotilla.signing import SecretKey

    genuine = FakeRepo("gone/model", make_repo(n_shards=3, shard_size=90_000, seed=31))
    tampered_files = dict(genuine.files)
    shard = "model-00002-of-00003.safetensors"
    tampered_files[shard] = bytes(b ^ 0x5A for b in genuine.files[shard])   # same size, different bytes
    evil = FakeRepo(genuine.repo, tampered_files, seeders=1)
    evil.revision = genuine.revision

    sk = SecretKey.generate()
    index = tmp_path / "ledger" / "index.json"
    doc, _ = capture(genuine.repo, genuine.revision, FakeHF([genuine]), FakePF([genuine]))   # while HF had it
    add_entry(index, doc)
    index.with_name("index.json.minisig").write_text(sk.sign(index.read_bytes()))

    A, _ = seeder(evil, tmp_path / "A")
    cfg = make_cfg(tmp_path, budget_bytes=10_000_000)
    state = State(tmp_path / "state")
    logs: list[str] = []
    eng = Engine(cfg, state, log=logs.append, listen_host="127.0.0.1", dht=False,
                 connect=[("127.0.0.1", A.listen_port())])
    pf, hf = FakePF([evil]), FakeHF([evil], gone={evil.repo})

    c = build_candidate(evil.repo, None, neutral_feed(), None, pf, hf, Config())
    assert c.verify_source == "pirateface-only"
    eng.replan([c], [], probe_seconds=0)
    assert drive(eng, eng.all_done), logs
    assert all(e["status"] == "verified" for e in state.holdings[c.infohash]["files"].values())   # fooled

    led = load_ledger([sk.public], tmp_path / "state", (), url=None, bundled=index, log=logs.append)
    c2 = build_candidate(evil.repo, None, neutral_feed(), None, pf, hf, Config(), ledger=led)
    assert isinstance(c2, TorrentCand) and c2.verify_source.startswith("ledger")
    assert [m["path"] for m in c2.manifest_mismatches] == [shard]
    save_path = Path(state.holdings[c.infohash]["save_path"])
    eng.replan([c2], [], probe_seconds=0)
    assert drive(eng, lambda: c.infohash in state.data["blocklist"]), logs[-10:]
    assert any("re-verify" in l for l in logs) and any("VERIFY FAILED" in l for l in logs)
    assert c.infohash not in state.holdings and not save_path.exists()
    eng.shutdown()


def test_wrong_pirateface_manifest_still_seeds_hf_verified_bytes(tmp_path):
    """Pirate Face's listed SHA-256 is wrong, the swarm holds the
    genuine Hugging Face bytes. Prefer HF: download, verify against HF, seed."""
    files = make_repo(n_shards=3, shard_size=120_000, seed=21)
    repo = FakeRepo("orca/model", files, seeders=1)
    A, _ = seeder(repo, tmp_path / "A")
    a_addr = ("127.0.0.1", A.listen_port())

    pf = FakePF([repo])
    listing = repo.listing()
    bad = "model-00002-of-00003.safetensors"
    listing["files"] = [f if f["path"] != bad else {"path": bad, "sha256": "0" * 64} for f in listing["files"]]
    pf.overrides[repo.repo] = listing

    cfg = make_cfg(tmp_path, budget_bytes=10_000_000)
    state = State(tmp_path / "state")
    logs: list[str] = []
    eng = Engine(cfg, state, log=logs.append, listen_host="127.0.0.1", dht=False, connect=[a_addr])
    c = candidate(repo, pf=pf)
    assert [m["path"] for m in c.manifest_mismatches] == [bad]
    eng.replan([c], [], probe_seconds=0)

    assert drive(eng, eng.all_done), logs
    hold = state.holdings[c.infohash]
    assert c.infohash not in state.data["blocklist"]
    assert all(e["status"] == "verified" for e in hold["files"].values()), hold
    assert hold["manifest_mismatches"][0]["path"] == bad
    base = Path(hold["save_path"])
    assert (base / repo.name / bad).read_bytes() == files[bad]
    eng.shutdown()
