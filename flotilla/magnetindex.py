"""Pirate Face's magnet index: kept and seeded by every node, and used while Pirate Face is down.

Pirate Face publishes every listed model's magnet as a torrent of its own, `pirateface/magnet-index`:
weekly shards (`shards/YYYY-MM-DD.txt`, one magnet per line) and, from a build that drops something,
`shards/removed-YYYY-MM-DD.txt` (one v1 infohash per line). Each build is a new torrent holding every
earlier shard byte for byte. A copy outlives the site, so:

* Once a day, while Pirate Face is up, the node fetches the newest build's .torrent like any other
  listing, checks it holds nothing but those files (and is small), and seeds it. A new build added
  over the old one's folder only downloads the new shards.
* While Pirate Face can't be reached, a model's magnet comes from this copy (`listing`), and its
  .torrent from the swarm (`metadata_from_swarm`): libtorrent checks the metadata against the
  infohash. Everything after that is unchanged: every file must match Hugging Face, or the hash
  ledger, before anything is downloaded, and the signed list's pins still apply.

Shards are read the way Pirate Face's own CLI reads them: in date order, each build's removals applied
right after its shard, the last line for a model winning. A removals file that can't be read makes
the whole copy answer nothing (the safe side: never bring back a torrent Pirate Face removed).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
import shutil
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import libtorrent as lt

from .magnet import MagnetError, parse_magnet, validate_magnet
from .torrentmeta import TorrentError, TorrentMeta, parse_torrent

INDEX_REPO = "pirateface/magnet-index"
NAME = "magnet-index"
FILE = re.compile(r"LICENSE|shards/(removed-)?\d{4}-\d{2}-\d{2}\.txt")
_SHARD = re.compile(r"(\d{4}-\d{2}-\d{2})\.txt")
_REMOVED = re.compile(r"removed-(\d{4}-\d{2}-\d{2})\.txt")
_HASH = re.compile(r"[0-9a-fA-F]{40}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
MAX_BYTES = 256 << 20          # the 2026-11 builds are a few MB
MAX_LINE = 16 << 10            # a magnet line is a few hundred characters
MAX_LINES = 1_000_000          # per shard or removals file; the whole catalog is tens of thousands
_TORRENT = re.compile(r"[0-9a-f]{40}\.torrent")
_PARTS = re.compile(r"\.[0-9a-f]{40}\.parts")
SYNC_EVERY = 86400.0
SWARM_METADATA_SECONDS = 120.0


class IndexError_(ValueError):
    pass


class Unavailable(Exception):
    """The swarm didn't give the torrent's metadata in time."""


@dataclass(frozen=True)
class Entry:
    magnet: str
    infohash: str
    revision: str
    build: str


def check_build(meta: TorrentMeta) -> None:
    """A magnet index build holds the license and shards, nothing else, and is small."""
    if meta.name != NAME:
        raise IndexError_(f"not the magnet index (named {meta.name!r})")
    files = meta.real_files()
    if not files or not all(FILE.fullmatch(f.repo_path) for f in files):
        raise IndexError_("it holds files other than the license and shards")
    if meta.total_size > MAX_BYTES:
        raise IndexError_("it's far larger than a magnet index")


def entry_of(line: str, build: str) -> tuple[str, Entry] | None:
    """(repo, Entry) for one shard line, or None if it isn't a magnet Flotilla would follow: every
    tracker and web seed on the allowlist, exactly one web seed naming owner/name/commit."""
    try:
        m = validate_magnet(parse_magnet(line.strip()))
    except MagnetError:
        return None
    if len(m.web_seeds) != 1:
        return None
    parts = [p for p in urllib.parse.urlsplit(m.web_seeds[0]).path.split("/") if p]
    if len(parts) < 5 or parts[:2] != ["api", "ws"] or not _COMMIT.fullmatch(parts[4]):
        return None
    repo = f"{parts[2]}/{parts[3]}"
    if not _REPO.fullmatch(repo):
        return None
    return repo, Entry(line.strip(), m.btih, parts[4], build)


class _Unreadable(Exception):
    pass


def _lines(path: Path):
    """A file's lines, read as a stream (a shard may be up to MAX_BYTES), a line longer than MAX_LINE
    as None. A missing file has none. _Unreadable if it can't be read or has more than MAX_LINES."""
    try:
        f = path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    except OSError as e:
        raise _Unreadable(str(e)) from e
    with f:
        try:
            for _ in range(MAX_LINES):
                line = f.readline(MAX_LINE + 1)
                if not line:
                    return
                if len(line) > MAX_LINE and not line.endswith("\n"):
                    while line and not line.endswith("\n"):     # the rest of it, never held whole
                        line = f.readline(MAX_LINE + 1)
                    yield None
                    continue
                yield line
            if f.readline(1):
                raise _Unreadable(f"more than {MAX_LINES} lines")
        except OSError as e:
            raise _Unreadable(str(e)) from e


