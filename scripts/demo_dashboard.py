"""Offline dashboard demo for UI work: python scripts/demo_dashboard.py [--hours 8] [--port 7499]

Runs the real Flotilla engine and dashboard against a fake swarm on 127.0.0.1:
three made-up models, a full seeder, a partial seeder, and a slow downloader
pulling from this node so the upload numbers move. Nothing touches the
internet: no trackers, no DHT, no Pirate Face or Hugging Face calls.

Everything works, including Replan (re-probes the fake swarm), Drop and
settings (saved to the demo's own config). The partial seeder shows up after
the node's first plan, so some of what the node holds is then over-copied and
the Reorienting card has a swap to show (Reorient now carries it out). Files
live in a temp folder that is deleted on exit.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import libtorrent as lt  # noqa: E402

from flotilla.candidates import build_candidate  # noqa: E402
from flotilla.config import Config, State, render_config  # noqa: E402
from flotilla.engine import Engine  # noqa: E402
from flotilla.feed import FeedModel, neutral_feed  # noqa: E402
from tests.fakes import FakeHF, FakePF, FakeRepo, make_repo  # noqa: E402

LOCAL = {"enable_dht": False, "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
         "allow_multiple_connections_per_ip": True}


def peer(name: str, fp: str) -> lt.session:
    return lt.session({**LOCAL, "listen_interfaces": "127.0.0.1:0", "peer_fingerprint": fp, "user_agent": name})


def add(ses: lt.session, repo: FakeRepo, path: Path, prios=None, limit=0) -> lt.torrent_handle:
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(lt.bdecode(repo.raw))
    atp.save_path = str(path)
    if prios is not None:
        atp.file_priorities = prios
    if limit:
        atp.download_limit = limit
    atp.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
    return ses.add_torrent(atp)


class DemoEngine(Engine):
    """The real engine, with discovery swapped for the fake catalogue."""

    def __init__(self, *a, catalogue, **kw):
        super().__init__(*a, **kw)
        self.catalogue = catalogue

    def discover(self):
        cands = []
        for repo, entry, pf, hf in self.catalogue:
            c = build_candidate(repo.repo, None, neutral_feed(), entry, pf, hf, self.cfg)
            c.priority = neutral_feed().priority(entry) if entry else c.priority
            cands.append(c)
        dropped = set(self.state.data["dropped"])
        return [c for c in cands if c.repo not in dropped], []

    def sample(self, now: float, force: bool = False) -> None:  # a chart point every 10 s, not every minute
        if force or now - self._last_sample >= 10:
            self._last_sample = 0
            super().sample(now, force=force)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7499)
    ap.add_argument("--hours", type=float, default=8)
    args = ap.parse_args()
    base = Path(tempfile.mkdtemp(prefix="flotilla-demo-"))
    print(f"demo files in {base}", flush=True)

    big = FakeRepo("demo-labs/Frontier-MoE-700B", make_repo(n_shards=60, shard_size=300_000, seed=1),
                   seeders=1, leechers=2)
    lost = FakeRepo("ghost-ai/Cyber-Agent-27B-GGUF", make_repo(n_shards=12, shard_size=400_000, seed=2),
                    seeders=1, hf_backed=False)
    small = FakeRepo("tiny-co/Small-Embed-1B", make_repo(n_shards=3, shard_size=100_000, seed=3), seeders=2)

    # A full seeder of everything, and a partial seeder holding every third shard of the big model.
    A = peer("full-seeder", "-XA0100-")
    for r in (big, lost, small):
        add(A, r, r.write(base / "A"))
    C = peer("partial-seeder", "-XC0100-")
    some = [1 if (i % 3 == 0 or not f.repo_path.endswith(".safetensors")) else 0
            for i, f in enumerate(big.meta.files)]
    hc = add(C, big, base / "C", prios=some)
    a_addr = ("127.0.0.1", A.listen_port())
    hc.connect_peer(a_addr)
    for _ in range(100):
        if hc.status().is_finished:
            break
        time.sleep(0.1)
    c_addr = ("127.0.0.1", C.listen_port())

    # Pirate Face lists a wrong hash for the small model: shows the "hash disagreed" tag.
    pf_small = FakePF([small])
    listing = small.listing()
    listing["files"] = [dict(f, sha256="0" * 64) if f["path"].endswith(".safetensors") else f
                        for f in listing["files"]]
    pf_small.overrides[small.repo] = listing
    catalogue = [
        (big, FeedModel(big.repo, {"capability": 95, "cyber": 80, "takedown_risk": 70}, target_replicas=2),
         FakePF([big]), FakeHF([big])),
        (lost, FeedModel(lost.repo, {"capability": 70, "cyber": 90, "takedown_risk": 95}, target_replicas=3),
         FakePF([lost]), FakeHF([lost], gone={lost.repo})),
        (small, None, pf_small, FakeHF([small])),
    ]

    cfg = Config()
    cfg.folder, cfg.listen_port = base / "node", 0
    cfg.budget_gb, cfg.reserve_gb = 0.02, 0           # 20 MB: all of two models, part of the big one
    cfg.whole_model_under_gb = 0.001                   # the 0.3 MB model is kept whole
    cfg.upload_mbps, cfg.download_mbps, cfg.monthly_upload_cap_gb = 1, 20, 0
    cfg.probe_seconds, cfg.refresh_hours = 4, 6
    cfg.reorient, cfg.churn_gb, cfg.min_hold_days = "weekly", 0.002, 0   # swap up to 2 MB; nothing is too new to go
    cfg.dashboard_port, cfg.include_unlisted = args.port, False
    cfg.path = base / "config.toml"
    cfg.path.write_text(render_config(cfg))
    state = State(base / "state")
    eng = DemoEngine(cfg, state, log=lambda m: print("[node]", m, flush=True), listen_host="127.0.0.1",
                     dht=False, connect=[a_addr], catalogue=catalogue)

    def late_seeder():
        """The partial seeder is found after the first plan: its shards are then the over-copied ones."""
        while not eng.stop.is_set() and not eng._plans_applied:
            time.sleep(0.5)
        eng.connect.append(c_addr)
        eng.commands.put(("replan", {}))

    threading.Thread(target=late_seeder, daemon=True).start()

    def downloader():
        """A slow peer that keeps pulling the big model from this node only."""
        L = peer("downloader", "-XL0100-")
        while not eng.stop.is_set():
            h = add(L, big, base / "L", limit=12_000)
            started = time.monotonic()
            while not eng.stop.is_set() and time.monotonic() - started < 600:
                if not h.status().num_peers and eng.ses.listen_port():
                    h.connect_peer(("127.0.0.1", eng.ses.listen_port()))
                time.sleep(5)
            L.remove_torrent(h, lt.session.delete_files)   # start over so there's always something to send
            time.sleep(2)

    threading.Thread(target=downloader, daemon=True).start()
    print(f"dashboard: http://127.0.0.1:{args.port}  (Ctrl-C to stop)", flush=True)
    try:
        eng.run(until=time.time() + args.hours * 3600)
    finally:
        shutil.rmtree(base, ignore_errors=True)
        print("demo stopped; files removed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
