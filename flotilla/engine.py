"""libtorrent engine: probe swarms, apply plans, seed, verify.

Two libtorrent sessions:

* main  - long-lived; holds the files the planner picked and uploads them.
* probe - short-lived, created per refresh. It joins every candidate swarm at
          4 KB/s wanting one small file. That keeps peers
          connected long enough to read their piece bitfields (a session that
          wants nothing is dropped as an "upload to upload connection" before
          it learns anything). It downloads almost nothing, writes into its own
          scratch folder and deletes it afterwards. Our own main session is
          excluded by its per-node peer-id prefix, so we never count ourselves.

Everything needed to keep seeding after a restart lives on disk under the state
dir: the verified .torrent, resume data, and per-file expected hashes.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import ipaddress
import json
import math
import os
import queue
import random
import re
import shutil
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import libtorrent as lt

from . import USER_AGENT, __version__, netsafe, sweep
from .candidates import Rejection, TorrentCand, gather
from .config import (BUILTIN_EDITOR_KEYS, FOCUS_CHOICES, REORIENT_CHOICES, REORIENT_EVERY, Config, State, memory_budget,
                     own_memory)
from .hfpieces import HFPieces
from .history import History
from .ledger import Ledger, load_ledger
from .remote import WRITE_EVERY, snapshot_wanted, take_requests, write_snapshot
from .feed import (BUNDLED_FEED, REPO_RE, Feed, FeedError, add_pick, apply_local, list_picks, load_feed, origin_view,
                   parse_model_ref, rank_priority, read_allot, read_order, remove_pick, set_allot, set_order)
from .netinfo import address_present
from .magnetindex import INDEX_REPO, MagnetIndex
from .perfile import PerFile
from .vpnport import RESTART_ROUTE, Forward, GluetunAbsent, GluetunAuth, PortFinder, gluetun_restart_vpn
from .hfauth import remove_token, save_secret
from .pfaccount import Account, clean_handle, clean_key, key_path, kind_of
from .planner import GB, Params, Plan, Unit, list_full, plan as make_plan, set_targets, swappable
from .signing import PublicKey, SignatureError
from .sources import HuggingFace, PirateFace, git_blob_sha1, sha256_file
from .torrentmeta import parse_torrent
from .v2hash import NO_TREE, piece_layer_tree

PROBE_RATE = 4096          # bytes/s per probed torrent
PROBE_PREFIX = "-FP0100-"
# Peer ids of Flotilla nodes (-FL0100-<node>) and probes (-FP0100-), whatever the version: they hold
# pieces to seed them, so they're never demand, even while Pirate Face's tracker calls them leechers.
FLOTILLA_PEER = re.compile(rb"^-F[LP]\d{4}-")
SETTLE = 3.0    # seconds a probe watches a peer before counting it as a downloader
STATUS_EVERY = 10.0
SAVE_EVERY = 300.0          # state.json is written this often (and at once after real changes)
STATUS_FILE_EVERY = 60.0    # status.json (for `flotilla status`) is written this often
# Torrent status reads. libtorrent's default asks for everything (piece bitfields, distributed
# copies...), work that grows with the piece count and runs on its network thread. Only the
# accurate byte counters are used here, and one read per torrent is shared for STATUS_MAX_AGE.
STATUS_FLAGS = lt.torrent_handle.query_accurate_download_counters
STATUS_MAX_AGE = 2.0
SNAPSHOT_FRESH = 1.5        # seconds one dashboard snapshot is shared between requests (several tabs)
UPLOAD_PEERS_MAX = 100_000  # distinct peers remembered per run (the count shows "at least" past it)
SAMPLE_EVERY = 5.0   # history thread wake-ups; sample() itself records once per clock minute
# Reachable: a peer started a connection to this node within REACHABLE_FOR, or the VPN forwards it a
# port. A node nobody can reach only ever talks to peers it dials itself, so it dials harder
# (DIAL_OUT, in place of libtorrent's defaults) and announces every DIAL_OUT_ANNOUNCE (the tracker's
# own minimum interval still holds), to hear about new leechers sooner. uTP and PEX stay on: they're
# what libtorrent's hole punching (BEP 55) needs to link two nodes neither of which can be reached.
REACHABLE_FOR = 3 * 86400.0
# vpn_port_recover: how long a lost port waits before gluetun is asked to reconnect, and the wait
# between tries, doubling from the first to the last while no port comes back.
RECOVER_AFTER = 600.0
RECOVER_EVERY = (3600.0, 86400.0)
# It also keeps connections to other seeds, which libtorrent drops as useless ("upload to upload"): a
# seed that stays connected can tell it about leechers it can't reach (PEX) and introduce the two for
# hole punching. Most clients still hang up from their side (Pirate Face's own seeder does); other
# unreachable Flotilla nodes keep the connection too.
DIAL_OUT = {"connection_speed": 60, "torrent_connect_boost": 60, "peer_connect_timeout": 10,
            "seeding_outgoing_connections": True, "enable_outgoing_utp": True, "enable_incoming_utp": True,
            "close_redundant_connections": False}
# A peer that connected in only counts once it has traded at least a block: DHT crawlers connect in
# with made-up infohashes and leave, and they say nothing about whether real peers can reach us.
INBOUND_MIN_BYTES = 16 * 1024
DIAL_OUT_ANNOUNCE = 15 * 60.0
STUCK_FOR = 86400.0   # a torrent Hugging Face wouldn't serve stays out of plans this long (see _stuck)
REPICK_AFTER = 3 * 86400.0   # a file a reorientation swapped out isn't picked again for this long (_cooling)
RESUME_EVERY = 30 * 60.0
# Refreshes in a row Pirate Face must list no torrent for a held model before its copy is dropped:
# one odd answer shouldn't delete a model (a takedown that must act at once goes on the feed's deny list).
DELIST_GRACE = 3
# A node that already holds files waits a random while before its first look at the swarm after
# it starts, seeding what it has meanwhile: many nodes restarted together (a host reboot, an
# update) would otherwise all ask Pirate Face and probe every swarm in the same minutes.
STARTUP_SPREAD = 30 * 60.0
# Each node's reorientation period is its own, within this share of the chosen one (by node id),
# so nodes that started together drift apart instead of reorienting in lockstep for good.
REORIENT_SPREAD = 0.1
# A file that couldn't be read while verifying (a flaky disk, a permission, too many open files) says
# nothing about the torrent: it's checked again, after VERIFY_QUICK quick looks, at VERIFY_RETRY
# seconds, doubling up to the last.
VERIFY_QUICK = 2
VERIFY_RETRY = (30.0, 3600.0)
# A torrent libtorrent stopped on a disk error (a full or read-only disk, a vanished file) is started
# again after the first wait, doubling up to the second while it keeps failing.
TORRENT_RETRY = (60.0, 3600.0)
# One failing housekeeping step (a full disk under state.json, say) is logged at most this often.
STEP_LOG_EVERY = 600.0
# libtorrent's rate limits are C ints: 2**31 B/s and up wrap round (to 0, unlimited, or to almost nothing).
RATE_MAX = 2**31 - 1


def _rate_limit(bps: float) -> int:
    """A rate in bytes/s as libtorrent takes it: at least 1 B/s, at most RATE_MAX."""
    try:
        return min(RATE_MAX, max(1, int(bps)))
    except (ValueError, OverflowError):      # nan, inf: no usable limit was given
        return RATE_MAX


def _finite(v) -> bool:
    """A real number from the dashboard: not a bool, not NaN or infinite (JSON can't carry those back)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _write_private(path: Path, data: bytes) -> None:
    """Write `path` whole (a temporary file, then a rename) readable by this user only. Resume data
    holds the trackers, and the account's seeding tracker address carries its private key."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)            # made fresh below, so never readable by others, even briefly
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _write_atomic(path: Path, data: bytes) -> None:
    """Write `path` whole: a crash or a full disk leaves the old file, or none, never a short one."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def unblock(data: dict, target: str | None) -> list[str]:
    """Take torrents off the blocklist in state data: one by infohash, every torrent of a model by
    its name (author/model), or all of them (target None). Returns the infohashes taken off."""
    block = data.setdefault("blocklist", [])
    why = data.setdefault("blocked", {})
    if target is None:
        gone = list(block)
    else:
        t = target.strip().lower()
        gone = [ih for ih in block if ih == t or str((why.get(ih) or {}).get("repo", "")).lower() == t]
    data["blocklist"] = [ih for ih in block if ih not in gone]
    for ih in gone:
        why.pop(ih, None)
    return gone


def _from_internet(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def _slug(repo: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in repo.replace("/", "__"))[:120]


def _inside(base: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _ih(h: lt.torrent_handle) -> str:
    return str(h.info_hashes().v1)


def listen_interfaces(cfg: Config, port: int, listen_host: str | None = None) -> str:
    if listen_host:
        hosts = [listen_host]
    elif cfg.bind_interface:
        hosts = [cfg.bind_interface]
    else:
        hosts = ["0.0.0.0", "[::]"]
    return ",".join(f"{h}:{port}" for h in hosts)


def session_settings(cfg: Config, *, port: int, fingerprint: str, listen_host: str | None = None,
                     dht: bool = True, probe: bool = False) -> dict:
    s = {
        "user_agent": f"{USER_AGENT} libtorrent/{lt.__version__}",
        "peer_fingerprint": fingerprint,
        "listen_interfaces": listen_interfaces(cfg, port, listen_host),
        "enable_dht": dht,
        "enable_lsd": dht,
        "enable_upnp": cfg.port_forwarding and not probe,
        "enable_natpmp": cfg.port_forwarding and not probe,
        # Only what _handle_alerts reads (tracker errors still come under error).
        "alert_mask": lt.alert_category.status | lt.alert_category.error | lt.alert_category.storage
        | lt.alert_category.file_progress,
        "connections_limit": cfg.connections_limit,
        "choking_algorithm": int(lt.choking_algorithm_t.rate_based_choker),
        "seed_choking_algorithm": int(lt.seed_choking_algorithm_t.anti_leech),
        "allow_multiple_connections_per_ip": listen_host is not None,
        # Memory. libtorrent 2.x lets each web seed ask for 5 x 16 MiB at once and buffers those
        # replies, so a node fetching several big models from web seeds grew to 2-4 GB. Smaller
        # requests, two at a time, keep it to a few hundred MB at the same speed. (Web seeds are
        # dropped now, see _atp; these stay in case one is ever added.)
        "urlseed_max_request_bytes": 2 * 1024 * 1024,
        "urlseed_pipeline_size": 2,
        "max_queued_disk_bytes": 32 * 1024 * 1024,
    }
    if cfg.bind_interface and not listen_host:
        # Outgoing peer, tracker and DHT traffic too, not just the listen socket.
        s["outgoing_interfaces"] = cfg.bind_interface
        # UPnP/NAT-PMP would talk to the real router, and Local Service Discovery
        # multicasts your infohashes on every interface (it ignores the pin). Neither
        # belongs on a pinned node.
        s["enable_upnp"] = s["enable_natpmp"] = s["enable_lsd"] = False
    if probe:
        s.update({"download_rate_limit": 256 * 1024, "upload_rate_limit": 16 * 1024})
    else:
        s.update({"upload_rate_limit": _rate_limit(cfg.upload_bps),
                  "download_rate_limit": _rate_limit(cfg.download_bps)})
    return s


def _atp(raw: bytes, save_path: Path, prios: list[int]) -> lt.add_torrent_params:
    atp = lt.add_torrent_params()
    d = lt.bdecode(raw)
    # Drop web seeds (outside the info dict, so the infohash is unchanged). Pirate Face's redirects
    # to Hugging Face download links that expire after an hour, and libtorrent never asks a web
    # seed again for a file it redirected: every file not done within the hour stalled for good.
    # A probe also counted the web seed as a seeder of every file. hfpieces does the job instead.
    for k in (b"url-list", b"httpseeds"):
        d.pop(k, None)
    atp.ti = lt.torrent_info(d)
    atp.save_path = str(save_path)
    atp.file_priorities = prios
    atp.flags = atp.flags & ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
    return atp


def _set_tracker(h: lt.torrent_handle, old: str | None, new: str | None) -> None:
    """Replace tracker `old` with `new` on a torrent (either may be None), in a tier of its own."""
    entries = []
    for t in h.trackers():
        if t["url"] != old and t["url"] != new:
            e = lt.announce_entry(t["url"])
            e.tier = t["tier"]
            entries.append(e)
    if new:
        e = lt.announce_entry(new)
        e.tier = max((x.tier for x in entries), default=-1) + 1
        entries.append(e)
    h.replace_trackers(entries)


ANNOUNCE_CHECK_EVERY = 60.0     # how often to read how the account's tracker answers
_ERROR_CATEGORIES = {"libtorrent": lt.libtorrent_category, "http": lt.http_category,
                     "system": lt.system_category, "generic": lt.generic_category}


def announce_state(entry: dict) -> tuple[str, str | None]:
    """How a tracker answers one torrent, from libtorrent's record of it (torrent_handle.trackers()):
    ("ok", None) once it has answered and an announce still gets through, ("failing", why) while
    every announce to it fails, ("waiting", None) before its first answer. One endpoint that works is
    enough: a second listen address the tracker can't be reached from doesn't make it failing."""
    leaves = [leaf for ep in entry.get("endpoints") or [entry] for leaf in ep.get("info_hashes") or [ep]]
    tried = [leaf for leaf in leaves if leaf.get("next_announce", 0) > 0]     # a v1 torrent never uses its v2 slot
    failing = [leaf for leaf in tried if leaf.get("fails")]
    if not tried or len(failing) < len(tried):
        return ("ok" if tried and entry.get("verified") else "waiting"), None
    for leaf in failing:
        if leaf.get("message"):          # the tracker's own words ("unregistered torrent", say)
            return "failing", "it says: " + " ".join(str(leaf["message"]).split())[:120]
    err = next((leaf["last_error"] for leaf in failing if (leaf.get("last_error") or {}).get("value")), None)
    if not err:
        return "failing", None
    category = _ERROR_CATEGORIES.get(err.get("category"))
    return "failing", (category().message(err["value"]) if category else f"{err.get('category')} error {err['value']}").lower()


def _compact_trees(raw: bytes, prios: list[int], sizes: list[int], piece_length: int) -> bytes:
    """Resume data whose v2 hash trees for files we won't download hold only their piece layer.

    Pirate Face's .torrent files leave out BEP 52's piece layers, so libtorrent asks v2 peers for
    them, for every file a peer has, wanted or not, and holds each answer in a full-size tree:
    4-8 MB per GB of file, until the torrent is unloaded. A node in a 500 GB model's swarm grew
    by gigabytes for shards it never downloads. Cut to the piece layer, those trees load at 2 KB
    per GB and libtorrent doesn't ask for them again. A tree without the whole layer is emptied
    (libtorrent asks again). Wanted files keep their trees."""
    try:
        d = lt.bdecode(raw)
    except RuntimeError:          # corrupt: read_resume_data reports it and the torrent is rechecked
        return raw
    trees = d.get(b"trees") if isinstance(d, dict) else None
    if not trees:
        return raw
    bitfields = d.get(b"file-version") == 2           # version 1 masks are strings of '0' and '1'
    changed = False
    for i, t in enumerate(trees):
        if not isinstance(t, dict) or b"hashes" not in t:
            new = dict(NO_TREE)
        elif i < len(prios) and prios[i] == 0 and t[b"hashes"]:
            new = piece_layer_tree(t, sizes[i], piece_length) if bitfields and i < len(sizes) else dict(NO_TREE)
        else:
            continue
        if new != t:
            trees[i] = new
            changed = True
    return lt.bencode(d) if changed else raw


def _forget_pieces(raw: bytes, piece_length: int, sizes: list[int], files: set[int]) -> bytes:
    """Resume data that no longer has the pieces touching these files (deleted from disk)."""
    pieces: set[int] = set()
    offset = 0
    for i, size in enumerate(sizes):
        if i in files and size:
            pieces.update(range(offset // piece_length, (offset + size - 1) // piece_length + 1))
        offset += size
    r = lt.read_resume_data(raw)
    r.have_pieces = [bool(x) and i not in pieces for i, x in enumerate(r.have_pieces)]
    r.verified_pieces = [bool(x) and i not in pieces for i, x in enumerate(r.verified_pieces)]
    r.unfinished_pieces = {p: m for p, m in r.unfinished_pieces.items() if p not in pieces}
    return lt.write_resume_data_buf(r)


# ------------------------------------------------------------------ probe


def probe(cands: list[TorrentCand], cfg: Config, scratch: Path, seconds: float, exclude_pid_prefix: bytes,
          log=print, listen_host: str | None = None, dht: bool = True,
          connect: list[tuple[str, int]] | None = None) -> dict[str, dict]:
    """Watch each swarm. Returns {infohash: {"seeds": n, "partial": {file_index: n}, "peers": n,
    "downloaders": n, "wanted": {file_index: n}}}. Downloaders are peers still fetching (not seeds, not
    done with what they chose, not Flotilla nodes): the swarm's real demand, and only those this node
    could reach. "wanted" splits them by file (see wanted_files)."""
    if not cands or seconds <= 0:
        return {}
    scratch.mkdir(parents=True, exist_ok=True)
    ses = lt.session(session_settings(cfg, port=0, fingerprint=PROBE_PREFIX, listen_host=listen_host,
                                      dht=dht, probe=True))
    handles: dict[str, lt.torrent_handle] = {}
    for c in cands:
        # Want one small file, not the whole torrent: enough to stay interested so peers keep
        # talking and send their piece maps (which cover every file whatever we want). Wanting
        # every file of a hybrid (v2) torrent makes libtorrent set up per-file hash state for the
        # whole torrent: a probe of a few dozen 500 GB+ models ran past 3 GB.
        prios = [0] * len(c.meta.files)
        real = c.meta.real_files()
        if real:
            prios[min(real, key=lambda f: f.size).index] = 1
        atp = _atp(c.meta.raw, scratch / c.infohash, prios)
        atp.download_limit = PROBE_RATE
        atp.upload_limit = 1024
        h = ses.add_torrent(atp)
        for addr in connect or []:
            h.connect_peer(addr)
        handles[c.infohash] = h
    log(f"probe: watching {len(handles)} swarm(s) for {seconds:.0f}s (peers can see your IP address)")

    seen: dict[str, dict[bytes, object]] = {ih: {} for ih in handles}
    # Each peer's latest word on whether it still wants pieces, and when it was first seen: a peer looks
    # like it does until its piece map arrives, so only the last sample counts, and only for a peer seen
    # for SETTLE seconds (one that connected at the very end would otherwise count, seed or not).
    wanting: dict[str, dict[bytes, list]] = {ih: {} for ih in handles}   # key -> [first, wants, last, bits]
    started = time.monotonic()
    deadline = started + seconds
    while True:
        ses.pop_alerts()
        for ih, h in handles.items():
            for p in h.get_peer_info():
                pid = p.pid.to_bytes()
                if pid.startswith(exclude_pid_prefix) or pid.startswith(PROBE_PREFIX.encode()):
                    continue
                key = pid if any(pid) else str(p.ip).encode()
                # A seed by its flag, or by a piece map with every piece (the flag can come later).
                seed = bool(p.flags & lt.peer_info.seed)
                bits = b"" if seed else bytes(p.pieces)   # one byte per piece, not an 8-byte list slot
                seed = seed or (bool(bits) and all(bits))
                if not FLOTILLA_PEER.match(pid) and not (p.flags & (lt.peer_info.connecting | lt.peer_info.handshake)):
                    t = time.monotonic()
                    w = wanting[ih].setdefault(key, [t, True, t, b""])
                    w[1], w[2], w[3] = not (seed or p.flags & lt.peer_info.upload_only), t, bits
                if seed:
                    seen[ih][key] = True
                elif any(bits):
                    seen[ih][key] = bits
        now = time.monotonic()
        if now >= deadline:
            break
        # Sample often at first (connections are short-lived), then back off:
        # reading big bitfields is not free.
        step = 1.0 if now - started < 20 else 5.0
        time.sleep(min(step, max(0.1, deadline - now)))

    out: dict[str, dict] = {}
    for c in cands:
        peers = seen[c.infohash]
        seeds = sum(1 for b in peers.values() if b is True)
        partial = {}
        for f in c.files:
            partial[f.index] = sum(
                1 for b in peers.values() if b is not True and all(b[f.first_piece : f.last_piece + 1])
            )
        down = [bits for first, wants, last, bits in wanting[c.infohash].values() if wants and last - first >= SETTLE]
        out[c.infohash] = {"seeds": seeds, "partial": partial, "peers": len(peers), "downloaders": len(down),
                           "wanted": wanted_files(c.files, down)}
    for h in handles.values():
        ses.remove_torrent(h, lt.session.delete_files)
    end = time.monotonic() + 5
    while time.monotonic() < end and ses.get_torrents():
        ses.pop_alerts()
        time.sleep(0.1)
    del ses
    shutil.rmtree(scratch, ignore_errors=True)
    return out


def wanted_files(files: list, downloaders: list[bytes]) -> dict[int, int]:
    """How many downloaders want each file, from their piece maps. A downloader with part of a file
    is fetching it; one that has started no file it still lacks (no pieces yet, or between files)
    may want any file it lacks, so it counts towards each. A torrent bundling many quantizations
    then shows which ones people are fetching, where the torrent's count can't tell them apart.
    A piece shared with a neighbouring file doesn't count as having started that neighbour."""
    def has(bits: bytes, i: int) -> bool:
        return i < len(bits) and bool(bits[i])

    out = {f.index: 0 for f in files}
    for bits in downloaders:
        lacks, started = [], []
        for f in files:
            if all(has(bits, i) for i in range(f.first_piece, f.last_piece + 1)):
                continue
            lacks.append(f.index)
            inner = range(f.first_piece + 1, f.last_piece) if f.last_piece - f.first_piece >= 2 \
                else range(f.first_piece, f.last_piece + 1)
            if any(has(bits, i) for i in inner):
                started.append(f.index)
        for i in started or lacks:
            out[i] += 1
    return out


def apply_probe(cands: list[TorrentCand], result: dict[str, dict]) -> None:
    """availability = complete seeds (max of observed and tracker count) + partial holders seen."""
    for c in cands:
        r = result.get(c.infohash)
        if not r:
            continue
        if "downloaders" in r and (r["peers"] or not c.seeders):
            c.downloaders = r["downloaders"]       # else it saw nobody: the planner falls back on the tracker
            for f in c.files:
                f.downloaders = r.get("wanted", {}).get(f.index)
        we_are_seed = all(f.held for f in c.files if f.kind != "skip")
        tracker_seeds = max(0, c.seeders - (1 if we_are_seed else 0))
        if r["peers"] == 0 and tracker_seeds > 0:
            continue  # saw nobody; leave availability None so the planner uses the tracker count
        seeds = max(r["seeds"], tracker_seeds)
        for f in c.files:
            f.availability = float(seeds + r["partial"].get(f.index, 0))


# ------------------------------------------------------------------ engine


class Engine:
    def __init__(self, cfg: Config, state: State, log=print, *, listen_host: str | None = None,
                 dht: bool = True, connect: list[tuple[str, int]] | None = None):
        self.cfg, self.state = cfg, state
        self._raw_log = log
        self.log_ring: collections.deque = collections.deque(maxlen=300)
        self.listen_host, self.dht, self.connect = listen_host, dht, connect or []
        # Unique per node, so our own probe can skip us without skipping other
        # Flotilla seeders.
        self.fingerprint = f"-FL0100-{state.node_id[:6]}"
        self.ses = lt.session(session_settings(cfg, port=cfg.listen_port, fingerprint=self.fingerprint,
                                               listen_host=listen_host, dht=dht))
        self.handles: dict[str, lt.torrent_handle] = {}
        self._st: dict[str, tuple] = {}     # ih -> (monotonic, handle, status): see status()
        self.lock = threading.RLock()
        self.verify_q: queue.Queue = queue.Queue()
        self.failed: list[tuple[str, int, str]] = []
        self.stop = threading.Event()
        self.resume_dir = state.dir / "resume"
        self.torrent_dir = state.dir / "torrents"
        # Seeding credit (an opt-in Pirate Face account key) and per-file swarms.
        self.pf_account = Account(key_path(cfg.path), lambda: PirateFace(tuple(cfg.allowed_hosts)), log=self.log)
        self._pf_nudge = threading.Event()      # set to look at the account file now, not in a minute
        self._tracker_in_use: tuple[int, str | None] = (0, None)
        self._announce: dict | None = None      # how the account's tracker answers: see check_announces
        self._announce_at = 0.0
        # The port the VPN forwards to this node (vpnport.py), and the one it listens on for it.
        self.vpn_forward: Forward | None = None
        self.listening_on = cfg.listen_port
        # The port asked for (listening_on) and the one libtorrent got: if another program (another
        # torrent client, say) holds the one asked for, libtorrent takes the next free one (it tries ten,
        # then lets the system pick). port_view() says so; _port_noted keeps the log to one line.
        self._port_noted: tuple[int, int] | None = None
        self.perfile = PerFile(self)
        self.index = MagnetIndex(self)
        self.last_plan: Plan | None = None
        self.feed: Feed | None = None
        self.ledger: Ledger | None = None
        self.next_refresh = 0.0
        self._up_seen: dict[str, int] = {}
        self.disk_paused = False
        self._last_pct = -1
        # This run: when it started/ends, bytes uploaded, and who we uploaded to.
        self.run_started = time.time()
        self.update_available: dict | None = None    # a newer signed release, from _update_loop
        self.run_until: float | None = None
        self.session_up_start = 0
        self.upload_peers: set[bytes] = set()
        self._last_peer_scan = 0.0
        # Who started the connections: peers that connected in (seen last at inbound["last"]), and
        # whether this node is dialing harder because nobody can reach it.
        self.state.data.setdefault("inbound", {"last": 0.0})
        self.peers_in = self.peers_out = self.peers_holepunched = 0
        self.dialing_out = False
        self._dial_defaults = {k: self.ses.get_settings()[k] for k in DIAL_OUT}
        self._last_dial_announce = 0.0
        self.state.data.setdefault("blocklist", [])
        self.state.data.setdefault("blocked", {})      # infohash -> {repo, at, why}: what `flotilla unblock` names
        self.state.data.setdefault("dropped", [])      # repos the user removed from the dashboard
        self.state.data.setdefault("ledger", {})       # seed-hours per torrent (a points estimate)
        if self.state.data.get("ledger_v", 1) < 2:
            # Before 0.2.9 the estimate also counted torrents held only in part, which earn nothing.
            for e in self.state.data["ledger"].values():
                e["points_est"] = 0.0
            self.state.data["ledger_retired"] = 0.0
            self.state.data["ledger_v"] = 2
        # Reorienting: when this node last swapped held pieces for needier ones, and what it swapped.
        self.state.data.setdefault("reorient", {})
        self._reorient_now = False                     # the user asked for one, whatever the schedule
        self._last_cands: list[TorrentCand] = []       # what the last plan chose from
        self.reorient_preview: dict | None = None      # what the next reorientation would swap
        # Dashboard plumbing: commands run on the main loop, never on the web thread.
        self.user_paused = False
        self.commands: queue.Queue = queue.Queue()
        # Bytes moved per torrent, per minute and per hour, for as long as the node runs.
        self.history = History(state.dir / "history.db")
        self._hist_seen: dict[str, tuple[int, int]] = {}   # libtorrent's counters at the last sample
        self._last_remote = 0.0   # last snapshot written for a dashboard in another process
        # Models held back to stay under the memory budget: files they haven't started wait, files
        # already downloading finish, and everything they have keeps seeding.
        self._parked: set[str] = set()
        self._last_unpark = 0.0
        # Reloads that cut peer-supplied hash trees down to their piece layers (see _compact_trees).
        self._v2_size: dict[str, int] = {}                          # bytes in each v2 torrent
        self._compacted: dict[str, tuple[float, int]] = {}          # ih -> (when, bytes freed)
        self._last_compact = 0.0
        self._main_ident = threading.get_ident()      # run() sets it again
        self._refresher: threading.Thread | None = None
        self._plans_applied = 0
        self._refresh_failed = False
        self._mem_used: int | None = None
        self._dirty = False      # state changed in a way worth saving before the next SAVE_EVERY
        self._snap: tuple[float, dict] | None = None       # (monotonic, the last dashboard snapshot)
        self._snap_lock = threading.Lock()
        self._local: tuple | None = None   # (file signature, (picks, order, allot)): see local_view()
        self._last_sample = 0.0
        self._sampler: threading.Thread | None = None
        self.cand_info: dict[str, dict] = {}
        self.last_rejected: list[Rejection] = []
        self.dashboard = None
        self._bind_check: tuple[float, bool | None] = (0.0, None)
        # Torrents libtorrent stopped on a disk error, started again with backoff (_retry_errored):
        # ih -> {"tries", "due" (None once retried), "at" (the last error)}.
        self._errored: dict[str, dict] = {}
        self._step_errors: dict[str, tuple[float, int]] = {}   # step -> (last logged, failures since)
        self._shut = False
        self._tighten_resume_dir()
        netsafe.pin(cfg.bind_interface)     # our own requests leave by the same pin as torrent traffic
        # Pieces no peer can supply, from Hugging Face (see hfpieces); its thread starts in run().
        self.hf = HFPieces(self)
        # A verification queue does not survive restarts: anything that was
        # mid-check goes back to "wanted" and is re-checked once the torrent is.
        for hold in self.state.holdings.values():
            for e in hold["files"].values():
                if e["status"] in ("flushing", "verifying"):
                    e["status"] = "wanted"
        threading.Thread(target=self._verifier, daemon=True).start()

    def log(self, msg: str) -> None:
        self.log_ring.append((time.time(), msg))
        try:
            self._raw_log(msg)
        except Exception:     # stdout on a full disk, or closed: the dashboard's log still has it
            pass

    def _step_failed(self, step: str, e: BaseException) -> None:
        """Log a housekeeping step that failed, at most once per STEP_LOG_EVERY for that step: a full
        disk fails the same writes every few seconds."""
        now = time.monotonic()
        last, since = self._step_errors.get(step, (None, 0))
        if last is None or now - last >= STEP_LOG_EVERY:
            more = f" ({since} more time(s) since the last report)" if since else ""
            self.log(f"{step} failed: {type(e).__name__}: {e}{more}; carrying on")
            self._step_errors[step] = (now, 0)
        else:
            self._step_errors[step] = (last, since + 1)

    def _guarded(self, step: str, fn, *args):
        """Run one step of the main loop; a failure is logged (rate-limited) and the loop goes on."""
        try:
            return fn(*args)
        except Exception as e:
            self._step_failed(step, e)
            return None

    def _tighten_resume_dir(self) -> None:
        """Resume data holds every tracker of its torrent, the account's seeding tracker (and so its
        key) among them: this user only. Files written by older versions were readable by all."""
        try:
            if not self.resume_dir.is_dir():
                return
            os.chmod(self.resume_dir, 0o700)
            for f in self.resume_dir.iterdir():
                if f.is_file() and f.stat().st_mode & 0o077:
                    os.chmod(f, 0o600)
        except OSError as e:
            self.log(f"couldn't make the resume data private ({e})")

    def _resume_dir_ready(self) -> None:
        if not self.resume_dir.is_dir():
            self.resume_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.resume_dir, 0o700)

    def status(self, ih: str, h: lt.torrent_handle, fresh: bool = False):
        """This torrent's status, read cheaply (STATUS_FLAGS) and shared for STATUS_MAX_AGE
        between everything that asks (accounting, the ledger, the dashboard, Hugging Face).
        `fresh`: read it now (and share that)."""
        now = time.monotonic()
        hit = self._st.get(ih)
        if not fresh and hit is not None and hit[1] == h and now - hit[0] < STATUS_MAX_AGE:
            return hit[2]
        st = h.status(STATUS_FLAGS)
        self._st[ih] = (now, h, st)
        return st

    # ---------------------------------------------------------- discovery

    def trusted_keys(self) -> list[PublicKey]:
        keys = []
        for k in BUILTIN_EDITOR_KEYS + list(self.cfg.trusted_keys):
            try:
                keys.append(PublicKey.parse(k))
            except SignatureError as e:
                self.log(f"feed: ignoring bad trusted key: {e}")
        return keys

    def discover(self) -> tuple[list[TorrentCand], list[Rejection]]:
        self.feed = load_feed(self.cfg.feed_urls, self.trusted_keys(), self.state.data["feed_version"],
                              self.state.dir / "cache", tuple(self.cfg.feed_allowed_hosts), log=self.log,
                              bundled=BUNDLED_FEED)
        self.feed = apply_local(self.feed, self.cfg.local_feed, log=self.log)
        if self.feed.version > self.state.data["feed_version"]:
            self.state.data["feed_version"] = self.feed.version
        allowed = tuple(self.cfg.allowed_hosts)
        pf = PirateFace(allowed, cache_dir=self.state.dir / "cache")
        pf.prune_cache()
        hf = HuggingFace(allowed, self.state.dir / "cache")
        hf.prune_cache()
        with self.lock:
            held_repos = {h["repo"] for h in self.state.holdings.values()}
        self.ledger = load_ledger(self.trusted_keys(), self.state.dir, tuple(self.cfg.feed_allowed_hosts), log=self.log)
        try:
            self.index.sync(pf)
        except (netsafe.FetchError, netsafe.UnsafeURL, ValueError, OSError) as e:
            self.log(f"magnet index: couldn't check for a new build ({e})")
        picks = self._sweep_picks(held_repos)
        swept = set(self.state.data.get("sweep", {}).get("picked", []))
        cands, rejected = gather(self.cfg, self.feed, pf, hf, held_repos, log=self.log, ledger=self.ledger,
                                 index=self.index, covered=bool(self.state.data.get("list_covered")),
                                 sweep=picks, swept=swept)
        for c in cands:
            if c.index_build:
                self.log(f"{c.repo}: Pirate Face can't be reached; listed from the magnet index (build "
                         f"{c.index_build}), checked against {c.verify_source}")
            if c.replaces:
                self.log(f"{c.repo}: Pirate Face replaced the pinned torrent {c.replaces[:12]}… with "
                         f"{c.infohash[:12]}…; following it ({c.verify_source} vouches for every file)")
        with self.lock:
            block, dropped = set(self.state.data["blocklist"]), set(self.state.data["dropped"])
        return [c for c in cands if c.infohash not in block and c.repo not in dropped], rejected

    def _sweep_picks(self, held_repos: set[str]) -> list[str]:
        """The catalog sweep's picks (sweep.py) while the whole list has its copies: scraped at most
        daily, and kept in state.json in between."""
        rec = self.state.data.setdefault("sweep", {})
        if not (self.cfg.catalog_sweep and self.state.data.get("list_full") and self.index.enabled
                and self.index.entries and self.cfg.sweep_candidates > 0):
            return []
        now = time.time()
        if now - rec.get("at", 0) < sweep.EVERY and rec.get("week") == sweep.week_of(now):
            return [p["repo"] for p in rec.get("picks", [])]
        skip = {m.repo.lower() for m in (self.feed.models if self.feed else [])} | {r.lower() for r in held_repos}
        try:
            picks = sweep.pick(self.index.entries, self.state.node_id, now, self.cfg.sweep_candidates, skip)
        except (OSError, ValueError) as e:
            self.log(f"catalog sweep: couldn't ask the tracker for seeder counts ({e}); trying again next refresh")
            return [p["repo"] for p in rec.get("picks", [])]
        with self.lock:
            rec.update(at=now, week=sweep.week_of(now), picks=picks)
            rec["picked"] = (rec.get("picked", []) + [p["repo"] for p in picks if p["repo"] not in rec.get("picked", [])])[-2000:]
        none = sum(1 for p in picks if p["seeders"] == 0)
        self.log(f"catalog sweep: scraped {sweep.SLICE} of Pirate Face's {len(self.index.entries)} models; "
                 f"looking at the {len(picks)} least seeded ({none} with no seeder)")
        return [p["repo"] for p in picks]

    # ---------------------------------------------------------- planning

    def mark_held(self, cands: list[TorrentCand]) -> None:
        for c in cands:
            files = (self.state.holdings.get(c.infohash) or {}).get("files", {})
            h = self.handles.get(c.infohash)
            progress = None
            for f in c.files:
                e = files.get(str(f.index))
                held = bool(e and e["status"] in ("wanted", "flushing", "verifying", "verified"))
                f.held, f.held_since = held, (e.get("since") if held else None)
                if not held:
                    f.done = 0.0
                elif e["status"] == "verified":
                    f.done = 1.0
                else:
                    if progress is None:
                        try:
                            progress = h.file_progress(lt.torrent_handle.piece_granularity) if h else []
                        except RuntimeError:
                            progress = []
                    got = progress[f.index] if f.index < len(progress) else 0
                    f.done = min(1.0, got / f.size) if f.size else 1.0

    def budget_bytes(self) -> int:
        """min(budget, held + free - reserve). Held is what the holdings' folders take on disk: not
        everything in the folder, where other files (the user's own, a model's leftovers, a probe's
        scratch) aren't ours to plan over. Counting them promised the plan space it would never get,
        and the disk guard had to stop the downloads it started."""
        self.cfg.folder.mkdir(parents=True, exist_ok=True)
        with self.lock:
            folders = {hold["save_path"] for hold in self.state.holdings.values() if hold.get("save_path")}
        ours = 0
        for folder in folders:
            for root, _dirs, files in os.walk(folder):
                for name in files:
                    try:
                        st = os.stat(os.path.join(root, name))
                        ours += getattr(st, "st_blocks", 0) * 512 or st.st_size
                    except OSError:
                        pass
        free = shutil.disk_usage(self.cfg.folder).free
        return int(max(0, min(self.cfg.budget_gb * GB, ours + free - self.cfg.reserve_gb * GB)))

    def _kept(self, cands: list[TorrentCand], rejected: list[Rejection]) -> list[str]:
        """Holdings the plan can't see but apply keeps: models whose listing failed for now
        (a transient rejection). They aren't candidates, so the plan has to be told their space."""
        transient = {r.repo for r in rejected if r.transient}
        delisted = {r.repo for r in rejected if r.delisted}
        listed = {c.infohash for c in cands}
        with self.lock:
            return [ih for ih, hold in self.state.holdings.items()
                    if ih not in listed and (hold["repo"] in transient
                                             or (hold["repo"] in delisted
                                                 and hold.get("delisted", 0) + 1 < DELIST_GRACE))]

    def params(self, churn_bytes: int = 0, cands: list[TorrentCand] | None = None,
               rejected: list[Rejection] | None = None) -> Params:
        """What the planner works from. churn_bytes: how much of what's held it may swap for needier
        pieces (0 between reorientations). cands and rejected: what the plan chooses from, so
        holdings kept outside it (_kept) count against the budget."""
        kept = self._kept(cands or [], rejected or [])
        with self.lock:
            kept_bytes = sum(e["size"] for ih in kept for e in self.state.holdings[ih]["files"].values())
        return Params(
            budget_bytes=self.budget_bytes(), upload_bps=self.cfg.upload_bps, download_bps=self.cfg.download_bps,
            kept_bytes=kept_bytes, kept_torrents=len(kept),
            max_torrents=self.cfg.max_torrents, spread=self.cfg.spread, hold_bonus=self.cfg.hold_bonus,
            min_hold_days=self.cfg.min_hold_days, whole_model_under_bytes=int(self.cfg.whole_model_under_gb * GB),
            now=time.time(), allot={r: int(gb * GB) for r, gb in (self.feed.allot_gb if self.feed else {}).items()},
            churn_bytes=churn_bytes, manual=self.feed.manual if self.feed else frozenset(),
            reachable=self.reachable(), focus=self.cfg.focus,
            stuck=self._stuck(), cooling=self._cooling(),
        )

    def _cooling(self) -> frozenset[tuple[str, str]]:
        """Files reorientations swapped out within REPICK_AFTER. Without this the next reorientation,
        on a new probe's counts, can find a file just dropped worth a bit more than what's held and
        fetch it again."""
        now = time.time()
        mem = self.state.data.setdefault("swapped_out", {})
        for k, t in list(mem.items()):
            if now - t > REPICK_AFTER:
                del mem[k]
        return frozenset(tuple(k.split(":", 1)) for k in mem)

    def _remember_swapped_out(self, units, now: float) -> None:
        mem = self.state.data.setdefault("swapped_out", {})
        for u in units:
            for f in u.files:
                mem[f"{u.cand.infohash}:{f.repo_path}"] = now

    def _stuck(self) -> frozenset[str]:
        """Torrents Hugging Face wouldn't serve a file no peer was sending, now or within STUCK_FOR. Kept
        a while: dropping the file clears the gated mark, and the plan would pick it again at once."""
        now = time.time()
        mem = self.state.data.setdefault("stuck", {})
        for ih in list(getattr(self.hf, "gated", {}) or {}):
            mem[ih] = now
        for ih, t in list(mem.items()):
            if now - t > STUCK_FOR:
                del mem[ih]
        return frozenset(mem)

    def replan(self, cands: list[TorrentCand], rejected: list[Rejection],
               probe_seconds: float | None = None) -> Plan | None:
        """Probe the candidates' swarms, then plan and apply. Called on the refresh thread, the
        planning and applying are handed to the main loop (they add and remove torrents, and
        only the main loop reads libtorrent's alerts) and this returns None."""
        with self.lock:
            self.mark_held(cands)
        seconds = self.cfg.probe_seconds if probe_seconds is None else probe_seconds
        result = probe(cands, self.cfg, self.cfg.folder / ".probe", seconds, self.fingerprint.encode(),
                       log=self.log, listen_host=self.listen_host, dht=self.dht, connect=self.connect)
        apply_probe(cands, result)
        was_covered = bool(self.state.data.get("list_covered"))
        # The spare entries were looked at only if the list was covered when they were gathered.
        covered = set_targets(cands, spare_included=was_covered)
        full = was_covered and covered and list_full(cands)
        if full != bool(self.state.data.get("list_full")):
            self.log("everything on the list has its target number of copies: spare space now goes to the "
                     "least-seeded models in Pirate Face's catalog (the catalog sweep)" if full else
                     "some of the list is short of copies again: the catalog sweep pauses")
            with self.lock:
                self.state.data["list_full"] = full
        if covered != was_covered:
            spare = sum(1 for m in (self.feed.models if self.feed else []) if m.spare)
            self.log(f"the swarm now holds the target number of copies of nearly everything on the list: its "
                     f"{spare} spare model(s) are looked at from the next refresh"
                     if covered else "some of the list is short of copies again: its spare models are no "
                                     "longer looked at")
            with self.lock:
                self.state.data["list_covered"] = covered
        with self.lock:
            for c in cands:
                self.cand_info[c.infohash] = {
                    "repo": c.repo, "priority": c.priority, "target": c.target_replicas, "hf_backed": c.hf_backed,
                    "seeders": c.seeders, "leechers": c.leechers, "downloaders": c.downloaders,
                    "files": [{"i": f.index, "path": f.repo_path, "size": f.size, "avail": f.availability,
                               "downloaders": f.downloaders}
                              for f in c.files if f.kind == "weight"],
                }
            # Forget swarms no longer looked at (old revisions, models off the list) unless held.
            for ih in set(self.cand_info) - {c.infohash for c in cands} - set(self.state.holdings):
                del self.cand_info[ih]
        if threading.get_ident() != self._main_ident:
            self.commands.put(("apply_plan", {"cands": cands, "rejected": rejected}))
            return None
        return self._plan_and_apply(cands, rejected)

    def _plan_and_apply(self, cands: list[TorrentCand], rejected: list[Rejection]) -> Plan:
        now = time.time()
        ro = self.state.data["reorient"]
        asked, self._reorient_now = self._reorient_now, False
        due = asked or self.reorient_due(now)
        if self._carry_over(cands):
            with self.lock:
                self.mark_held(cands)
        base = self.params(cands=cands, rejected=rejected)
        churn = int(self.cfg.churn_gb * GB)
        p = make_plan(cands, dataclasses.replace(base, churn_bytes=churn if due else 0), self.state.node_id)
        self.apply(p, cands, rejected)
        self.last_plan = p
        self._plans_applied += 1
        self._last_cands = cands
        out, into = self._swap_groups(p.swap_out), self._swap_groups(p.swap_in)
        if due:
            ro["last"] = now
            ro["result"] = {"at": now, "churn_gb": self.cfg.churn_gb, "asked": asked, "out": out, "in": into}
            self._remember_swapped_out(p.swap_out, now)
            if out:
                self.log(f"reorient: dropped {self._swap_words(out)}, the lowest-priority pieces held, for "
                         f"{self._swap_words(into)} the swarm needs more (up to {self.cfg.churn_gb:g} GB each time)")
            elif not churn:
                self.log("reorient: nothing swapped, the churn size is 0 GB")
            else:
                can_go = any(swappable(u, base.manual) for pk in p.picks.values() for u in pk.units)
                days = f"{self.cfg.min_hold_days:g} day{'' if self.cfg.min_hold_days == 1 else 's'}"
                self.log("reorient: nothing to swap"
                         + (f": everything held is a model you chose, a copy the swarm needs this node to keep, "
                            f"or was picked less than {days} ago and is coming in"
                            if not can_go
                            else ": nothing in the swarm needs the space more than what's held"))
        else:
            ro.setdefault("last", now)       # a new node's first reorientation comes one period after it starts
            if out:                          # only a model the user chose comes in between reorientations
                self.log(f"made room for {self._swap_words(into)} of your own models: dropped {self._swap_words(out)}")
        self._save_now()
        self._update_preview(base, now)
        self._snap = None      # a new plan: the dashboard's next poll shows it
        return p

    def _carry_over(self, cands: list[TorrentCand]) -> int:
        """A held model whose torrent changed (Pirate Face replaced it, or listed a new revision): move
        the files already held and verified into the new torrent's folder, so they're checked again
        against the new torrent and its hashes instead of downloaded again. Main loop only."""
        listed = {c.infohash for c in cands}
        moved_total = 0
        for old, hold in list(self.state.holdings.items()):
            if old in listed:
                continue
            new = [c for c in cands if c.repo == hold["repo"] and c.infohash not in self.state.holdings]
            if len(new) > 1:
                new = [c for c in new if c.quant == hold.get("quant")]
            if len(new) != 1:
                continue
            c = new[0]
            by_path = {f.repo_path: f for f in c.files if f.kind != "skip"}
            moves = []
            for k, e in hold["files"].items():
                f = by_path.get(e["path"])
                same = f is not None and f.size == e["size"] and (
                    (f.sha256 and (e.get("sha256") or "").lower() == f.sha256.lower())
                    or (f.git_oid and (e.get("git_oid") or "").lower() == f.git_oid.lower()))
                if e["status"] == "verified" and same:
                    moves.append((e, f))
            if not moves:
                continue
            # Under the lock throughout: the dashboard, history, memory and Hugging Face threads go
            # through handles and holdings under it, and this takes torrents out of both.
            with self.lock:
                self.perfile.drop(old)                   # they point at the old folder
                h = self.handles.get(old)
                if h is not None:
                    self._take_out(old, h)
                src, dest = Path(hold["save_path"]), self.save_path(c.repo, c.infohash)
                files = {}
                for e, f in moves:
                    to = dest / c.meta.files[f.index].path
                    try:
                        to.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(src / e["tpath"], to)
                    except OSError as err:
                        self.log(f"{c.repo}: couldn't move {e['path']} over ({err}); it's downloaded again")
                        continue
                    files[str(f.index)] = {"path": f.repo_path, "tpath": c.meta.files[f.index].path,
                                           "size": f.size, "sha256": f.sha256, "git_oid": f.git_oid,
                                           "status": "wanted", "since": e.get("since") or time.time(), "prio": 4}
                try:
                    self._store_torrent(c.infohash, c.meta.raw)
                except OSError as err:   # apply stores it again when it picks the new torrent
                    self.log(f"{c.repo}: couldn't store the new .torrent ({err})")
                self.state.holdings[c.infohash] = {"repo": c.repo, "quant": c.quant, "revision": c.revision,
                                                   "save_path": str(dest), "files": files}
                led = self.state.data["ledger"]
                if old in led and c.infohash not in led:
                    led[c.infohash] = led.pop(old)
                self._remove(old, delete=True)          # what's left of the old torrent
            why = (f"Pirate Face replaced its torrent ({old[:12]}… → {c.infohash[:12]}…)"
                   if hold.get("revision") == c.revision else f"it's listed at a new revision ({c.revision[:12]})")
            self.log(f"{c.repo}: {why}; moved {len(files)} verified file(s) over to be checked against the new "
                     f"torrent instead of downloading them again")
            moved_total += len(files)
        return moved_total

    # ---------------------------------------------------------- reorienting

    def reorient_at(self) -> float | None:
        """When the next scheduled reorientation is due (None: never, or no plan made yet)."""
        every = REORIENT_EVERY.get(self.cfg.reorient)
        last = self.state.data["reorient"].get("last")
        return last + every * self._reorient_stretch() if every and last else None

    def _reorient_stretch(self) -> float:
        """This node's own share of the reorientation period, 1 ± REORIENT_SPREAD (see there)."""
        u = int(hashlib.sha256(f"reorient\x00{self.state.node_id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        return 1.0 + REORIENT_SPREAD * (2 * u - 1)

    def reorient_due(self, now: float) -> bool:
        at = self.reorient_at()
        return at is not None and now >= at

    @staticmethod
    def _swap_groups(units: list[Unit]) -> list[dict]:
        """Units by model, in the order given: what the dashboard and the log show of a swap."""
        out: dict[str, dict] = {}
        for u in units:
            c = u.cand
            g = out.setdefault(c.infohash, {"repo": c.repo, "pieces": 0, "bytes": 0, "rarest": u.availability,
                                            "target": c.target_replicas, "priority": round(c.priority, 3),
                                            "hf_backed": c.hf_backed, "whole": False})
            g["pieces"] += sum(1 for f in u.files if f.kind == "weight") or 1
            g["bytes"] += u.size
            g["rarest"] = min(g["rarest"], u.availability)
            g["whole"] = g["whole"] or u.whole
        return list(out.values())

    @staticmethod
    def _swap_words(groups: list[dict]) -> str:
        total = sum(g["bytes"] for g in groups)
        size = f"{total / GB:.1f} GB" if total >= GB / 10 else f"{total / 1e6:.1f} MB"
        names = ", ".join(f"{g['pieces']} of {g['repo']}" if not g["whole"] else g["repo"] for g in groups[:4])
        more = f" and {len(groups) - 4} more" if len(groups) > 4 else ""
        return f"{size} ({names}{more})"

    def _update_preview(self, base: Params | None = None, at: float | None = None) -> None:
        """Work out what a reorientation would swap right now, from the last look at the swarm.
        The dashboard shows it ahead of time. Runs on the main loop."""
        if not self._last_cands and at is None:
            return
        with self.lock:
            self.mark_held(self._last_cands)
        base = base or self.params(cands=self._last_cands, rejected=self.last_rejected)
        p = make_plan(self._last_cands, dataclasses.replace(base, churn_bytes=int(self.cfg.churn_gb * GB)),
                      self.state.node_id)
        can_go = [u for pk in p.picks.values() for u in pk.units if swappable(u, base.manual)] + p.swap_out
        self.reorient_preview = {
            "at": at if at is not None else (self.reorient_preview or {}).get("at"),
            "churn_gb": self.cfg.churn_gb,
            "out": self._swap_groups(p.swap_out), "in": self._swap_groups(p.swap_in),
            "lowest": self._swap_groups(p.lowest), "top": self._swap_groups(p.top),
            # What a swap may take from: held, not picked within min_hold_days (or not coming in), not a copy
            # this node keeps for the swarm, not a model the user chose.
            "swappable": sum(u.size for u in can_go),
            "held": sum(f.size for c in self._last_cands for f in c.files if f.held),
        }

    def reorient_view(self) -> dict:
        ro = self.state.data["reorient"]
        return {"every": self.cfg.reorient, "churn_gb": self.cfg.churn_gb, "last": ro.get("last"),
                "next": self.reorient_at(), "asked": self._reorient_now, "min_hold_days": self.cfg.min_hold_days,
                "preview": self.reorient_preview, "result": ro.get("result")}

    def _do_reorient(self, _) -> None:
        self._reorient_now = True
        self.next_refresh = 0
        self.log("reorientation requested from the dashboard: looking at the swarm first")

    def _next_refresh_at(self) -> float:
        """The next look at the swarm: refresh_hours from now, or sooner if a reorientation is due before then."""
        now = time.time()
        at, due = now + self.refresh_interval(), self.reorient_at()
        return min(at, due) if due and due > now else at

    def _do_apply_plan(self, body: dict) -> None:
        self._plan_and_apply(body["cands"], body["rejected"])

    def _start_refresh(self) -> None:
        """Refresh on its own thread. Its lookups and swarm probe take minutes, and on the main
        loop they held up everything else, the memory budget's reloads included: a node whose
        peers were sending hash trees died at its memory cap before the first refresh ended."""
        if self._refresher is not None and self._refresher.is_alive():
            return

        def work():
            try:
                self.refresh()
            except Exception as e:  # keep seeding even if a refresh blows up
                self._refresh_failed = True
                self.log(f"refresh failed ({e}); seeding what we hold and retrying later")

        self._refresher = threading.Thread(target=work, name="refresh", daemon=True)
        self._refresher.start()

    def refresh(self) -> Plan:
        cands, rejected = self.discover()
        self.last_rejected = rejected
        for r in rejected:
            self.log(f"skip {r.repo}: {r.reason}")
        for c in cands:
            for m in c.manifest_mismatches:
                other = "Hugging Face" if "huggingface" in m else "the hash ledger"
                expected = m.get("huggingface") or m.get("ledger") or ""
                self.log(f"note {c.repo}: Pirate Face's SHA-256 for {m['path']} ({m['pirateface'][:12]}…) "
                         f"disagrees with {other} ({expected[:12]}…); verifying against {other}")
        return self.replan(cands, rejected)

    # ---------------------------------------------------------- session ops

    def save_path(self, repo: str, ih: str) -> Path:
        return self.cfg.folder / f"{_slug(repo)}-{ih[:8]}"

    def _add(self, ih: str, prios: list[int], use_resume: bool = True) -> lt.torrent_handle:
        hold = self.state.holdings[ih]
        raw = (self.torrent_dir / f"{ih}.torrent").read_bytes()
        meta = parse_torrent(raw)
        if meta.infohash_v1 != ih:
            raise RuntimeError(f"stored .torrent for {ih} does not match its infohash")
        atp = _atp(raw, Path(hold["save_path"]), prios)
        self._v2_size[ih] = meta.total_size if meta.hybrid else 0
        rf = self.resume_dir / f"{ih}.fastresume"
        if use_resume and rf.exists():
            try:
                r = lt.read_resume_data(_compact_trees(rf.read_bytes(), prios, [f.size for f in meta.files],
                                                       meta.piece_length))
                if str(r.info_hashes.v1) == ih:
                    r.ti, r.save_path, r.file_priorities = atp.ti, atp.save_path, prios
                    # File priorities are ours to set; the saved piece priorities only echo old ones.
                    # libtorrent applies them over file priorities on load, and a later
                    # prioritize_files() that doesn't change a file's priority leaves its pieces
                    # alone, so files paused for memory before a restart stayed at 0 for good.
                    r.piece_priorities = []
                    # Never trust trackers / web seeds from resume data: use the verified .torrent's
                    # trackers, and no web seeds (see _atp).
                    r.trackers, r.url_seeds = list(meta.trackers), []
                    r.flags = (r.flags | lt.torrent_flags.override_trackers | lt.torrent_flags.override_web_seeds) \
                        & ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
                    atp = r
            except Exception as e:  # corrupt resume file: fall back to a recheck
                self.log(f"resume data for {hold['repo']} unusable ({e}); rechecking")
        h = self.ses.add_torrent(atp)
        for addr in self.connect:
            h.connect_peer(addr)
        # Below the disk reserve only downloads wait (as in guard_disk): a seed doesn't write, so a
        # model with every file in keeps seeding when it's added again (a trim, a memory reload).
        if self.disk_paused and any(e["status"] == "wanted" for e in hold["files"].values()):
            h.pause()
        self.add_seeding_tracker(h)
        self.handles[ih] = h
        return h

    def _store_torrent(self, ih: str, raw: bytes) -> None:
        """Keep the verified .torrent under the state dir, written whole. One already there that
        isn't these bytes (cut short by a full disk or a crash, say) is written again: _add would
        fail on it at every refresh."""
        tfile = self.torrent_dir / f"{ih}.torrent"
        try:
            if tfile.read_bytes() == raw:
                return
        except OSError:
            pass
        self.torrent_dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(tfile, raw)

    def extra_handles(self) -> list[tuple[str, str, object]]:
        """(status key, history key, handle) of the torrents besides the models: per-file swarms (counted
        as their model's) and the magnet index."""
        return [*self.perfile.handles(), *self.index.handles()]

    def add_seeding_tracker(self, h: lt.torrent_handle) -> None:
        """Announce to the connected account's seeding tracker too, so this torrent counts toward its
        Pirate Face points. Trackers sit outside the info dict: the infohash doesn't change."""
        url = self._tracker_in_use[1]
        if url:
            _set_tracker(h, None, url)

    def _apply_seeding_tracker(self) -> None:
        """Follow the account: add, swap or remove its seeding tracker on every torrent."""
        gen, url = self.pf_account.tracker()
        if gen == self._tracker_in_use[0]:
            return
        old = self._tracker_in_use[1]
        self._tracker_in_use = (gen, url)
        if url == old:
            return
        # Every tier, so the public trackers keep hearing from us as well as the account's own.
        self.ses.apply_settings({"announce_to_all_tiers": bool(url)})
        for h in [*self.handles.values(), *(c[2] for c in self.extra_handles())]:
            _set_tracker(h, old, url)

    def _wait_gone(self, ih: str, want_deleted: bool, timeout: float = 15.0) -> None:
        end = time.monotonic() + timeout
        target = lt.sha1_hash(bytes.fromhex(ih))
        gone = False
        while time.monotonic() < end:
            alerts = self.ses.pop_alerts()
            deleted = want_deleted and any(
                isinstance(a, (lt.torrent_deleted_alert, lt.torrent_delete_failed_alert))
                and str(a.info_hashes.v1) == ih for a in alerts)
            # Every alert popped is handled, the rest of the batch too: it can hold other torrents'
            # file_completed, torrent_checked and save_resume_data alerts, which come only once.
            self._handle_alerts(alerts)
            if deleted:
                return
            if not want_deleted and not self.ses.find_torrent(target).is_valid():
                if gone:
                    return
                gone = True  # one more spin so storage can close files
            time.sleep(0.1)

    def _remove(self, ih: str, delete: bool) -> None:
        self.perfile.drop(ih)                      # before the files can go
        h = self.handles.pop(ih, None)
        self._up_seen.pop(ih, None)
        self._st.pop(ih, None)
        self._errored.pop(ih, None)
        hold = self.state.holdings.get(ih)
        if h is not None:
            self.ses.remove_torrent(h, lt.session.delete_files if delete else 0)
            self._wait_gone(ih, want_deleted=delete)
        (self.resume_dir / f"{ih}.fastresume").unlink(missing_ok=True)
        if delete:
            if hold:
                sp = Path(hold["save_path"])
                if _inside(self.cfg.folder, sp) and sp.resolve() != self.cfg.folder.resolve():
                    shutil.rmtree(sp, ignore_errors=True)
            self.state.holdings.pop(ih, None)
            (self.torrent_dir / f"{ih}.torrent").unlink(missing_ok=True)
            self._v2_size.pop(ih, None)
            self._compacted.pop(ih, None)
            # Its seed-hours only show while it's held; the points estimate it earned stays in the
            # node's total (points_view's "est") without keeping an entry per torrent ever seeded.
            e = self.state.data["ledger"].pop(ih, None)
            if e:
                self.state.data["ledger_retired"] = self.state.data.get("ledger_retired", 0.0) + e["points_est"]

    def _evict_files(self, ih: str, indices: set[int], prios: list[int]) -> None:
        """Drop some files of a torrent we keep: save its resume data, take it out, delete those
        files, forget their pieces in the resume data and add it again. Only the dropped files'
        pieces are forgotten (with any piece they share with a kept file, which is fetched again),
        so the rest isn't re-hashed: a full recheck read every byte the torrent holds. Without
        resume data it falls back to that recheck."""
        hold = self.state.holdings[ih]
        self.perfile.drop(ih, indices)                # before the files go
        rf = self.resume_dir / f"{ih}.fastresume"
        h = self.handles.get(ih)
        saved = h is not None and self._save_resume_now(ih, h)
        settle = self._take_out(ih, h) if h is not None else None
        base = Path(hold["save_path"])
        sizes = []
        for i in indices:
            e = hold["files"].pop(str(i), None)
            if e:
                p = base / e["tpath"]
                if _inside(base, p):
                    p.unlink(missing_ok=True)
        try:
            meta = parse_torrent((self.torrent_dir / f"{ih}.torrent").read_bytes())
            sizes = [f.size for f in meta.files]
            if saved:
                _write_private(rf, _forget_pieces(rf.read_bytes(), meta.piece_length, sizes, indices))
        except Exception as e:    # unusable resume data: a full recheck is slower but always right
            self.log(f"trim {hold['repo']}: resume data unusable ({e}); rechecking")
            saved = False
        if not saved:
            rf.unlink(missing_ok=True)
        self._add(ih, prios, use_resume=saved)
        if settle:
            self._settle_counters(ih, *settle)

    def _save_resume_now(self, ih: str, h: lt.torrent_handle, timeout: float = 30.0) -> bool:
        """Ask libtorrent for this torrent's resume data and wait until it's written. Main loop only."""
        h.save_resume_data(lt.torrent_handle.save_info_dict)
        saved, end = None, time.monotonic() + timeout
        while saved is None and time.monotonic() < end:
            alerts = self.ses.pop_alerts()
            for a in alerts:
                if isinstance(a, (lt.save_resume_data_alert, lt.save_resume_data_failed_alert)) \
                        and _ih(a.handle) == ih:
                    saved = isinstance(a, lt.save_resume_data_alert)
            self._handle_alerts(alerts)          # writes the resume file
            if saved is None:
                time.sleep(0.05)
        return bool(saved)

    def _take_out(self, ih: str, h: lt.torrent_handle) -> tuple[int, int, int, int]:
        """Remove a torrent from the session to add it again. Its counters start from zero once
        it's back, so this settles them first and returns what _settle_counters needs."""
        self._account_upload()
        st = h.status(STATUS_FLAGS)
        pu, pd = self._hist_seen.get(ih, (0, 0))
        self.handles.pop(ih, None)
        self._st.pop(ih, None)
        self.ses.remove_torrent(h)
        self._wait_gone(ih, want_deleted=False)
        return pu, pd, st.total_payload_upload, st.total_payload_download

    def _settle_counters(self, ih: str, pu: int, pd: int, up: int, down: int) -> None:
        self._up_seen[ih] = 0
        # The history counts bytes since its last sample: carry what moved before the reload.
        self._hist_seen[ih] = (pu - up, pd - down)

    def resume_holdings(self) -> None:
        """Start seeding what we already hold, before any network lookups."""
        for ih, hold in list(self.state.holdings.items()):
            if ih in self.handles:
                continue
            try:
                n = len(parse_torrent((self.torrent_dir / f"{ih}.torrent").read_bytes()).files)
            except (OSError, ValueError) as e:
                self.log(f"cannot resume {hold['repo']}: {e}")
                continue
            prios = [0] * n
            for k, e in hold["files"].items():
                prios[int(k)] = e.get("prio") or 4
            try:
                self._add(ih, prios)
            except Exception as e:       # one model that won't load mustn't keep the rest from seeding
                self.log(f"cannot resume {hold['repo']}: {e}")
        if self.handles:
            self.log(f"resumed seeding {len(self.handles)} torrent(s)")
        self.perfile.resume()
        self.index.resume()

    # ---------------------------------------------------------- applying

    def apply(self, p: Plan, cands: list[TorrentCand], rejected: list[Rejection]) -> None:
        now = time.time()
        transient = {r.repo for r in rejected if r.transient}
        delisted = {r.repo for r in rejected if r.delisted}
        by_ih = {c.infohash: c for c in cands}
        with self.lock:
            for ih in list(self.state.holdings):
                hold = self.state.holdings[ih]
                if ih in by_ih:
                    hold.pop("delisted", None)       # listed again: the count starts over
                if ih in p.picks:
                    continue
                if ih not in by_ih and hold["repo"] in transient:
                    self.log(f"keep {hold['repo']}: listing unreachable right now, holding as-is")
                    continue
                if ih not in by_ih and hold["repo"] in delisted:
                    hold["delisted"] = hold.get("delisted", 0) + 1
                    if hold["delisted"] < DELIST_GRACE:
                        self.log(f"keep {hold['repo']}: Pirate Face lists no torrent for it "
                                 f"({hold['delisted']} of {DELIST_GRACE} refreshes before it's dropped)")
                        continue
                self.log(f"drop {hold['repo']} ({ih[:12]}): no longer in the plan")
                self._remove(ih, delete=True)

            drops: dict[str, set[int]] = {}
            for ih, f in p.drops:
                if ih in p.picks:
                    drops.setdefault(ih, set()).add(f.index)

            for ih, pick in p.picks.items():
                new = ih not in self.state.holdings
                try:
                    self._apply_pick(ih, pick, drops, now)
                except Exception as e:
                    # One model that can't be added (a disk error, a torrent libtorrent refuses) mustn't
                    # keep the rest of the plan from being applied and saved. It's tried again at the
                    # next refresh; one this plan was only now bringing in isn't kept meanwhile.
                    self.log(f"couldn't apply the plan for {pick.cand.repo} ({ih[:12]}): {e}; "
                             f"trying again at the next refresh")
                    if new and ih not in self.handles:
                        self.state.holdings.pop(ih, None)
            self._save_now()

    def _save_now(self) -> None:
        """Write state.json now (after a plan, a verdict, a dashboard action). If it can't be written
        (a full disk), what changed stays marked for the main loop's next save, and that's all."""
        with self.lock:
            try:
                self.state.save()
                self._dirty = False
            except OSError as e:
                self._dirty = True
                self._step_failed("saving state", e)

    def _apply_pick(self, ih: str, pick, drops: dict[str, set[int]], now: float) -> None:
        c = pick.cand
        prios = [0] * len(c.meta.files)
        for f in pick.files:
            prios[f.index] = 7 if (not f.held and (f.availability or 0) == 0) else 4
        self._store_torrent(ih, c.meta.raw)
        hold = self.state.holdings.setdefault(ih, {
            "repo": c.repo, "quant": c.quant, "revision": c.revision,
            "save_path": str(self.save_path(c.repo, ih)), "files": {},
        })
        hold["verify_source"] = c.verify_source
        hold["manifest_mismatches"] = c.manifest_mismatches
        for f in pick.files:
            e = hold["files"].setdefault(str(f.index), {
                "path": f.repo_path, "tpath": c.meta.files[f.index].path, "size": f.size,
                "sha256": f.sha256, "git_oid": f.git_oid, "status": "wanted", "since": now,
            })
            e["prio"] = prios[f.index]
            if ((e.get("sha256") or "").lower(), (e.get("git_oid") or "").lower()) != \
                    ((f.sha256 or "").lower(), (f.git_oid or "").lower()):
                # The expected hash changed: a hash-ledger entry arrived for a model only Pirate
                # Face vouched for. Check the file again against the new hash.
                self.log(f"re-verify {c.repo}/{f.repo_path}: expected hash now from {c.verify_source}")
                e["sha256"], e["git_oid"] = f.sha256, f.git_oid
                if e["status"] in ("verified", "failed"):
                    e["status"] = "wanted"
        if ih in self._parked:     # held back for memory: files it hasn't started stay at 0
            waiting = self._unstarted(ih)
            prios = [0 if i in waiting else p for i, p in enumerate(prios)]
        if ih in drops:
            self.log(f"trim {c.repo}: dropping {len(drops[ih])} file(s) for higher-need shards")
            self._evict_files(ih, drops[ih], prios)
        elif ih in self.handles:
            self.handles[ih].prioritize_files(prios)
            self._queue_unverified(ih)
        else:
            self._add(ih, prios)

    # ---------------------------------------------------------- verification

    def _verifier(self) -> None:
        while True:
            item = self.verify_q.get()
            try:
                self._verify_one(*item)
            except Exception as e:
                # Never let one file end the thread: every later file would stay "verifying" for good
                # (and `run --once` would never finish). Looked at again later, like a read error.
                self._step_failed("verify", e)
                try:
                    self._verify_later(item[0], item[1], max(item[2] + 1, VERIFY_QUICK + 1))
                except Exception:
                    pass

    def _verify_later(self, ih: str, idx: int, attempt: int) -> float:
        """Queue a file to be verified again: quickly at first, then backing off (VERIFY_RETRY)."""
        if attempt <= VERIFY_QUICK:
            delay = 2.0 * attempt
        else:
            delay = min(VERIFY_RETRY[1], VERIFY_RETRY[0] * 2 ** (attempt - VERIFY_QUICK - 1))
        t = threading.Timer(delay, self.verify_q.put, args=((ih, idx, attempt),))
        t.daemon = True                # an hour's wait mustn't hold up the process at exit
        t.start()
        return delay

    def _verify_one(self, ih: str, idx: int, attempt: int) -> None:
        with self.lock:
            hold = self.state.holdings.get(ih)
            entry = dict(hold["files"].get(str(idx)) or {}) if hold else None
            base = Path(hold["save_path"]) if hold else None
        if not entry or base is None or entry.get("status") != "verifying":
            return
        path = base / entry["tpath"]
        ok, why, io_error = False, "", None
        try:
            if path.stat().st_size != entry["size"]:
                why = "size mismatch"
            elif entry.get("sha256"):
                ok = sha256_file(path) == entry["sha256"].lower()
                why = "" if ok else "SHA-256 mismatch"
            elif entry.get("git_oid"):
                ok = git_blob_sha1(path, entry["size"]) == entry["git_oid"].lower()
                why = "" if ok else "git blob hash mismatch"
            else:
                why = "no expected hash"
        except OSError as e:
            # The file couldn't be read (EIO, EACCES, EMFILE...): that's this machine, not the torrent.
            io_error = f"{e.strerror or e}" + (f" ({e.errno})" if e.errno else "")
        if io_error is not None:
            delay = self._verify_later(ih, idx, attempt + 1)
            with self.lock:
                live = self.state.holdings.get(ih, {}).get("files", {}).get(str(idx))
                if live is None:
                    return
                first = "io_error" not in live
                live["io_error"] = io_error
                live.setdefault("io_error_since", time.time())
                repo = self.state.holdings[ih]["repo"]
            if first or attempt >= VERIFY_QUICK:
                self.log(f"verify {repo}/{entry['path']}: couldn't read it ({io_error}); it stays unverified "
                         f"and is checked again in {delay:.0f} s")
            return
        if not ok and attempt < VERIFY_QUICK:
            # Belt and braces: give the disk a moment and look again before
            # concluding the torrent is bad.
            self._verify_later(ih, idx, attempt + 1)
            return
        with self.lock:
            live = self.state.holdings.get(ih, {}).get("files", {}).get(str(idx))
            if live is None:
                return
            live["status"] = "verified" if ok else "failed"
            live["verified_at"] = time.time()
            live.pop("io_error", None)
            live.pop("io_error_since", None)
            self._dirty = True         # a verdict that took a whole file's hashing: save it soon
            repo = self.state.holdings[ih]["repo"]
            if ok:
                self.log(f"verified {repo}/{entry['path']} ({self.state.holdings[ih].get('verify_source')})")
            else:
                self.failed.append((ih, idx, why))
                self.log(f"VERIFY FAILED {repo}/{entry['path']}: {why}")

    def _handle_failures(self) -> None:
        with self.lock:
            failed, self.failed = self.failed, []
            for ih, idx, why in failed:
                if ih not in self.state.holdings:
                    continue
                repo = self.state.holdings[ih]["repo"]
                # libtorrent already checked every piece against the torrent, so a
                # mismatch with the published hash means the torrent itself is wrong.
                self.log(f"blocking torrent {ih[:12]} for {repo}: file {idx} failed ({why}); deleting its data "
                         f"(`flotilla unblock {ih}` lets it be picked again)")
                if ih not in self.state.data["blocklist"]:
                    self.state.data["blocklist"].append(ih)
                self.state.data["blocked"][ih] = {"repo": repo, "at": time.time(), "why": why}
                self._remove(ih, delete=True)
            if failed:
                self._save_now()

    def _unstarted(self, ih: str) -> set[int]:
        """Wanted files of a model that haven't received any data yet.

        These are what the memory budget holds back. libtorrent allocates a file's full v2 hash
        tree (4-8 MB per GB of file) when its first piece arrives, and shrinks it to about
        2 MB per GB once the whole file is verified. A half-downloaded file paused at priority 0
        keeps the full tree, so pausing it saves nothing; letting it finish frees most of it."""
        hold = self.state.holdings.get(ih) or {}
        wanted = {int(k) for k, e in hold.get("files", {}).items() if e["status"] == "wanted"}
        h = self.handles.get(ih)
        if not wanted or h is None:
            return wanted
        try:
            progress = h.file_progress()
        except Exception:
            return wanted
        return {i for i in wanted if i >= len(progress) or progress[i] == 0}

    def _set_downloading(self, ih: str, on: bool) -> None:
        """Let a model's wanted files download, or hold back the ones it hasn't started (priority 0).
        Files already downloading carry on, and the torrent keeps seeding whatever it has."""
        h = self.handles.get(ih)
        hold = self.state.holdings.get(ih)
        if h is None or hold is None:
            return
        # Built from the holding, not read back from the handle: libtorrent applies priorities
        # later, so right after a re-plan set them, get_file_priorities() still returns the old
        # (paused) values. Comparing against those skipped the pause and let the re-plan's
        # priorities start the download again.
        try:
            n = len(h.get_file_priorities())
        except Exception:
            return
        waiting = set() if on else self._unstarted(ih)
        new = [0] * n
        for k, e in hold["files"].items():
            i = int(k)
            if i < n:
                new[i] = 0 if i in waiting else (e.get("prio") or 4)
        h.prioritize_files(new)

    COMPACT_MIN = 10 * GB       # bytes of files a torrent doesn't download before a reload is worth it
    COMPACT_GAP = 30.0          # seconds between reloads, and before holding back after one
    COMPACT_IDLE = 3600.0       # before reloading a torrent whose last reload freed little

    def _compact_candidate(self, now: float) -> str | None:
        """The v2 torrent likely holding the most peer-supplied hash trees (see _compact_trees)."""
        best, most = None, self.COMPACT_MIN
        for ih in self.handles:
            when, freed = self._compacted.get(ih, (0.0, 0))
            if when and now - when < (self.COMPACT_GAP if freed >= 64 << 20 else self.COMPACT_IDLE):
                continue
            hold = self.state.holdings.get(ih) or {}
            unwanted = self._v2_size.get(ih, 0) - sum(e["size"] for e in hold.get("files", {}).values())
            if unwanted > most:
                best, most = ih, unwanted
        return best

    def _do_compact_trees(self, _=None) -> None:
        """Reload one torrent so the hash trees peers sent for files we don't download are cut to
        their piece layers (_compact_trees runs when it's added again). Runs on the main loop."""
        now = time.time()
        with self.lock:
            ih = self._compact_candidate(now)
            h = self.handles.get(ih) if ih else None
        if h is None:
            return
        repo = self.state.holdings[ih]["repo"]
        before = own_memory()
        if not self._save_resume_now(ih, h):
            self._compacted[ih] = (now, 0)
            self.log(f"memory: couldn't save {repo}'s resume data; not reloading it")
            return
        with self.lock:
            if self.handles.get(ih) is not h:    # removed while we waited
                return
            hold = self.state.holdings[ih]
            n = len(h.get_file_priorities())
            waiting = self._unstarted(ih) if ih in self._parked else set()
            prios = [0] * n
            for k, e in hold["files"].items():
                if int(k) < n and int(k) not in waiting:
                    prios[int(k)] = e.get("prio") or 4
            settle = self._take_out(ih, h)
            try:
                self._add(ih, prios)
            except Exception as e:
                self.log(f"memory: couldn't add {repo} again ({e}); the next re-plan will")
                return
            self._settle_counters(ih, *settle)
        after = own_memory()
        freed = before - after if before and after else 0
        self._compacted[ih] = (now, freed)
        size = f" ({before / GB:.1f} GB -> {after / GB:.1f} GB)" if before and after else ""
        self.log(f"memory: reloaded {repo} to let go of hash data for files it doesn't download{size}")

    def govern_memory(self, now: float, used: int | None = None) -> None:
        """Stay under the memory budget (Config.memory_gb). libtorrent's memory grows with the hash
        trees peers send for files we don't download (see _compact_trees) and with every file it is
        downloading (see _unstarted). Above 70% of the budget, a reload cuts the first kind down,
        one torrent every 30 s, the same one again while its reloads free memory. Over the budget, the least important model also starts no new files,
        while the files it has started finish and free most of their memory; under 70% again, the
        most important held-back model goes again, at most every two minutes so it doesn't flap."""
        used = own_memory() if used is None else used
        self._mem_used = used
        if used is None:
            return
        budget = memory_budget(self.cfg)
        with self.lock:
            # Hash trees peers sent for files we don't download: a reload cuts them down.
            if used > budget * 0.7 and now - self._last_compact > self.COMPACT_GAP and self._compact_candidate(now):
                self._last_compact = now
                self.commands.put(("compact_trees", {}))
            # Forget models that finished, or left the plan, while paused.
            self._parked = {ih for ih in self._parked if ih in self.state.holdings and any(
                e["status"] == "wanted" for e in self.state.holdings[ih]["files"].values())}

            def prio(ih):
                return (self.cand_info.get(ih) or {}).get("priority") or 0

            # Models that would start new files: holding one of these back is what saves memory.
            starting = [ih for ih in self.state.holdings
                        if ih in self.handles and ih not in self._parked and self._unstarted(ih)
                        ] if used > budget and now - self._last_compact > self.COMPACT_GAP else []
            if starting:
                ih = min(starting, key=prio)
                self._parked.add(ih)
                self._set_downloading(ih, False)
                self.log(f"memory: {used / GB:.1f} GB, over the {budget / GB:.1f} GB budget; "
                         f"{self.state.holdings[ih]['repo']} starts no new files for now (files already "
                         f"downloading finish, and it keeps seeding)")
            elif self._parked and used < budget * 0.7 and now - self._last_unpark > 120:
                ih = max(self._parked, key=prio)
                self._parked.discard(ih)
                self._last_unpark = now
                self._set_downloading(ih, True)
                self.log(f"memory: {used / GB:.1f} GB of {budget / GB:.1f} GB; downloading "
                         f"{self.state.holdings[ih]['repo']} again")

    def _queue_unverified(self, ih: str) -> None:
        """Files that are complete but not yet verified: flush, then verify."""
        h = self.handles.get(ih)
        if not h:
            return
        progress = h.file_progress()
        flush = False
        with self.lock:
            for k, e in self.state.holdings.get(ih, {}).get("files", {}).items():
                i = int(k)
                if e["status"] in ("wanted", "flushing") and i < len(progress) and progress[i] == e["size"]:
                    e["status"] = "flushing"
                    flush = True
        if flush:
            h.flush_cache()

    def _start_verify(self, ih: str) -> None:
        with self.lock:
            for k, e in self.state.holdings.get(ih, {}).get("files", {}).items():
                if e["status"] == "flushing":
                    e["status"] = "verifying"
                    self.verify_q.put((ih, int(k), 0))

    # ---------------------------------------------------------- run loop

    def _handle_alerts(self, alerts) -> None:
        for a in alerts:
            try:
                self._handle_alert(a)
            except Exception as e:      # one alert (a resume file on a full disk) mustn't lose the rest
                self._step_failed(f"alert {type(a).__name__}", e)

    def _handle_alert(self, a) -> None:
        if isinstance(a, lt.file_completed_alert):
            # Pieces passed their hash check, but may still be in the write
            # cache. Flush first; verify when cache_flushed_alert arrives.
            ih = _ih(a.handle)
            with self.lock:
                entry = self.state.holdings.get(ih, {}).get("files", {}).get(str(a.index))
                if entry and entry["status"] == "wanted":
                    entry["status"] = "flushing"
                    a.handle.flush_cache()
        elif isinstance(a, lt.cache_flushed_alert):
            self._start_verify(_ih(a.handle))
        elif isinstance(a, lt.torrent_checked_alert):
            self._queue_unverified(_ih(a.handle))
        elif isinstance(a, lt.save_resume_data_alert):
            ih = _ih(a.handle)
            self._resume_dir_ready()
            _write_private(self.resume_dir / f"{ih}.fastresume", lt.write_resume_data_buf(a.params))
        elif isinstance(a, lt.listen_succeeded_alert):
            self._note_port()
        elif isinstance(a, (lt.torrent_error_alert, lt.file_error_alert)):
            ih = _ih(a.handle)
            if self.handles.get(ih) == a.handle:      # a model's torrent (per-file swarms see to their own)
                self._torrent_failed(ih, a.message(), time.time())
            else:
                self.log(f"libtorrent: {a.message()}")
        elif isinstance(a, lt.listen_failed_alert):
            self.log(f"libtorrent: {a.message()}")

    def _torrent_failed(self, ih: str, msg: str, now: float) -> None:
        """libtorrent stopped a model's torrent on an error (a full or read-only disk, a file gone).
        It never starts it again by itself (torrents aren't auto-managed here): _retry_errored does,
        waiting TORRENT_RETRY[0], then twice as long each time it fails again."""
        e = self._errored.get(ih)
        if e is not None and e["due"] is not None:
            return                       # a retry is already set: the same fault, said again
        if e is None or now - e["at"] > 2 * TORRENT_RETRY[1]:
            e = {"tries": 0}             # first failure, or the first in a long while
        e["tries"] += 1
        wait = min(TORRENT_RETRY[1], TORRENT_RETRY[0] * 2 ** (e["tries"] - 1))
        e.update(at=now, due=now + wait)
        self._errored[ih] = e
        repo = (self.state.holdings.get(ih) or {}).get("repo", ih[:12])
        self.log(f"libtorrent stopped {repo}: {msg}; starting it again in {wait / 60:.0f} min"
                 + (f" (try {e['tries']})" if e["tries"] > 1 else ""))

    def _retry_errored(self, now: float) -> None:
        """Start again the torrents libtorrent stopped on an error, once their wait is over. While
        the disk guard holds downloads back, a torrent still downloading waits for it to lift."""
        for ih, e in list(self._errored.items()):
            h = self.handles.get(ih)
            if h is None:
                del self._errored[ih]
                continue
            if e["due"] is None:
                if now - e["at"] > 2 * TORRENT_RETRY[1]:
                    del self._errored[ih]        # fine for a long while since: start afresh next time
                continue
            if now < e["due"]:
                continue
            hold = self.state.holdings.get(ih) or {}
            downloading = any(f["status"] == "wanted" for f in hold.get("files", {}).values())
            if self.disk_paused and downloading:
                continue
            e["due"] = None
            try:
                h.clear_error()
                st = h.status()
                if st.upload_mode:           # libtorrent's own answer to a failed write; never ours
                    h.unset_flags(lt.torrent_flags.upload_mode)
                if st.paused:
                    h.resume()
            except RuntimeError as err:       # removed meanwhile
                self.log(f"couldn't start {hold.get('repo', ih[:12])} again ({err})")
                continue
            self._st.pop(ih, None)
            self.log(f"starting {hold.get('repo', ih[:12])} again after its error")

    def _account_upload(self) -> None:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        up = self.state.data["upload"]
        if up["month"] != month:
            up["month"], up["bytes"] = month, 0
            if self.ses.is_paused() and not self.user_paused:
                self.ses.resume()
                self.log("new month: upload cap reset, seeding resumed")
        for key, _, h in [*((ih, ih, h) for ih, h in self.handles.items()), *self.extra_handles()]:
            total = self.status(key, h).total_payload_upload
            up["bytes"] += max(0, total - self._up_seen.get(key, 0))
            self._up_seen[key] = total
        cap = self.cfg.monthly_upload_cap_gb * GB
        if cap and up["bytes"] >= cap and not self.ses.is_paused():
            self.ses.pause()
            self.log(f"monthly upload cap of {self.cfg.monthly_upload_cap_gb:g} GB reached; paused until next month")

    def write_status(self) -> None:
        doc = self.status_doc()
        self.state.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state.dir / "status.tmp"
        tmp.write_text(json.dumps(doc, separators=(",", ":")))
        os.replace(tmp, self.state.dir / "status.json")

    def status_doc(self) -> dict:
        rows = []
        with self.lock:
            for ih, h in list(self.handles.items()):  # the web thread reads while the engine adds
                st = self.status(ih, h)
                hold = self.state.holdings.get(ih, {})
                files = hold.get("files", {})
                whole_gb = self._earning_gb(ih, h)
                rows.append({
                    "repo": hold.get("repo"), "infohash": ih, "state": str(st.state).split(".")[-1],
                    "files": len(files), "verified": sum(1 for e in files.values() if e["status"] == "verified"),
                    "bytes": sum(e["size"] for e in files.values()), "done": st.total_wanted_done,
                    "wanted": st.total_wanted, "up_rate": st.upload_payload_rate,
                    "down_rate": st.download_payload_rate, "uploaded": st.all_time_upload,
                    "peers": st.num_peers, "seeds": st.num_seeds, "verify_source": hold.get("verify_source"),
                    "manifest_mismatches": hold.get("manifest_mismatches", []),
                    "hf_bytes": hold.get("hf_bytes", 0), "per_file": self.perfile.count(ih),
                    # a day, at Pirate Face's seeding rate (1 per 3 h at 10 GB), only while the whole model is here
                    "points_day": round(8 * math.sqrt(whole_gb / 10), 4) if whole_gb else 0,
                    # Files that couldn't be read to verify them (checked again later), and why libtorrent
                    # stopped the torrent if it did (started again later): this machine's disk, not the torrent.
                    "io_errors": [{"path": e["path"], "error": e["io_error"], "since": e.get("io_error_since")}
                                  for e in files.values() if e.get("io_error")],
                    "error": st.errc.message() if st.errc.value() else None,
                })
            doc = {
                "at": datetime.now(timezone.utc).isoformat(), "node": self.state.node_id[:8],
                "paused": self.ses.is_paused(), "disk_paused": self.disk_paused, "listen_port": self.ses.listen_port(),
                "port": self.port_view(),
                "feed": {"version": self.feed.version, "source": self.feed.source, "signer": self.feed.signer,
                         "local": self.feed.local_source,
                         "expires": self.feed.expires.isoformat() if self.feed.version else None} if self.feed else None,
                "vpn_port": self.vpn_port_view(), "reach": self.reach_view(),
                "upload_month": dict(self.state.data["upload"]), "next_refresh": self.next_refresh,
                "budget_bytes": self.last_plan.budget_bytes if self.last_plan else None, "torrents": rows,
                "run": {"started": self.run_started, "until": self.run_until,
                        "uploaded": self.session_uploaded, "upload_peers": len(self.upload_peers)},
                "points": self.points_view(rows),
            }
        doc["hf_gated"] = self.hf.gated_list()
        doc["update"] = self.update_available
        doc["per_file"] = self.perfile.info()
        doc["pirateface"] = self.pf_account.info()
        doc["magnet_index"] = self.index.info()
        return doc

    # ---------------------------------------------------------- dashboard

    SETTINGS = {  # name: (min, max)
        "budget_gb": (1, 1e6), "reserve_gb": (0, 1e6), "upload_mbps": (0.1, 1e5), "download_mbps": (0.1, 1e5),
        "upload_share": (0.05, 1), "download_share": (0.05, 1), "monthly_upload_cap_gb": (0, 1e7),
        "hf_gb_per_day": (0, 1e6), "churn_gb": (0, 1e6),
    }
    SWITCHES = ("hf_fallback", "vpn_port_recover")   # on/off settings
    CHOICES = {"reorient": REORIENT_CHOICES, "focus": FOCUS_CHOICES}   # one-of settings
    ACTIONS = ("settings", "pause", "resume", "replan", "reorient", "drop", "add", "remove", "order", "allot", "skip",
               "unskip", "hf_disconnect", "rescue", "points_connect", "points_disconnect", "points_handle",
               "unblock")

    def _bind_up(self) -> bool | None:
        if not self.cfg.bind_interface:
            return None
        t, v = self._bind_check
        if time.time() - t > 15:
            v = address_present(self.cfg.bind_interface)
            self._bind_check = (time.time(), v)
        return v

    def snapshot(self) -> dict:
        """Everything the dashboard shows. Called from the web thread. Every open tab polls, so a
        snapshot is shared for SNAPSHOT_FRESH seconds, or until the next dashboard action is done."""
        with self._snap_lock:
            hit = self._snap
            if hit is not None and time.monotonic() - hit[0] < SNAPSHOT_FRESH:
                return hit[1]
            doc = self._build_snapshot()
            self._snap = (time.monotonic(), doc)
            return doc

    def local_view(self) -> tuple[list[dict], list[str], dict[str, float]]:
        """Your picks, order and set-aside space from my-priorities.json, read again only when the
        file changes (the dashboard asks several times per snapshot)."""
        path = self.cfg.local_feed
        try:
            st = path.stat()
            sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            sig = None
        hit = self._local
        if hit is not None and hit[0] == sig and sig is not None:
            return hit[1]
        view = (list_picks(path), read_order(path), read_allot(path))
        self._local = (sig, view)
        return view

    def _build_snapshot(self) -> dict:
        doc = self.status_doc()
        try:
            du = shutil.disk_usage(self.cfg.folder)
            free, total = du.free, du.total
        except OSError:
            free = total = 0
        with self.lock:
            for t in doc["torrents"]:
                info = self.cand_info.get(t["infohash"], {})
                hold = self.state.holdings.get(t["infohash"], {})
                held = {int(k): e for k, e in hold.get("files", {}).items()}
                t.update({"priority": info.get("priority"), "target": info.get("target"),
                          "hf_backed": info.get("hf_backed"), "swarm_seeders": info.get("seeders"),
                          "swarm_leechers": info.get("leechers"), "swarm_downloaders": info.get("downloaders"),
                          "ledger": self.state.data["ledger"].get(t["infohash"])})
                if info.get("files"):
                    t["shards"] = [{"path": f["path"], "size": f["size"], "avail": f["avail"],
                                    "downloaders": f.get("downloaders"),
                                    "status": held.get(f["i"], {}).get("status"),
                                    "io_error": held.get(f["i"], {}).get("io_error")} for f in info["files"]]
                else:  # restarted and not replanned yet: show what we hold
                    t["shards"] = [{"path": e["path"], "size": e["size"], "avail": None, "status": e["status"],
                                    "io_error": e.get("io_error")}
                                   for _, e in sorted(held.items()) if e["size"] >= 64 << 20]
            doc.update({
                "version": __version__, "now": time.time(),
                "settings": {k: getattr(self.cfg, k) for k in (*self.SETTINGS, *self.SWITCHES, *self.CHOICES)},
                "reorient": self.reorient_view(),
                "disk": {"free": free, "total": total, "held": sum(t["done"] for t in doc["torrents"]),
                         "folder": str(self.cfg.folder), "paused": self.disk_paused},
                "network": {"bind": self.cfg.bind_interface, "bind_up": self._bind_up(),
                            "port_forwarding": self.cfg.port_forwarding, "listen_port": self.listening_on,
                            "vpn_port": self.vpn_port_view(),
                            **self.reach_view()},
                "config_path": str(self.cfg.path) if self.cfg.path else None,
                "user_paused": self.user_paused,
                "log": [{"t": t, "msg": m} for t, m in list(self.log_ring)[-100:]],
                "rejected": [{"repo": r.repo, "reason": r.reason} for r in self.last_rejected][:60],
                "dropped": list(self.state.data["dropped"]),
                "picks": self.picks_status(doc["torrents"]),
                "priorities": self.priorities_view(doc["torrents"]),
                "allot_total_gb": sum(self.local_view()[2].values()),
                "memory": {"used": self._mem_used, "budget": memory_budget(self.cfg),
                           "paused": sorted(self.state.holdings[ih]["repo"] for ih in self._parked
                                            if ih in self.state.holdings)},
            })
        doc["hf"] = self.hf.info()
        return doc

    def submit(self, action: str, body: dict) -> str:
        """Validate a dashboard request and queue it for the main loop."""
        if action not in self.ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        if action == "settings":
            clean = {}
            for k, v in body.items():
                if k in self.SWITCHES:
                    if not isinstance(v, bool):
                        raise ValueError(f"{k} must be true or false")
                    clean[k] = v
                    continue
                if k in self.CHOICES:
                    if v not in self.CHOICES[k]:
                        raise ValueError(f"{k} must be one of: {', '.join(self.CHOICES[k])}")
                    clean[k] = v
                    continue
                if k not in self.SETTINGS:
                    raise ValueError(f"unknown setting {k!r}")
                if not _finite(v):
                    raise ValueError(f"{k} must be a number")
                lo, hi = self.SETTINGS[k]
                if not lo <= v <= hi:
                    raise ValueError(f"{k} must be between {lo:g} and {hi:g}")
                clean[k] = float(v)
            if not clean:
                raise ValueError("nothing to change")
            body = clean
        elif action == "drop":
            ih = str(body.get("infohash", ""))
            if ih not in self.state.holdings:
                raise ValueError("this node doesn't hold that model")
            body = {"infohash": ih}
        elif action in ("skip", "unskip"):
            repo = body.get("repo")
            if not isinstance(repo, str) or not REPO_RE.match(repo):
                raise ValueError("which model?")
            if action == "unskip" and repo not in self.state.data["dropped"]:
                raise ValueError(f"{repo} isn't skipped")
            body = {"repo": repo}
        elif action == "order":
            order = body.get("order")
            if not isinstance(order, list):
                raise ValueError("order must be a list of models")
            set_order(self.cfg.local_feed, order)   # FeedError explains a bad entry
            self.log(f"priority order changed ({len(order)} models); replanning (free space follows it now, "
                     f"what's held shifts at the next reorientation)")
            self.commands.put(("replan_soon", {}))
            return "saved"
        elif action == "allot":
            repo, gb = body.get("repo"), body.get("gb")
            if not isinstance(repo, str) or not REPO_RE.match(repo):
                raise ValueError("which model?")
            # NaN passes `gb < 0` and every comparison after it, then sits in the snapshot, whose JSON
            # the page can't read: only finite numbers.
            if gb is not None and (not _finite(gb) or gb < 0):
                raise ValueError("GB must be a positive number, or empty to clear")
            others = sum(v for r, v in read_allot(self.cfg.local_feed).items() if r != repo)
            if gb and others + gb > self.cfg.budget_gb:
                raise ValueError(f"that sets aside {others + gb:g} GB in total, more than your {self.cfg.budget_gb:g} GB "
                                 f"of space. {max(0, self.cfg.budget_gb - others):g} GB is left to give this model.")
            set_allot(self.cfg.local_feed, repo, gb or None)
            self.log(f"set aside {gb:g} GB for {repo}; replanning" if gb else f"cleared the space set aside for {repo}")
            self.commands.put(("replan_soon", {}))
            return "saved"
        elif action == "rescue":
            repo = body.get("repo")
            if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
                raise ValueError("not a model name")
            listed = {m.repo for m in self.feed.models} if self.feed else set()
            if repo not in listed and repo not in {m["repo"] for m in self.local_view()[0]}:
                raise ValueError(f"{repo} isn't on your list")
            threading.Thread(target=self._ask_rescue, args=(repo,), name="rescue", daemon=True).start()
            return "asking"
        elif action == "points_connect":
            secret = body.get("secret")
            if not isinstance(secret, str) or len(secret) > 1000:
                raise ValueError("paste the Seeding tracker address or the community torrent key")
            return self._points_save(clean_key(secret))      # PFKeyError (a ValueError) explains
        elif action == "points_disconnect":
            return self._points_save(None)
        elif action == "points_handle":
            handle = body.get("handle")
            if not isinstance(handle, str) or len(handle) > 200:
                raise ValueError("a handle is the name in pirateface.co/name")
            body = {"handle": clean_handle(handle)}           # PFKeyError (a ValueError) explains
        elif action == "unblock":
            # `flotilla unblock`: the main loop changes the blocklist; this only checks there's a match.
            target = None if body.get("all") is True else body.get("target")
            if target is not None and (not isinstance(target, str) or not 0 < len(target) <= 200):
                raise ValueError("an infohash or author/model, or all")
            with self.lock:              # a dry run on a copy
                n = len(unblock({"blocklist": list(self.state.data["blocklist"]),
                                 "blocked": dict(self.state.data["blocked"])}, target))
            if not n:
                raise ValueError("nothing blocked matches that" if target else "nothing is blocked")
            self.commands.put(("unblock", {"target": target}))
            return f"unblocked {n} torrent(s); replanning"
        elif action in ("add", "remove"):
            # Picks are just a file: save right away (so the page shows it at once),
            # and let the main loop do the replan when it's free.
            ref = body.get("ref")
            if not isinstance(ref, str) or len(ref) > 2000:
                raise ValueError("paste a Hugging Face or Pirate Face link, or author/model")
            repo, _ = parse_model_ref(ref)   # FeedError (a ValueError) explains what's wrong
            if action == "remove":
                gone = remove_pick(self.cfg.local_feed, ref)
                self.log(f"removed {gone} from your picks" if gone else f"{repo} wasn't in your picks")
                return "removed" if gone else "not in your picks"
            prio = body.get("priority", 100)
            if not _finite(prio) or not 0 <= prio <= 100:
                raise ValueError("priority must be between 0 and 100")
            repo, new = add_pick(self.cfg.local_feed, ref, float(prio))
            self.log(f"{'added' if new else 'updated'} {repo} in your picks; replanning")
            self.commands.put(("replan_soon", {}))
            return "added" if new else "updated"
        else:
            body = {}
        self.commands.put((action, body))
        return "queued"

    def _points_save(self, secret: str | None) -> str:
        """Connect (save the tracker address or key) or disconnect the Pirate Face account, from the
        dashboard. Saved right away; the points thread picks it up at once. Never logs the secret."""
        info = self.pf_account.info()
        path = self.pf_account.path
        if path is None:
            raise ValueError("there's no config file to keep it next to")
        if info["from_env"]:
            raise ValueError(f"it comes from the file FLOTILLA_PF_KEY_FILE names ({path}): change it there")
        try:
            if secret is None:
                gone = remove_token(path)
            else:
                save_secret(path, secret)
        except OSError as e:
            raise ValueError(f"couldn't {'remove' if secret is None else 'save'} {path}: {e.strerror or e}") from None
        self._pf_nudge.set()
        if secret is None:
            self.log("pirate face: account disconnected from the dashboard" if gone
                     else "pirate face: no account was connected")
            return "disconnected" if gone else "not connected"
        if kind_of(secret) == "tracker":
            self.log("pirate face: seeding tracker address saved from the dashboard")
            return "connected"
        self.log("pirate face: community key saved from the dashboard; checking it with Pirate Face")
        return "checking"

    def _ask_rescue(self, repo: str) -> None:
        """Ask Pirate Face to add a model it has no torrent for (sends only its name). Own thread."""
        try:
            PirateFace(tuple(self.cfg.allowed_hosts)).request_rescue(repo)
        except (netsafe.FetchError, ValueError) as e:
            self.log(f"couldn't ask Pirate Face to add {repo} ({e})")
            return
        with self.lock:
            self.state.data.setdefault("rescue_asked", {})[repo] = time.time()
            self._dirty = True
        self._snap = None
        self.log(f"asked Pirate Face to add {repo}; Flotilla picks it up once it's listed")

    def _remote_loop(self) -> None:
        """On its own thread, so a dashboard outside this process works from the moment the node
        starts, even while the first refresh (lookups, the swarm probe) is still running. Actions
        it takes in are queued and carried out by the main loop."""
        while not self.stop.is_set():
            self.serve_remote(time.time(), act=False)
            self.stop.wait(1.0)

    UPDATE_FIRST, UPDATE_EVERY = 120.0, 86400.0

    def _update_loop(self) -> None:
        """Once a day, is a newer signed release out? Only noted (dashboard, status, log once per
        version); installing is `flotilla update`, run by the user."""
        self.stop.wait(self.UPDATE_FIRST)
        while not self.stop.is_set():
            self.check_update()
            self.stop.wait(self.UPDATE_EVERY)

    def check_update(self) -> None:
        """Goes through the network pin like every other request, and fails quietly: it's only a notice."""
        from . import release
        from .access import in_container
        from .netsafe import FetchError

        if not self.cfg.check_updates:
            self.update_available = None
            return
        try:
            rel = release.latest()
        except (release.ReleaseError, FetchError, OSError):
            return
        if not release.newer(rel.version, __version__):
            self.update_available = None
            return
        where = "docker" if in_container() else "here"
        told = (self.update_available or {}).get("version")
        self.update_available = {"version": rel.version, "page": rel.page, "where": where}
        if told != rel.version:
            how = "run `flotilla update` on the machine running Docker" if where == "docker" else "run `flotilla update`"
            self.log(f"Flotilla {rel.version} is out ({rel.page}): {how}")

    def _memory_loop(self) -> None:
        """The memory budget on its own thread: the first refresh holds up the main loop for
        minutes while the models resumed at startup are already downloading."""
        while not self.stop.is_set():
            try:
                self.govern_memory(time.time())
            except Exception as e:   # never let this stop seeding
                self.log(f"memory: check failed ({e})")
            self.stop.wait(5.0)

    def _sample_loop(self) -> None:
        """History samples on their own thread: refresh() (lookups, the swarm probe) holds up the
        main loop for minutes at startup and at every refresh while libtorrent keeps seeding,
        and a sampler on the main loop charted that time as "not seeding"."""
        while not self.stop.is_set():
            try:
                self.sample(time.time())
            except Exception as e:   # never let a bad sample end the history for this run
                self.log(f"history: sample failed ({e})")
            self.stop.wait(SAMPLE_EVERY)

    def serve_remote(self, now: float, act: bool = True) -> None:
        """For a dashboard running outside this process (outside a VPN container, say): answer its
        actions from the inbox and, while it's watching, keep its snapshot fresh."""
        try:
            handled = take_requests(self.state.dir, self.submit)
            if handled and act:
                self.process_commands()   # act on them before the next snapshot shows the result
            if (handled or now - self._last_remote >= WRITE_EVERY) and snapshot_wanted(self.state.dir):
                self._last_remote = now
                write_snapshot(self.state.dir, self.snapshot())
        except Exception as e:   # the remote dashboard must never stop seeding
            if now - self._last_remote > 60:
                self.log(f"dashboard: couldn't share state ({e})")
                self._last_remote = now

    def process_commands(self) -> None:
        while True:
            try:
                action, body = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                getattr(self, f"_do_{action}")(body)
            except Exception as e:
                self.log(f"dashboard: {action} failed: {e}")
            self._snap = None     # the next poll shows the result, not a snapshot from before it

    def save_config(self) -> None:
        if not self.cfg.path:
            return
        from .config import write_config

        write_config(self.cfg)

    def _do_settings(self, s: dict) -> None:
        changed = {k for k, v in s.items() if getattr(self.cfg, k) != v}   # Save sends every field each time
        for k, v in s.items():
            setattr(self.cfg, k, v)
        self.ses.apply_settings({"upload_rate_limit": _rate_limit(self.cfg.upload_bps),
                                 "download_rate_limit": _rate_limit(self.cfg.download_bps),
                                 "connections_limit": self.cfg.connections_limit})
        self.save_config()
        def shown(v):
            return ("on" if v else "off") if isinstance(v, bool) else v if isinstance(v, str) else f"{v:g}"

        self.log("settings changed: " + ", ".join(f"{k} = {shown(v)}" for k, v in s.items()))
        if "budget_gb" in s or "reserve_gb" in s:
            self.next_refresh = min(self.next_refresh or time.time(), time.time() + 5)
        if "focus" in changed:
            self.log(f"focus: {self.cfg.focus}; replanning (free space follows it now, what's held shifts at the "
                     f"next reorientation)")
            self.commands.put(("replan_soon", {}))
        if changed & {"reorient", "churn_gb", "focus"}:
            self._update_preview()
            due = self.reorient_at()         # a shorter schedule can bring the next one forward
            if due and self.next_refresh:
                self.next_refresh = min(self.next_refresh, max(due, time.time() + 5))

    def _do_points_handle(self, body: dict) -> None:
        self.cfg.pf_handle = body["handle"]
        self.save_config()
        self.log(f"pirate face: the dashboard links to pirateface.co/{body['handle']} for your points" if body["handle"]
                 else "pirate face: handle cleared")

    def _do_pause(self, _) -> None:
        self.user_paused = True
        self.ses.pause()
        self.log("paused from the dashboard")

    def _do_resume(self, _) -> None:
        self.user_paused = False
        self.ses.resume()
        self.log("resumed from the dashboard")

    def _do_hf_disconnect(self, _) -> None:
        try:
            gone = self.hf.conn.disconnect()
        except OSError as e:
            where = " (it's the file FLOTILLA_HF_TOKEN_FILE names: remove it there)" \
                if self.hf.conn.info()["from_env"] else ""
            self.log(f"hugging face: couldn't remove the account token: {e}{where}")
            return
        self.log("hugging face: account token disconnected from the dashboard; if it's no longer needed, delete it "
                 "at huggingface.co/settings/tokens too" if gone else "hugging face: no account token was connected")

    def _do_replan_soon(self, _) -> None:
        self.next_refresh = min(self.next_refresh or time.time(), time.time() + 3)

    @staticmethod
    def friendly(reason: str) -> str:
        if reason.startswith("no magnet") or ".torrent download failed" in reason:
            return "no torrent on Pirate Face yet"
        if reason.startswith("license"):
            return reason.replace("not in policy.licenses", "isn't one you allow")
        if "deny" in reason:
            return "on the deny list"
        return reason

    def priorities_view(self, torrents: list[dict]) -> list[dict]:
        """Every model on the list (published, yours, or just carried), by priority,
        with how much need its swarm shows."""
        feed = self.feed
        held = {t["repo"]: t for t in torrents}
        rejected = {r.repo: r.reason for r in self.last_rejected}
        info = {i["repo"]: i for i in self.cand_info.values()}
        picks, order, allot = self.local_view()
        entries, seen = [], set()
        for m in (feed.models if feed else []):
            if m.repo not in seen:
                seen.add(m.repo)
                entries.append((m.repo, feed.priority(m), "yours" if m.local else "list", m.note,
                                origin_view(m.repo, m.origin)))
        # Your file is read directly, so a drag or a new pick shows at once (the
        # engine itself picks the change up at the replan it triggers).
        for m in picks:
            if m["repo"] not in seen:
                seen.add(m["repo"])
                scores = m.get("scores") or {}
                entries.append((m["repo"], (max(scores.values()) / 100) if scores else 1.0, "yours", m.get("note", ""), None))
        for t in torrents:
            if t["repo"] not in seen:
                seen.add(t["repo"])
                entries.append((t["repo"], feed.unlisted_priority if feed else 0.3, "carrying", "", None))
        if order:
            rank = {r: rank_priority(i, len(order)) for i, r in enumerate(order)}
            entries = [(r, rank.get(r, p), s, n, o) for r, p, s, n, o in entries]
        skipped = set(self.state.data["dropped"])
        entries = [e for e in entries if e[0] not in skipped]
        out = []
        for repo, prio, source, note, origin in sorted(entries, key=lambda e: -e[1]):
            row = {"repo": repo, "priority": round(prio, 3), "source": source, "note": note, "origin": origin,
                   "allot_gb": allot.get(repo), "held_bytes": held[repo]["done"] if repo in held else 0,
                   "rescue_asked": self.state.data.get("rescue_asked", {}).get(repo)}
            i = info.get(repo)
            if i:
                avail = [f["avail"] for f in i["files"] if f["avail"] is not None]
                row.update(pieces=len(i["files"]), size=sum(f["size"] for f in i["files"]), seeders=i["seeders"],
                           leechers=i["leechers"], downloaders=i.get("downloaders"), hf_backed=i["hf_backed"], target=i["target"], measured=len(avail),
                           below=sum(1 for a in avail if a < i["target"]), zero=sum(1 for a in avail if a == 0),
                           rarest=min(avail) if avail else None, status="ok")
            elif repo in rejected:
                row["status"] = self.friendly(rejected[repo])
            else:
                row["status"] = "not checked yet"
            out.append(row)
        return out

    def picks_status(self, torrents: list[dict]) -> list[dict]:
        carrying = {t["repo"] for t in torrents}
        rejected = {r.repo: r.reason for r in self.last_rejected}
        planned = {i["repo"] for i in self.cand_info.values()}
        out = []
        for m in self.local_view()[0]:
            repo = m["repo"]
            if repo in carrying:
                status, kind = "carrying it", "ok"
            elif repo in rejected:
                reason = rejected[repo]
                if reason.startswith("no magnet") or ".torrent download failed" in reason:
                    status = "no torrent on Pirate Face yet"
                elif reason.startswith("license"):
                    status = reason.replace("not in policy.licenses", "isn't one you allow")
                else:
                    status = reason
                kind = "warn"
            elif repo in planned:
                status, kind = "checked: other pieces need the space more right now", ""
            else:
                status, kind = "will be checked at the next replan", ""
            scores = m.get("scores") or {}
            out.append({"repo": repo, "status": status, "kind": kind,
                        "priority": max(scores.values()) if scores else None})
        return out

    def _do_replan(self, _) -> None:
        self.next_refresh = 0
        self.log("replan requested from the dashboard")

    def _do_skip(self, b: dict) -> None:
        """Never carry this model: it leaves the list, and anything held of it is deleted."""
        repo = b["repo"]
        freed = 0
        with self.lock:
            if repo not in self.state.data["dropped"]:
                self.state.data["dropped"].append(repo)
            for ih, hold in list(self.state.holdings.items()):
                if hold.get("repo") == repo:
                    freed += sum(e["size"] for e in hold["files"].values() if e["status"] == "verified")
                    self._remove(ih, delete=True)
                    self.cand_info.pop(ih, None)
            self._save_now()
        self.log(f"skipped {repo}: it won't be carried" + (f"; freed {freed / GB:.1f} GB" if freed else "")
                 + ". Restore it any time under Skipped on the Priorities tab")
        self.commands.put(("replan_soon", {}))

    def _do_unblock(self, b: dict) -> None:
        """A torrent blocked after a file failed verification may be picked again (`flotilla unblock`)."""
        with self.lock:
            gone = unblock(self.state.data, b.get("target"))
            self._save_now()
        if gone:
            self.log(f"unblocked {len(gone)} torrent(s) ({', '.join(ih[:12] for ih in gone[:5])}"
                     f"{'…' if len(gone) > 5 else ''}); replanning")
            self.commands.put(("replan_soon", {}))

    def _do_unskip(self, b: dict) -> None:
        repo = b["repo"]
        with self.lock:
            if repo in self.state.data["dropped"]:
                self.state.data["dropped"].remove(repo)
            self._save_now()
        self.log(f"restored {repo}: it can be carried again; replanning")
        self.commands.put(("replan_soon", {}))

    def _do_drop(self, b: dict) -> None:
        ih = b["infohash"]
        repo = self.state.holdings.get(ih, {}).get("repo", ih)
        with self.lock:
            if repo not in self.state.data["dropped"]:
                self.state.data["dropped"].append(repo)
            self._remove(ih, delete=True)
            self.cand_info.pop(ih, None)
            self._save_now()
        self.log(f"dropped {repo} and deleted its files; it won't be picked again")

    # ---------------------------------------------------------- history and ledger

    def sample(self, now: float, force: bool = False) -> None:
        """Once per clock minute, add the bytes each torrent moved since the last sample to the history.

        One sample per clock minute, not one per 60 s: a sample due "60 s after the last one"
        lands a few seconds late each time, drifts off the minute grid and leaves an empty
        minute (charted as "not seeding") every quarter hour or so. Called from its own thread
        (_sample_loop), so a refresh that blocks the main loop for minutes leaves no gap either.

        libtorrent's per-torrent totals count from when the torrent was added; a total that
        went down means the torrent was re-added, so the new total is all new bytes."""
        if not force and now // 60 == self._last_sample // 60:
            return
        # Count time online only while seeding, and never a long gap (the computer was asleep).
        prev = self._last_sample or self.run_started   # the first sample covers the time since startup
        online = 0.0 if self.ses.is_paused() else max(0.0, min(now - prev, 120.0))
        self._last_sample = now
        moved, names = {}, {}
        with self.lock:
            # A model's per-file swarms count as the model's own upload.
            every = [*((ih, ih, h) for ih, h in self.handles.items()), *self.extra_handles()]
            for key, ih, h in every:
                st = self.status(key, h, fresh=True)      # the minute's totals, to the second
                u, d = st.total_payload_upload, st.total_payload_download
                pu, pd = self._hist_seen.get(key, (0, 0))
                du, dd = (u - pu if u >= pu else u), (d - pd if d >= pd else d)
                self._hist_seen[key] = (u, d)
                if du or dd:
                    mu, md = moved.get(ih, (0, 0))
                    moved[ih] = (mu + du, md + dd)
                    hold = self.state.holdings.get(ih)
                    if hold:
                        names[ih] = hold["repo"]
                    elif key.startswith("index:"):
                        names[ih] = INDEX_REPO
            for key in set(self._hist_seen) - {k for k, _, _ in every}:
                del self._hist_seen[key]
            # Pieces from Hugging Face reach libtorrent through add_piece, which its download
            # counters don't count: add them here so the history shows everything fetched.
            for ih, n in self.hf.take_new().items():
                du, dd = moved.get(ih, (0, 0))
                moved[ih] = (du, dd + n)
                hold = self.state.holdings.get(ih)
                if hold:
                    names[ih] = hold["repo"]
        try:
            self.history.record(now, moved, online, names)
        except Exception as e:   # a full or locked disk must not stop seeding
            self.log(f"history: couldn't record ({e})")

    def _earning_gb(self, ih: str, h: lt.torrent_handle) -> float:
        """The GB of a model that earn Pirate Face seeding points now: all of it while every piece is
        here, else 0. Pirate Face credits a complete copy only: "finished" (the files picked are here,
        others aren't) earns nothing, so only "seeding" (every piece of the torrent) counts."""
        hold = self.state.holdings.get(ih)
        if not hold:
            return 0.0
        gb = sum(e["size"] for e in hold["files"].values() if e["status"] == "verified") / GB
        return gb if gb > 0 and "seeding" in str(self.status(ih, h).state) else 0.0

    def update_ledger(self, now: float) -> None:
        """Seed-hours per torrent and a points estimate with Pirate Face's published
        formula: 1 point per 3 seed-hours for a 10 GB torrent, scaled by sqrt(GB/10), counted
        only while this node holds the whole torrent (Pirate Face credits complete copies).
        Flotilla's own count: Pirate Face counts the real one through the account's seeding tracker.
        `points_since` is the part of it earned while the account now connected was announced to."""
        last = self.state.data.get("ledger_at") or now
        self.state.data["ledger_at"] = now
        seen, ident = self.pf_account.ident()
        since = self.state.data.get("points_since")
        started = seen and (since or {}).get("acct") != ident      # connected, replaced or disconnected
        if started:
            since = {"acct": ident, "since": now, "est": 0.0} if ident else None
            self.state.data["points_since"] = since
            self._dirty = True
        dt_h = max(0.0, min(now - last, 600)) / 3600   # cap gaps from sleep or crashes
        if dt_h == 0 or self.ses.is_paused():
            return
        led = self.state.data["ledger"]
        # The time up to `now` was seeded before a just-connected account was: it starts counting from here.
        announced = self._tracker_in_use[1] is not None and not started
        for ih, h in self.handles.items():
            gb = self._earning_gb(ih, h)
            if not gb:
                continue
            e = led.setdefault(ih, {"repo": self.state.holdings[ih]["repo"], "seed_hours": 0.0, "gb_hours": 0.0,
                                    "points_est": 0.0})
            e["seed_hours"] += dt_h
            e["gb_hours"] += gb * dt_h
            e["points_est"] += dt_h / 3 * math.sqrt(gb / 10)
            if since and announced:
                since["est"] += dt_h / 3 * math.sqrt(gb / 10)

    def check_announces(self, now: float) -> None:
        """Whether the account's tracker is answering, read from libtorrent every ANNOUNCE_CHECK_EVERY:
        having its address isn't the same as it counting anything (a pasted address with a wrong token
        looks the same until the tracker refuses it). Models only; the engine's thread."""
        url = self._tracker_in_use[1]
        if not url:
            self._announce = None
            return
        if self._announce and self._announce["gen"] == self._tracker_in_use[0] and now - self._announce_at < ANNOUNCE_CHECK_EVERY:
            return
        self._announce_at = now
        n = {"ok": 0, "failing": 0, "waiting": 0}
        why = None
        for h in list(self.handles.values()):
            try:
                entry = next((t for t in h.trackers() if t["url"] == url), None)
            except RuntimeError:         # removed meanwhile
                continue
            state, said = announce_state(entry) if entry else ("waiting", None)
            n[state] += 1
            why = why or said
        token = url.rsplit("/", 2)[-2]
        if why and (url in why or token in why):      # never show the tracker's token, whatever it says
            why = None
        self._announce = {**n, "why": why, "at": now, "gen": self._tracker_in_use[0]}

    def points_view(self, rows: list[dict]) -> dict:
        """What the dashboard and `flotilla points` say about points: the rate the models held whole
        earn at Pirate Face's published seeding rate (0 while paused), how many of the models that
        is, Flotilla's own estimate (all time, and since this account was connected), the handle to
        link to, and whether the account's tracker is answering. Never the tracker or the key."""
        since = self.state.data.get("points_since") or {}
        ann = self._announce
        return {"per_day": 0.0 if self.ses.is_paused() else sum(r["points_day"] for r in rows),
                "counting": sum(1 for r in rows if r["points_day"]), "models": len(rows),
                "est": sum(e["points_est"] for e in self.state.data["ledger"].values())
                + self.state.data.get("ledger_retired", 0.0),
                "since": since.get("since"), "est_since": since.get("est"),
                "handle": self.cfg.pf_handle or None,
                "announce": {k: ann[k] for k in ("ok", "failing", "waiting", "why", "at")} if ann else None}

    def tick(self) -> None:
        # Each step on its own: one that fails (a full disk under state.json, say) is logged and
        # the others still run, this time and every time after.
        self._guarded("alerts", lambda: self._handle_alerts(self.ses.pop_alerts()))
        self._guarded("blocking failed torrents", self._handle_failures)
        self._guarded("commands", self.process_commands)
        self._guarded("seeding tracker", self._apply_seeding_tracker)
        self._guarded("restarting stopped torrents", self._retry_errored, time.time())
        self._guarded("per-file swarms", self.perfile.tick, time.time())
        self._guarded("magnet index", self.index.tick)

    def _pirateface_loop(self) -> None:
        """The account key: re-read when it changes, its seeding tracker asked for when due."""
        while not self.stop.is_set():
            try:
                self.pf_account.poll()
            except Exception as e:      # never let a surprise end the thread
                self.log(f"pirate face: {e}")
            self._pf_nudge.wait(60)
            self._pf_nudge.clear()

    def _vpn_port_loop(self) -> None:
        """Keep listening on the port the VPN forwards, as it hands out new ones (vpnport.py). Own thread."""
        finder = PortFinder(self.cfg.bind_interface, port_file=self.cfg.vpn_port_file, log=self.log)
        mem = self._vpn_port_memory(f"{self.cfg.bind_interface}|{finder.provider}")
        finder.had = mem["port"]
        said = None
        while not self.stop.is_set():
            try:
                fw = finder.find(self.listening_on)
            except Exception as e:      # never let a surprise end the thread
                fw = Forward(note=f"couldn't look for a forwarded port: {e}")
            self._track_vpn_port(mem, fw, time.time())
            finder.had = mem["port"]
            self.vpn_forward = fw
            if fw.port:
                if (fw.listen, fw.port) != (self.listening_on, self._announced):
                    self._listen_on(fw)
                said = None
            elif fw.note != said:
                self.log(f"vpn port: none forwarded ({fw.note})")
                said = fw.note
            now = time.time()
            if (self.cfg.vpn_port_recover and finder.gluetun and not fw.port and mem["lost"]
                    and now - mem["lost"] >= RECOVER_AFTER and now >= mem.get("next_try", 0.0)
                    and self._recover_vpn_port(mem, finder.gluetun_auth, now)):
                self.stop.wait(20)           # gluetun reconnects in seconds: look for its new port soon
                continue
            self.stop.wait(fw.renew)

    _announced = 0

    def _vpn_port_memory(self, vpn: str) -> dict:
        """The port this VPN last forwarded and when it stopped, kept in state across runs: a port
        that went away is a fault to warn about, one never given is just how the VPN is. Starts
        afresh for another VPN (another interface or gluetun provider)."""
        mem = self.state.data.get("vpn_port")
        if not isinstance(mem, dict) or mem.get("vpn") != vpn:
            mem = self.state.data["vpn_port"] = {"vpn": vpn, "port": 0, "seen": 0.0, "lost": 0.0}
        return mem

    def _track_vpn_port(self, mem: dict, fw: Forward, now: float) -> None:
        if fw.port:
            if mem["lost"]:
                self.log(f"vpn port: forwarded again after {(now - mem['lost']) / 60:.0f} min without one")
            mem.update(port=fw.port, seen=now, lost=0.0, tries=0, next_try=0.0)
        elif mem["port"] and not mem["lost"]:
            mem["lost"] = now
            self.log(f"vpn port: LOST: the VPN no longer forwards port {mem['port']}; peers can't connect in "
                     f"until it forwards one again")

    def _recover_vpn_port(self, mem: dict, auth: str, now: float) -> bool:
        """vpn_port_recover: have gluetun reconnect its VPN, so it asks for a port again. True if it did."""
        tries = mem.get("tries", 0) + 1
        wait = min(RECOVER_EVERY[1], RECOVER_EVERY[0] * 2 ** (tries - 1))
        mem.update(tries=tries, last_try=now, next_try=now + wait)
        then = f"if it still has none, the next try is in {wait / 3600:g} h"
        try:
            gluetun_restart_vpn(auth)
        except GluetunAuth:
            self.log(f"vpn port: can't have gluetun reconnect: its control server won't let Flotilla's key at "
                     f"{RESTART_ROUTE}. Add that route to the key's role (routes, in gluetun's auth config.toml), "
                     f"or turn vpn_port_recover off; {then}")
            return False
        except GluetunAbsent as e:
            self.log(f"vpn port: couldn't have gluetun reconnect ({e}); {then}")
            return False
        self.log(f"vpn port: had gluetun reconnect its VPN to ask for a port again (try {tries}); {then}")
        return True

    def vpn_port_view(self) -> dict | None:
        """The forwarded port for status, with `lost` ({port, since}) while one the VPN forwarded is gone."""
        if not self.vpn_forward:
            return None
        view = self.vpn_forward.view()
        mem = self.state.data.get("vpn_port") or {}
        if not view["port"] and mem.get("lost"):
            view["lost"] = {"port": mem["port"], "since": mem["lost"], "recover": bool(self.cfg.vpn_port_recover),
                            "tries": mem.get("tries", 0), "last_try": mem.get("last_try") if mem.get("tries") else None}
        return view

    def port_view(self) -> dict:
        """The port libtorrent listens on, the one Flotilla asked for, and whether the VPN's forwarded
        port is the one in use (if not, peers can't connect in through the VPN)."""
        actual = self.ses.listen_port()
        fw = self.vpn_forward
        return {"port": actual, "wanted": self.listening_on, "moved": bool(actual) and actual != self.listening_on,
                "forwarded_taken": bool(fw and fw.port and actual and actual != fw.listen)}

    def _note_port(self) -> None:
        """Say once when the port asked for was taken and libtorrent listens on another."""
        view = self.port_view()
        key = (view["wanted"], view["port"])
        if key == self._port_noted or not view["port"]:
            return
        self._port_noted = key
        if view["forwarded_taken"]:
            self.log(f"port {view['wanted']}, the one the VPN forwards, is taken by another program on this machine "
                     f"(another torrent client?): listening on {view['port']} instead, so peers can't connect in "
                     f"through the VPN. Give the other program a different port, or stop it.")
        elif view["moved"]:
            self.log(f"port {view['wanted']} is taken by another program on this machine (another torrent client?): "
                     f"listening on {view['port']} instead. Set listen_port to keep a port of your own.")

    def _listen_on(self, fw: Forward) -> None:
        """Listen on the forwarded port (and tell trackers the public one, when the VPN maps it to
        another port here), then announce again so the swarm learns it now, not in half an hour."""
        self.ses.apply_settings({"listen_interfaces": listen_interfaces(self.cfg, fw.listen),
                                 "announce_port": fw.port if fw.port != fw.listen else 0})
        self.listening_on, self._announced = fw.listen, fw.port
        where = f" (to {fw.listen} here)" if fw.port != fw.listen else ""
        self.log(f"vpn port: {fw.source} forwards port {fw.port}{where}; peers can connect in")
        for h in self.ses.get_torrents():
            try:
                h.force_reannounce()
                h.force_dht_announce()
            except RuntimeError:        # removed meanwhile
                pass

    def _guarded_handles(self) -> list[tuple[str, object]]:
        """What the disk guard pauses: the models, and the magnet index's own download (its builds
        live in the state folder, usually on the same disk, and it isn't one of self.handles)."""
        out = list(self.handles.items())
        index = getattr(self, "index", None)
        if index is not None:
            out += [(key, h) for key, _, h in index.handles()]
        return out

    def guard_disk(self, free: int | None = None) -> None:
        """Keep the reserve free *while running*, not just at plan time: other
        programs fill disks too. Below the reserve, torrents still downloading
        are paused; finished ones keep seeding (seeding doesn't write)."""
        if free is None:
            try:
                free = shutil.disk_usage(self.cfg.folder).free
            except OSError:
                return
        reserve = self.cfg.reserve_gb * GB
        if free < reserve and not self.disk_paused:
            self.disk_paused = True
            n = 0
            for ih, h in self._guarded_handles():
                if not self.status(ih, h).is_finished:
                    h.pause()
                    n += 1
            self.log(f"disk: only {free / GB:.1f} GB free, below the {self.cfg.reserve_gb:g} GB reserve; "
                     f"paused {n} download(s), finished files keep seeding")
        elif self.disk_paused and free >= reserve + 2 * GB:
            self.disk_paused = False
            for _, h in self._guarded_handles():
                h.resume()
            self.log(f"disk: {free / GB:.1f} GB free again; downloads resumed")

    @property
    def session_uploaded(self) -> int:
        return self.state.data["upload"]["bytes"] - self.session_up_start

    def scan_upload_peers(self, now: float) -> None:
        """Remember every peer we've sent payload to this run (sampled once a minute)."""
        if now - self._last_peer_scan < 60:
            return
        self._last_peer_scan = now
        inbound = outbound = punched = 0
        for h in [*self.handles.values(), *(c[2] for c in self.extra_handles())]:
            for p in h.get_peer_info():
                if p.total_upload > 0 and len(self.upload_peers) < UPLOAD_PEERS_MAX:
                    pid = p.pid.to_bytes()
                    self.upload_peers.add(pid if any(pid) else str(p.ip).encode())
                if p.flags & (lt.peer_info.connecting | lt.peer_info.handshake):
                    continue
                if p.flags & lt.peer_info.holepunched:
                    punched += 1
                if p.flags & lt.peer_info.outgoing_connection:
                    outbound += 1
                elif _from_internet(p.ip[0]) and p.total_upload + p.total_download >= INBOUND_MIN_BYTES:
                    inbound += 1    # (one on your own network says nothing about the internet)
        self.peers_in, self.peers_out, self.peers_holepunched = inbound, outbound, punched
        if inbound:
            self.state.data["inbound"]["last"] = time.time()   # (`now` runs ahead at shutdown)
        self.set_dial_out(not self.reachable())

    def reach_view(self) -> dict:
        last = self.state.data["inbound"]["last"]
        return {"reachable": self.reachable(), "dialing_out": self.dialing_out, "peers_in": self.peers_in,
                "peers_out": self.peers_out, "peers_holepunched": self.peers_holepunched,
                "inbound_last": last or None}

    def reachable(self, now: float | None = None) -> bool:
        """Can peers start connections to this node? A port the VPN forwards says so at once; otherwise
        a peer that connected in within REACHABLE_FOR."""
        if self.vpn_forward and self.vpn_forward.port:
            return True
        return (now or time.time()) - self.state.data["inbound"]["last"] < REACHABLE_FOR

    def set_dial_out(self, on: bool) -> None:
        if on == self.dialing_out:
            return
        self.dialing_out = on
        self.ses.apply_settings(DIAL_OUT if on else self._dial_defaults)
        self.log("peers can't connect in: dialing out harder and announcing more often" if on
                 else "peers can connect in: back to libtorrent's usual dialing")

    def dial_out_announce(self, now: float) -> None:
        """While nobody can reach this node, ask trackers for peers more often than their usual
        interval (never sooner than the tracker's own minimum: force_reannounce keeps to it)."""
        if not self.dialing_out or now - self._last_dial_announce < DIAL_OUT_ANNOUNCE:
            return
        self._last_dial_announce = now
        for h in [*self.handles.values(), *(c[2] for c in self.extra_handles())]:
            try:
                h.force_reannounce()
            except RuntimeError:
                pass

    def report_progress(self) -> None:
        """One `jobq:progress` line per percent when running under the job queue.

        While downloading: percent of the assignment fetched. On a timed run
        with everything fetched: percent of the seeding time elapsed."""
        if not os.environ.get("JOBQ_JOB_ID"):
            return
        done = wanted = 0
        for ih, h in self.handles.items():
            st = self.status(ih, h)
            done += st.total_wanted_done
            wanted += st.total_wanted
        if wanted and done < wanted:
            pct = int(100 * done / wanted)
            msg = f"downloaded {done / GB:.1f} of {wanted / GB:.1f} GB"
        elif self.run_until:
            span = max(1.0, self.run_until - self.run_started)
            pct = int(100 * min(1.0, (time.time() - self.run_started) / span))
            msg = (f"seeding {(time.time() - self.run_started) / 3600:.1f} of {span / 3600:.1f} h, "
                   f"uploaded {self.session_uploaded / 1e6:.1f} MB to {len(self.upload_peers)} peer(s)")
        else:
            return
        if pct != self._last_pct:
            self._last_pct = pct
            print(f"jobq:progress {pct}% {msg}", flush=True)

    def run(self, once: bool = False, until: float | None = None) -> None:
        """Seed until stopped. `once`: exit when every assigned file is verified.
        `until`: exit at this Unix time (a timed seeding run)."""
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        try:
            self._run(once, until)
        finally:
            # However the loop ends (done, time up, stopped, or something it couldn't survive):
            # the last resume data, state and history are written and the threads are stopped.
            self.shutdown()

    def _run(self, once: bool, until: float | None) -> None:
        self._main_ident = threading.get_ident()
        self.run_started, self.run_until = time.time(), until
        self.session_up_start = self.state.data["upload"]["bytes"]
        if self.cfg.dashboard:
            from .dashboard import Dashboard

            from .access import base_address, describe, listen_for
            from .remote import dashboard_key

            access = self.cfg.dashboard_access
            host, keyed = listen_for(access)
            try:
                key = dashboard_key(self.state.dir) if keyed else None
                self.dashboard = Dashboard(self, self.cfg.dashboard_port, host=host, key=key, access=access)
                self.dashboard.start()
                if access == "public":
                    from .access import PUBLIC_WARNING

                    self.log(f"WARNING: {PUBLIC_WARNING}")
                if key:   # the key stays out of the log; `flotilla dashboard --url` prints it
                    self.log(f"dashboard: {base_address(access, self.dashboard.port)}"
                             f" ({describe(access)}; `flotilla dashboard --url` prints it with its access key)")
                else:
                    self.log(f"dashboard: {self.dashboard.url}")
            except OSError as e:
                self.log(f"dashboard not started (port {self.cfg.dashboard_port}: {e})")
        self.resume_holdings()
        threading.Thread(target=self._remote_loop, name="remote-dashboard", daemon=True).start()
        threading.Thread(target=self._memory_loop, name="memory", daemon=True).start()
        threading.Thread(target=self._update_loop, name="updates", daemon=True).start()
        threading.Thread(target=self._pirateface_loop, name="pirate-face", daemon=True).start()
        if self.cfg.bind_interface and self.cfg.vpn_port_forwarding and not self.listen_host:
            threading.Thread(target=self._vpn_port_loop, name="vpn-port", daemon=True).start()
        threading.Thread(target=self.perfile.run, name="per-file", daemon=True).start()
        # Always running: it does nothing while hf_fallback is off, and the dashboard can turn it on.
        threading.Thread(target=self.hf.run, name="hugging-face", daemon=True).start()
        self._sampler = threading.Thread(target=self._sample_loop, name="history", daemon=True)
        self._sampler.start()
        if self.state.holdings and not once:
            wait = random.uniform(0, STARTUP_SPREAD)
            self.next_refresh = time.time() + wait
            self.log(f"seeding what's held; first look at the swarm in {wait / 60:.0f} min "
                     f"(spread out, so restarted nodes don't all ask at once)")
        else:
            self._start_refresh()
            self.next_refresh = self._next_refresh_at()
        last_status = last_resume = 0.0
        last_save = last_status_file = time.time()
        guarded = self._guarded

        def save_state(now: float) -> None:
            nonlocal last_save
            with self.lock:
                self._account_upload()
                self.update_ledger(now)
                self.check_announces(now)
                # Upload totals and seed-hours change every few seconds but losing a few
                # minutes of them to a crash costs little; rewriting state.json (hundreds of
                # KB) every 10 s cost gigabytes of disk writes a day.
                if self._dirty or now - last_save >= SAVE_EVERY:
                    self.state.save()
                    self._dirty, last_save = False, now

        # Every step is guarded (_guarded): a full or read-only disk fails the state, status and
        # resume writes, and that must not end the process with nothing saved; it seeds on, logs
        # the failure now and then, and the writes succeed again once there's room.
        while not self.stop.is_set():
            guarded("waiting for libtorrent", self.ses.wait_for_alert, 1000)
            self.tick()
            now = time.time()
            if now - last_status >= STATUS_EVERY:
                guarded("saving state", save_state, now)
                guarded("disk guard", self.guard_disk)
                guarded("counting peers", self.scan_upload_peers, now)
                guarded("announcing", self.dial_out_announce, now)
                guarded("progress", self.report_progress)
                if now - last_status_file >= STATUS_FILE_EVERY:
                    guarded("writing status.json", self.write_status)
                    last_status_file = now
                last_status = now
            if now - last_resume >= RESUME_EVERY:
                guarded("saving resume data", self.save_resume)
                last_resume = now
            if once and (self._plans_applied or self._refresh_failed) and guarded("checking", self.all_done):
                break
            if until and now >= until:
                self.log("seeding time is up")
                break
            if now >= self.next_refresh:
                if self._refresher is not None and self._refresher.is_alive():
                    self.next_refresh = now + 30          # one is still running: ask again shortly
                else:
                    guarded("starting a refresh", self._start_refresh)
                    self.next_refresh = guarded("scheduling", self._next_refresh_at) or now + 3600

    def refresh_interval(self) -> float:
        """refresh_hours, give or take 15%, so a fleet of nodes doesn't hit
        Pirate Face in lockstep."""
        return self.cfg.refresh_hours * 3600 * random.uniform(0.85, 1.15)

    def all_done(self) -> bool:
        with self.lock:
            return all(e["status"] in ("verified", "failed")
                       for h in self.state.holdings.values() for e in h["files"].values())

    def save_resume(self) -> int:
        n = 0
        for h in self.handles.values():
            if h.is_valid() and h.need_save_resume_data():
                h.save_resume_data(lt.torrent_handle.save_info_dict)
                n += 1
        return n

    def shutdown(self) -> None:
        """Stop: the last history sample, resume data, status and state, then close. Each step on its
        own, so one that fails (a full disk) doesn't skip the rest. Once only: run() calls it however
        it ends, and a caller may too."""
        if self._shut:
            return
        self._shut = True
        self.stop.set()   # run() can also end on once/until; stop the helper threads either way
        self._pf_nudge.set()
        guarded = self._guarded
        if self._sampler is not None:
            self._sampler.join(timeout=10)
            self._sampler = None
        guarded("history", self.sample, time.time(), True)   # the last partial minute

        def settle():
            with self.lock:
                self._account_upload()
            self.scan_upload_peers(time.time() + 3600)
            hours = (time.time() - self.run_started) / 3600
            self.log(f"this run: {hours:.2f} h, uploaded {self.session_uploaded / 1e6:.1f} MB "
                     f"to {len(self.upload_peers)} distinct peer(s)")

        guarded("upload totals", settle)
        self.log("shutting down: saving resume data")

        def resume():
            self.ses.pause()
            pending = self.save_resume()
            end = time.monotonic() + 15
            while pending > 0 and time.monotonic() < end:
                for a in self.ses.pop_alerts():
                    if isinstance(a, (lt.save_resume_data_alert, lt.save_resume_data_failed_alert)):
                        pending -= 1
                    self._handle_alerts([a])
                time.sleep(0.1)

        guarded("saving resume data", resume)
        guarded("writing status.json", self.write_status)

        def save():
            with self.lock:
                self.state.save()

        guarded("saving state", save)
        if self.dashboard:
            guarded("stopping the dashboard", self.dashboard.stop)
            self.dashboard = None
        guarded("closing the history", self.history.close)