def parse_shards(folder: Path) -> dict[str, Entry] | None:
    """{repo: Entry} from a build's shards folder, or None if a removals file can't be trusted."""
    try:
        names = [p.name for p in folder.iterdir()]
    except OSError:
        return {}
    dates = sorted({m[1] for n in names for m in [_SHARD.fullmatch(n) or _REMOVED.fullmatch(n)] if m})
    found: dict[str, Entry] = {}
    for date in dates:
        gone = set()
        try:
            for raw in _lines(folder / f"removed-{date}.txt"):
                text = raw.strip() if raw is not None else "-"     # an overlong line isn't a hash
                if not text:
                    continue
                if not _HASH.fullmatch(text):
                    return None          # a stray character must not quietly drop a removal
                gone.add(text.lower())
        except _Unreadable:
            return None
        try:
            for line in _lines(folder / f"{date}.txt"):
                got = entry_of(line, date) if line and line.strip() else None
                if got:
                    found[got[0]] = got[1]
        except _Unreadable:
            pass                         # what was read of it stands, as a missing shard adds nothing
        for repo in [r for r, e in found.items() if e.infohash in gone]:
            del found[repo]
    return found


def metadata_from_swarm(magnet: str, session_settings: dict, scratch: Path, connect=(),
                        seconds: float = SWARM_METADATA_SECONDS) -> bytes:
    """The .torrent for a magnet, from its swarm (BEP 9): the info dict as peers send it, which
    libtorrent checks against the infohash, with the magnet's trackers. Raises Unavailable."""
    m = validate_magnet(parse_magnet(magnet))
    ses = lt.session(session_settings)
    try:
        atp = lt.parse_magnet_uri(magnet)
        atp.url_seeds = []
        atp.save_path = str(scratch)
        atp.flags = (atp.flags | lt.torrent_flags.upload_mode) & ~(lt.torrent_flags.auto_managed
                                                                     | lt.torrent_flags.paused)
        h = ses.add_torrent(atp)
        for addr in connect:
            h.connect_peer(addr)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ses.pop_alerts()
            if h.status().has_metadata:
                info = lt.bdecode(h.torrent_file().metadata())
                tiers = [[t.encode()] for t in m.trackers]
                top: dict = {b"info": info}
                if tiers:
                    top[b"announce"], top[b"announce-list"] = tiers[0][0], tiers
                raw = lt.bencode(top)
                if parse_torrent(raw).infohash_v1 != m.btih:
                    raise Unavailable("the metadata doesn't match the magnet")
                return raw
            time.sleep(0.2)
        raise Unavailable(f"no peer sent its metadata within {seconds:.0f}s")
    except (RuntimeError, TorrentError) as e:
        raise Unavailable(str(e)) from e
    finally:
        del ses
        shutil.rmtree(scratch, ignore_errors=True)


