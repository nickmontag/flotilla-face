"""Per-file swarms: each shard Flotilla holds also seeds in that file's own swarm.

Pirate Face publishes a BitTorrent v2 torrent for every file of its hybrid model torrents, named by
the file's pieces root (GET /api/files/<root>), and every listed upload holding the same bytes
shares it. In a model's swarm, a node holding some shards counts as a leecher; in each of those
shards' own swarms it is a complete seeder, which is what peers and Pirate Face count.

For every verified file of 1 MiB or more in a hybrid torrent, a worker thread:

1. asks Pirate Face for the file's swarm (and remembers "not listed" for a day);
2. fetches its .torrent and checks it: pure v2, exactly one file, the same pieces root and size,
   and the infohash Pirate Face named;
3. adds the piece layer Pirate Face's copy leaves out: from the hash tree libtorrent saved while
   downloading the file, or by hashing the file on disk. It's used only if it hashes up to the
   pieces root, so the torrent describes exactly the bytes already verified against Hugging Face;
4. keeps only trackers on the tracker allowlist and drops web seeds, then stores it.

The main loop adds it to the session in seed mode and upload mode, pointed at the model's file on
disk: it never downloads or writes, and libtorrent checks each piece against the root before it
sends it. It's taken out before the file is deleted, and when the file stops being verified.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import libtorrent as lt

from .magnet import DEFAULT_TRACKER_HOSTS
from .netsafe import FetchError, host_allowed
from .sources import PirateFace
from .v2hash import BLOCK, hash_file, layer_from_resume, needs_layer, root_from_layer

MIN_SIZE = 1 << 20              # configs and tokenizers aren't worth a swarm each (pf's limit too)
LOOKUP_GAP = 1.0                # seconds between lookups, so a node holding hundreds of shards goes gently
PASS_EVERY = 300.0              # seconds between passes over the held files
UNLISTED_RETRY = 86400.0        # a file Pirate Face has no swarm for: ask again a day later
ERROR_RETRY = 3600.0            # a lookup that failed, or a torrent that didn't check out
FRESH_WAIT = 2 * 3600.0         # a file verified this recently waits for libtorrent's saved hash tree
_HEX64 = re.compile(r"[0-9a-f]{64}")


class PerFileError(ValueError):
    pass


@dataclass
class Want:
    root: str
    ih: str                     # the model torrent's v1 infohash
    index: int                  # the file's index in it
    path: Path
    size: int
    piece_length: int           # the model torrent's
    verified_at: float
    repo: str


@dataclass
class Companion:
    root: str
    ih: str
    index: int
    v2: str
    handle: object


def build(raw: bytes, root: str, size: int, v2: str, layer_for, trackers: tuple[str, ...] = DEFAULT_TRACKER_HOSTS
          ) -> bytes:
    """The per-file .torrent to seed: Pirate Face's copy checked, with the piece layer that
    `layer_for(piece_length)` supplies, allowlisted trackers only and no web seeds."""
    try:
        d = lt.bdecode(raw)
    except RuntimeError as e:
        raise PerFileError(f"unreadable torrent ({e})") from e
    info = d.get(b"info") if isinstance(d, dict) else None
    if not isinstance(info, dict) or info.get(b"meta version") != 2 or b"pieces" in info or b"file tree" not in info:
        raise PerFileError("not a pure BitTorrent v2 torrent")
    plen = info.get(b"piece length")
    if not isinstance(plen, int) or plen < BLOCK or plen & (plen - 1):
        raise PerFileError("bad piece length")
    try:
        ti = lt.torrent_info({b"info": info})
    except RuntimeError as e:
        raise PerFileError(f"libtorrent won't load it ({e})") from e
    if str(ti.info_hashes().v2) != v2:
        raise PerFileError("its infohash isn't the one Pirate Face named")
    fs = ti.files()
    real = [i for i in range(fs.num_files()) if not fs.file_flags(i) & fs.flag_pad_file]
    if len(real) != 1 or str(fs.root(real[0])) != root or fs.file_size(real[0]) != size:
        raise PerFileError("it isn't exactly this one file")
    top: dict = {b"info": info}
    tiers = []
    for tier in d.get(b"announce-list") or ([[d[b"announce"]]] if d.get(b"announce") else []):
        ok = [t for t in tier if isinstance(t, bytes) and _tracker_ok(t, trackers)]
        if ok:
            tiers.append(ok)
    if not tiers:
        raise PerFileError("it names no tracker Flotilla announces to")
    top[b"announce"] = tiers[0][0]
    top[b"announce-list"] = tiers
    if needs_layer(size, plen):
        layer = layer_for(plen)
        if root_from_layer(layer, plen) != bytes.fromhex(root):
            raise PerFileError("the piece layer doesn't hash up to the file's root")
        top[b"piece layers"] = {bytes.fromhex(root): layer}
    out = lt.bencode(top)
    if str(lt.torrent_info(lt.bdecode(out)).info_hashes().v2) != v2:    # nothing in info changed
        raise PerFileError("rebuilding it changed its infohash")
    return out


def _tracker_ok(t: bytes, hosts: tuple[str, ...]) -> bool:
    from urllib.parse import urlsplit

    try:
        p = urlsplit(t.decode())
    except UnicodeDecodeError:
        return False
    return p.scheme in ("udp", "http", "https") and bool(p.hostname) and host_allowed(p.hostname, hosts)


def companion_params(raw: bytes, path: Path, scratch: Path) -> lt.add_torrent_params:
    """Seed `raw` from the file at `path`: seed mode (pieces are checked as they're asked for, not
    all up front) and upload mode (it never downloads, so never writes)."""
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(lt.bdecode(raw))
    fs = atp.ti.files()
    index = next(i for i in range(fs.num_files()) if not fs.file_flags(i) & fs.flag_pad_file)
    atp.save_path = str(scratch)
    atp.renamed_files = {index: str(path.resolve())}
    atp.flags = (atp.flags | lt.torrent_flags.seed_mode | lt.torrent_flags.upload_mode) \
        & ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
    return atp


class PerFile:
    """The node's per-file swarms. The worker thread prepares torrents; the main loop (tick, drop)
    adds and removes them, like every other session change."""

    def __init__(self, eng):
        self.eng = eng
        self.dir = eng.state.dir / "per-file"
        self.joined: dict[str, Companion] = {}
        self.ready: queue.Queue = queue.Queue()
        self.wake = threading.Event()
        rec = eng.state.data.setdefault("per_file", {})
        self.saved: dict[str, dict] = rec.setdefault("joined", {})     # root -> {ih, i, v2}
        self.retry: dict[str, float] = rec.setdefault("retry", {})      # root -> when to look again
        self.unlisted: dict[str, float] = rec.setdefault("unlisted", {})  # root -> when Pirate Face said so
        self._roots: dict[str, tuple[int, dict[int, str]]] = {}         # ih -> (piece length, {index: root})
        self.busy: str | None = None       # what the worker is hashing, for the dashboard
        self.waiting = 0
        self._last_check = 0.0
        self.client = lambda: PirateFace(tuple(eng.cfg.allowed_hosts))    # tests put a fake here

    @property
    def enabled(self) -> bool:
        return bool(self.eng.cfg.per_file_swarms)

    # ------------------------------------------------------------ worker thread

    def _model_roots(self, ih: str) -> tuple[int, dict[int, str]]:
        hit = self._roots.get(ih)
        if hit is None:
            try:
                ti = lt.torrent_info(lt.bdecode((self.eng.torrent_dir / f"{ih}.torrent").read_bytes()))
                fs = ti.files()
                v2 = not ti.info_hashes().v2.is_all_zeros()
                roots = {i: str(fs.root(i)) for i in range(fs.num_files())} if v2 else {}
                hit = (ti.piece_length(), {i: r for i, r in roots.items() if _HEX64.fullmatch(r) and set(r) != {"0"}})
            except (OSError, RuntimeError):
                hit = (0, {})
            self._roots[ih] = hit
        return hit

    def wanted(self) -> list[Want]:
        """Verified files of 1 MiB or more in hybrid torrents, one per pieces root."""
        with self.eng.lock:
            holds = {ih: (h["repo"], Path(h["save_path"]), [(int(k), dict(e)) for k, e in h["files"].items()])
                     for ih, h in self.eng.state.holdings.items()}
        for ih in set(self._roots) - set(holds):
            self._roots.pop(ih, None)
        out: dict[str, Want] = {}
        for ih, (repo, base, files) in holds.items():
            plen, roots = self._model_roots(ih)
            for i, e in files:
                root = roots.get(i)
                if root and e["status"] == "verified" and e["size"] >= MIN_SIZE and root not in out:
                    out[root] = Want(root, ih, i, base / e["tpath"], e["size"], plen,
                                     e.get("verified_at") or 0.0, repo)
        return list(out.values())

    def run(self) -> None:
        stop = self.eng.stop
        while not stop.is_set():
            if self.enabled:
                try:
                    self.one_pass(stop)
                except Exception as e:     # never let a surprise end the thread
                    self.eng.log(f"per-file swarms: {e}")
            self.wake.wait(PASS_EVERY)
            self.wake.clear()

    def one_pass(self, stop: threading.Event | None = None) -> None:
        now = time.time()
        todo = [w for w in self.wanted()
                if w.root not in self.joined and w.root not in self.saved and self.retry.get(w.root, 0) <= now]
        self.waiting = len(todo)
        pf = self.client()       # a fresh client each pass: its back-off lasts one pass
        for w in todo:
            if stop is not None and stop.is_set():
                return
            try:
                self.prepare(w, pf, stop)
            except FetchError as e:
                self._later(w.root, ERROR_RETRY)
                if e.status is None or e.status == 429 or e.status >= 500:
                    self.eng.log(f"per-file swarms: couldn't reach Pirate Face ({e}); trying again later")
                    return
            except (PerFileError, OSError) as e:
                self._later(w.root, ERROR_RETRY)
                self.eng.log(f"per-file swarms: skipping {w.repo}/{w.path.name} for now: {e}")
            self.waiting = max(0, self.waiting - 1)
            if stop is not None and stop.wait(LOOKUP_GAP):
                return

    def prepare(self, w: Want, pf: PirateFace, stop: threading.Event | None = None) -> bool:
        """Look up one file's swarm and queue its checked torrent for the main loop. False if
        Pirate Face has none (or it has to wait for libtorrent's saved hash tree)."""
        entry = pf.file_swarm(w.root)
        if not entry:
            self._later(w.root, UNLISTED_RETRY, unlisted=True)
            return False
        self._later(w.root, 0)
        v2 = str(entry.get("infohashV2") or "")
        if entry.get("root") != w.root or not _HEX64.fullmatch(v2) or entry.get("length") not in (None, w.size):
            raise PerFileError("Pirate Face's answer doesn't match the file")
        raw = pf.file_torrent(str(entry.get("torrent") or ""))
        def layer_for(plen: int) -> bytes:
            try:
                saved = (self.eng.resume_dir / f"{w.ih}.fastresume").read_bytes()
            except OSError:
                saved = b""
            got = layer_from_resume(saved, w.index, w.size, plen, bytes.fromhex(w.root), w.piece_length) \
                if saved else None
            if got:
                return got
            if time.time() - w.verified_at < FRESH_WAIT:
                raise _Later()
            self.busy = f"{w.repo}/{w.path.name}"
            try:
                root, got = hash_file(w.path, w.size, plen, stop)
            finally:
                self.busy = None
            if root.hex() != w.root:
                raise PerFileError("the file on disk doesn't hash to its root")
            return got

        try:
            out = build(raw, w.root, w.size, v2, layer_for)
        except _Later:
            self._later(w.root, 1800)
            return False
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f"{w.root}.tmp"
        tmp.write_bytes(out)
        tmp.replace(self.dir / f"{w.root}.torrent")
        self.ready.put((w.root, w.ih, w.index, v2))
        return True

    def _later(self, root: str, seconds: float, unlisted: bool = False) -> None:
        with self.eng.lock:          # state.json is written under the same lock
            if seconds:
                self.retry[root] = time.time() + seconds
            else:
                self.retry.pop(root, None)
            if unlisted:
                self.unlisted[root] = time.time()
            else:
                self.unlisted.pop(root, None)

    # ------------------------------------------------------------ main loop

    def resume(self) -> None:
        """Queue the per-file swarms joined before a restart (their torrents are stored)."""
        for root, rec in list(self.saved.items()):
            self.ready.put((root, rec["ih"], rec["i"], rec["v2"]))

    def tick(self, now: float) -> None:
        """Add the torrents the worker prepared, and take out any whose file is no longer verified."""
        added: dict[str, int] = {}
        while True:
            try:
                root, ih, index, v2 = self.ready.get_nowait()
            except queue.Empty:
                break
            repo = self._join(root, ih, index, v2)
            if repo:
                added[repo] = added.get(repo, 0) + 1
        for repo, n in added.items():
            self.eng.log(f"per-file swarms: seeding {n} file(s) of {repo} in their own swarms too")
        if now - self._last_check >= 60:
            self._last_check = now
            self._check()

    def _file(self, ih: str, index: int) -> tuple[dict, dict] | None:
        hold = self.eng.state.holdings.get(ih)
        e = hold["files"].get(str(index)) if hold else None
        return (hold, e) if e and e["status"] == "verified" else None

    def _join(self, root: str, ih: str, index: int, v2: str) -> str | None:
        if not self.enabled or root in self.joined:
            return None
        with self.eng.lock:
            found = self._file(ih, index)
        tfile = self.dir / f"{root}.torrent"
        if not found:
            with self.eng.lock:
                self.saved.pop(root, None)
            tfile.unlink(missing_ok=True)
            return None
        hold, e = found
        path = Path(hold["save_path"]) / e["tpath"]
        try:
            if path.stat().st_size != e["size"]:
                raise OSError("size changed")
            atp = companion_params(tfile.read_bytes(), path, self.dir / "unused")
            if str(atp.ti.info_hashes().v2) != v2:
                raise RuntimeError("stored torrent doesn't match")
            h = self.eng.ses.add_torrent(atp)
        except (OSError, RuntimeError, StopIteration) as err:
            with self.eng.lock:
                self.saved.pop(root, None)
            tfile.unlink(missing_ok=True)
            self._later(root, ERROR_RETRY)
            self.eng.log(f"per-file swarms: couldn't seed {hold['repo']}/{e['path']} in its own swarm ({err})")
            return None
        for addr in self.eng.connect:
            h.connect_peer(addr)
        self.eng.add_seeding_tracker(h)
        self.joined[root] = Companion(root, ih, index, v2, h)
        with self.eng.lock:
            self.saved[root] = {"ih": ih, "i": index, "v2": v2}
            self.eng._dirty = True
        return hold["repo"]

    def drop(self, ih: str, indices: set[int] | None = None, forget: bool = True) -> int:
        """Take out the per-file swarms of these files (all of the model's when indices is None),
        before the files themselves go. Never deletes anything but our stored torrents."""
        n = 0
        for root, c in list(self.joined.items()):
            if c.ih == ih and (indices is None or c.index in indices):
                self.eng.ses.remove_torrent(c.handle)
                del self.joined[root]
                self.eng._st.pop(f"v2:{root}", None)
                self.eng._up_seen.pop(f"v2:{root}", None)
                n += 1
        if forget:             # turned off, they're kept for when it's on again
            with self.eng.lock:
                for root, rec in list(self.saved.items()):
                    if rec["ih"] == ih and (indices is None or rec["i"] in indices):
                        del self.saved[root]
                        (self.dir / f"{root}.torrent").unlink(missing_ok=True)
        if indices is None:
            self._roots.pop(ih, None)
        return n

    def _check(self) -> None:
        """Take out per-file swarms whose file is no longer verified (or that libtorrent stopped with
        an error), all of them while the feature is off, and forget retry times that have passed and
        "not listed" marks for files no longer held (they'd stay in state.json, and in the count, for good)."""
        now = time.time()
        live = {w.root for w in self.wanted()} if self.unlisted else set()
        with self.eng.lock:
            gone = [c for c in self.joined.values() if not self.enabled or not self._file(c.ih, c.index)]
            for r in [r for r, t in self.retry.items() if t <= now]:
                del self.retry[r]
            for r in [r for r in self.unlisted if r not in live]:
                del self.unlisted[r]
        for c in self.joined.values():
            st = self.eng.status(f"v2:{c.root}", c.handle)
            if st.errc.value():
                self.eng.log(f"per-file swarms: stopped seeding a file in its own swarm ({st.errc.message()})")
                self._later(c.root, ERROR_RETRY)
                gone.append(c)
        for c in gone:
            if c.root in self.joined:
                self.drop(c.ih, {c.index}, forget=self.enabled)

    def handles(self) -> list[tuple[str, str, object]]:
        """(status key, model infohash, handle) of every per-file swarm, for upload accounting."""
        return [(f"v2:{c.root}", c.ih, c.handle) for c in self.joined.values()]

    def count(self, ih: str) -> int:
        return sum(1 for c in self.joined.values() if c.ih == ih)

    def info(self) -> dict:
        return {"enabled": self.enabled, "joined": len(self.joined), "waiting": self.waiting,
                "unlisted": len(self.unlisted), "hashing": self.busy}


class _Later(Exception):
    """The file was verified moments ago: wait for libtorrent's saved hash tree before hashing it."""
