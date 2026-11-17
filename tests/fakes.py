"""Synthetic model repos, torrents and fake Pirate Face / Hugging Face clients."""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

from flotilla.sources import HFFile
from flotilla.torrentmeta import make_torrent

TRACKER = "udp%3A%2F%2Ftracker.pirateface.co%3A6969%2Fannounce"


def git_oid(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def make_repo(n_shards: int = 6, shard_size: int = 200_000, seed: int = 7) -> dict[str, bytes]:
    rng = random.Random(seed)
    files = {
        "LICENSE": b"MIT License\n" * 10,
        "README.md": b"# test model\n",
        "config.json": b'{"model_type": "test"}',
        "modeling_test.py": b"print('never executed')\n",
    }
    for i in range(1, n_shards + 1):
        files[f"model-{i:05d}-of-{n_shards:05d}.safetensors"] = rng.randbytes(shard_size + rng.randint(0, 999))
    return files


class FakeRepo:
    def __init__(self, repo: str, files: dict[str, bytes], piece_length: int = 16384, seeders: int = 1,
                 leechers: int = 0, hf_backed: bool = True):
        self.repo = repo
        self.files = files
        self.name = repo.replace("/", "__")
        self.raw = make_torrent(sorted(files.items()), self.name, piece_length)
        from flotilla.torrentmeta import parse_torrent

        self.meta = parse_torrent(self.raw)
        self.infohash = self.meta.infohash_v1
        self.revision = hashlib.sha1(repo.encode()).hexdigest()
        self.seeders, self.leechers, self.hf_backed = seeders, leechers, hf_backed

    def magnet(self, extra: str = "") -> str:
        ws = f"https%3A%2F%2Fpirateface.co%2Fapi%2Fws%2F{self.repo.replace('/', '%2F')}%2F{self.revision}%2F"
        return f"magnet:?xt=urn:btih:{self.infohash}&tr={TRACKER}&ws={ws}{extra}"

    def listing(self) -> dict:
        return {
            "repoId": self.repo, "revision": self.revision, "seeders": self.seeders, "leechers": self.leechers,
            "files": [{"path": p, "sha256": hashlib.sha256(d).hexdigest()} for p, d in self.files.items()],
            "hf": self.hf_backed, "quant": "q", "infohash": self.infohash, "magnet": self.magnet(),
        }

    def tree(self) -> dict[str, HFFile]:
        out = {}
        for p, d in self.files.items():
            if p.endswith(".safetensors"):
                out[p] = HFFile(len(d), hashlib.sha256(d).hexdigest(), None)
            else:
                out[p] = HFFile(len(d), None, git_oid(d))
        return out

    def write(self, base: Path) -> Path:
        root = base / self.name
        for p, d in self.files.items():
            (root / p).parent.mkdir(parents=True, exist_ok=True)
            (root / p).write_bytes(d)
        return base


class FakePF:
    def __init__(self, repos: list[FakeRepo]):
        self.repos = {r.repo: r for r in repos}
        self.by_ih = {r.infohash: r for r in repos}
        self.overrides: dict[str, dict] = {}
        self.raw_overrides: dict[str, bytes] = {}

    def torrent(self, repo, quant=None):
        if repo in self.overrides:
            return self.overrides[repo]
        r = self.repos.get(repo)
        return r.listing() if r else None

    def torrent_file(self, ih):
        return self.raw_overrides.get(ih) or self.by_ih[ih].raw

    def new_torrents(self):
        return []

    def rescue(self):
        return {"rescued": []}


class FakeHF:
    def __init__(self, repos: list[FakeRepo], gone: set[str] = frozenset()):
        self.repos = {r.repo: r for r in repos}
        self.gone = gone
        self.tree_overrides: dict[str, dict] = {}
        self.main_overrides: dict[str, dict | None] = {}   # what "main" looks like, if it moved on
        self.calls: list[tuple[str, str]] = []

    def license(self, repo):
        return "mit"

    def tree(self, repo, revision):
        self.calls.append((repo, revision))
        if repo in self.gone:
            return None
        if revision == "main" and repo in self.main_overrides:
            return self.main_overrides[repo]
        if repo in self.tree_overrides:
            return self.tree_overrides[repo]
        return self.repos[repo].tree()