class MagnetIndex:
    """This node's copy of the index. sync() and the lookups run on the refresh thread; tick() adds
    and swaps the build's torrent on the main loop."""

    def __init__(self, eng):
        self.eng = eng
        self.dir = eng.state.dir / "index"
        self.files = self.dir / "files"
        self.rec = eng.state.data.setdefault("magnet_index", {})     # infohash, build, models, synced
        self.handle = None
        self.old = None               # the previous build's handle, until the new one is complete
        self._pending: str | None = None
        self._lock = threading.Lock()
        self.entries: dict[str, Entry] | None = None
        self._load_map()

    @property
    def enabled(self) -> bool:
        return bool(self.eng.cfg.keep_magnet_index)

    def _load_map(self) -> None:
        try:
            doc = json.loads((self.dir / "map.json").read_text())
            self.entries = {r: Entry(*e) for r, e in doc["entries"].items()}
        except (OSError, ValueError, KeyError, TypeError):
            self.entries = None

    # ------------------------------------------------------------ refresh thread

    def sync(self, pf, now: float | None = None) -> None:
        """Fetch the newest build if Pirate Face lists a new one (at most daily)."""
        now = time.time() if now is None else now
        if not self.enabled or now - self.rec.get("synced", 0) < SYNC_EVERY:
            return
        # Marked synced only once the listing and a new build's .torrent have both come: a FetchError
        # (Pirate Face down, a reply cut off) on either means trying again at the next refresh.
        info = pf.torrent(INDEX_REPO)
        if info is not None and not isinstance(info, dict):
            self.eng.log("magnet index: Pirate Face's listing of it isn't in the usual shape; asking again tomorrow")
            info = None
        ih = (info or {}).get("infohash")
        ih = ih.lower() if isinstance(ih, str) else ""
        if not re.fullmatch(r"[0-9a-f]{40}", ih) or ih == self.rec.get("infohash"):
            self._synced(now)
            return
        raw = pf.torrent_file(ih)
        try:
            meta = parse_torrent(raw)
            if meta.infohash_v1 != ih:
                raise IndexError_("its .torrent doesn't hash to the listed infohash")
            check_build(meta)
            from .candidates import check_urls

            check_urls(meta.trackers, meta.web_seeds)
        except (TorrentError, MagnetError, IndexError_, ValueError) as e:
            self.eng.log(f"magnet index: not keeping build {ih[:12]}: {e}")
            self._synced(now)
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / f"{ih}.torrent.tmp"
            tmp.write_bytes(raw)
            tmp.replace(self.dir / f"{ih}.torrent")
        except OSError as e:
            self.eng.log(f"magnet index: couldn't save build {ih[:12]} ({e}); trying again next refresh")
            return
        with self._lock:
            self._pending = ih
        self._synced(now)

    def _synced(self, now: float) -> None:
        with self.eng.lock:
            self.rec["synced"] = now

    def listing(self, repo: str) -> dict | None:
        """A listing for the repo, in /api/torrents' shape, from this copy; None if it doesn't list it."""
        e = (self.entries or {}).get(repo) if self.enabled else None
        if e is None:
            return None
        return {"repoId": repo, "magnet": e.magnet, "infohash": e.infohash, "revision": e.revision,
                "files": [], "hf": None, "seeders": 0, "leechers": 0, "index_build": e.build}

    def torrent(self, magnet: str) -> bytes:
        from .engine import PROBE_PREFIX, session_settings

        e = self.eng
        settings = session_settings(e.cfg, port=0, fingerprint=PROBE_PREFIX, listen_host=e.listen_host,
                                    dht=e.dht, probe=True)
        return metadata_from_swarm(magnet, settings, e.cfg.folder / ".metadata", e.connect)

    # ------------------------------------------------------------ main loop

    def resume(self) -> None:
        ih = self.rec.get("infohash")
        if self.enabled and ih and (self.dir / f"{ih}.torrent").exists():
            with self._lock:
                self._pending = ih
            self.prune({ih})        # what earlier runs left: old builds' .torrent files and shards

    # ------------------------------------------------------------ old builds' files

    def _build_files(self, ih: str) -> set[str] | None:
        """The paths under files/ that build `ih` holds, from its .torrent; None if it can't be read."""
        try:
            meta = parse_torrent((self.dir / f"{ih}.torrent").read_bytes())
        except (OSError, TorrentError, ValueError):
            return None
        return {f"{NAME}/{f.repo_path}" for f in meta.real_files()}

    def _unlink(self, path: Path) -> int:
        """Delete one file, only if it sits in the index folder with no symlinked folder on the way.
        A name that is itself a link loses only the link: nothing is followed."""
        try:
            base = os.path.realpath(self.dir)
            parent = os.path.realpath(path.parent)
            st = os.lstat(path)
            if not (parent == base or parent.startswith(base + os.sep)):
                return 0
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                return 0
            os.unlink(path)
            return 1
        except OSError:
            return 0

    def prune(self, keep: set[str]) -> int:
        """Delete what only builds other than `keep` (infohashes) held: their .torrent files, their
        shards and libtorrent's part files. A build holds every earlier shard, so a shard still in a
        kept build stays; if a kept build's file list can't be read, nothing in files/ is deleted."""
        removed = 0
        try:
            names = [p.name for p in self.dir.iterdir()]
        except OSError:
            return 0
        for n in names:
            if (_TORRENT.fullmatch(n) and n[:40] not in keep) or n.endswith(".torrent.tmp"):
                removed += self._unlink(self.dir / n)
        lists = [self._build_files(ih) for ih in keep]
        if lists and all(x is not None for x in lists):
            wanted = set().union(*lists)
            for sub in ("", NAME, f"{NAME}/shards"):
                try:
                    found = [p.name for p in (self.files / sub).iterdir()]
                except OSError:
                    continue
                for n in found:
                    rel = f"{sub}/{n}".lstrip("/")
                    if sub == "":
                        stale = bool(_PARTS.fullmatch(n)) and n[1:41] not in keep
                    else:
                        stale = bool(FILE.fullmatch(rel[len(NAME) + 1:])) and rel not in wanted
                    if stale:
                        removed += self._unlink(self.files / rel)
        if removed:
            self.eng.log(f"magnet index: deleted {removed} file(s) that only older builds held")
        return removed

    def tick(self) -> None:
        if not self.enabled:
            if self.handle is not None or self.old is not None:
                for h in (self.handle, self.old):
                    if h is not None:
                        self.eng.ses.remove_torrent(h)
                self.handle = self.old = None
            return
        with self._lock:
            ih, self._pending = self._pending, None
        if ih:
            self._add(ih)
        if self.handle is not None and self.eng.status("index", self.handle).is_seeding:
            if self.old is not None:          # the new build has every shard: let the old one go
                self.eng.ses.remove_torrent(self.old)
                self.old = None
                self.prune({str(self.handle.info_hashes().v1)})
            if self.rec.get("parsed") != self.rec.get("infohash"):
                self._parse()

    def _add(self, ih: str) -> None:
        if self.handle is not None and str(self.handle.info_hashes().v1) == ih:
            return
        try:
            atp = lt.add_torrent_params()
            d = lt.bdecode((self.dir / f"{ih}.torrent").read_bytes())
            for k in (b"url-list", b"httpseeds"):
                d.pop(k, None)
            atp.ti = lt.torrent_info(d)
            atp.save_path = str(self.files)
            atp.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
            h = self.eng.ses.add_torrent(atp)
        except (OSError, RuntimeError) as e:
            self.eng.log(f"magnet index: couldn't add build {ih[:12]} ({e})")
            return
        for addr in self.eng.connect:
            h.connect_peer(addr)
        self.eng.add_seeding_tracker(h)
        dropped = self.old is not None
        if dropped:                           # a third build before the second was complete
            self.eng.ses.remove_torrent(self.old)
        self.old, self.handle = self.handle, h
        if dropped:
            self.prune({str(x.info_hashes().v1) for x in (self.old, self.handle) if x is not None})
        with self.eng.lock:
            self.rec["infohash"] = ih

    def _parse(self) -> None:
        found = parse_shards(self.files / NAME / "shards")
        ih = self.rec.get("infohash")
        with self.eng.lock:
            self.rec["parsed"] = ih
        if found is None:
            self.entries = {}
            self.eng.log("magnet index: a removals list can't be read, so this copy isn't used until the next build")
            return
        self.entries = found
        build = max((e.build for e in found.values()), default="?")
        with self.eng.lock:
            self.rec["build"], self.rec["models"] = build, len(found)
        try:
            tmp = self.dir / "map.tmp"
            tmp.write_text(json.dumps({"infohash": ih, "entries": {r: list(dataclasses.astuple(e))
                                                                   for r, e in found.items()}}))
            tmp.replace(self.dir / "map.json")
        except OSError:
            pass
        self.eng.log(f"magnet index: keeping and seeding build {build} ({len(found)} models), "
                     f"used if Pirate Face can't be reached")

    def handles(self) -> list[tuple[str, str, object]]:
        return [(f"index:{h.info_hashes().v1}", str(h.info_hashes().v1), h)
                for h in (self.handle, self.old) if h is not None]

    def info(self) -> dict:
        return {"enabled": self.enabled, "build": self.rec.get("build"), "models": self.rec.get("models"),
                "seeding": bool(self.handle is not None and self.eng.status("index", self.handle).is_seeding)}
