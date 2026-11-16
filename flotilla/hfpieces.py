"""Pieces no peer can supply, fetched from Hugging Face.

Pirate Face's torrents carry a web seed that redirects to the files on Hugging Face, but
libtorrent can't keep using it. Hugging Face's download links expire after an hour, and once a
web seed has redirected for a file, libtorrent never asks it for that file again: every file
not finished within the hour stalled for good. So Flotilla drops those web seeds and does the
job itself, only where it's needed:

* A wanted file is *stuck* when peers sent it less than STUCK_RATE over the last STUCK_WINDOW
  (bytes from Hugging Face don't count) while the line had room to spare.
* The missing pieces of a stuck file whose model is still on Hugging Face at the torrent's
  pinned commit are fetched one at a time, rarest first, with byte-range requests. Every
  request goes through netsafe (HTTPS, the host allowlist, redirects checked hop by hop, the
  network pin). A download link is reused until shortly before it expires, then a new one is
  asked for.
* Each piece is checked against the torrent's own SHA-1 piece hash before it's handed to
  libtorrent (add_piece), which checks it again and seeds it like any other piece. Never handing
  libtorrent a bad piece matters: Pirate Face's v2 torrents lack piece layers, so a bad last
  piece makes libtorrent throw away every piece of that file.
* At most Config.hf_gb_per_day a day (UTC), using only the download bandwidth peers leave free.
* Gated models (Hugging Face serves them only to accounts their author let in) are fetched only
  with an account token the user connected (hfauth), sent only for those models. Until a
  model is let in, it's asked about again hourly, not every scan.
"""

from __future__ import annotations

import collections
import hashlib
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone

import libtorrent as lt

from . import netsafe
from .hfauth import Connection, token_path
from .netsafe import FetchError, UnsafeURL
from .planner import GB
from .torrentmeta import parse_torrent

HF_BASE = "https://huggingface.co"
HF_API_HOST = "huggingface.co"
HF_HOSTS = ("huggingface.co", "hf.co")   # the site, and its download CDN (us.aws.cdn.hf.co, *.xethub.hf.co)
STUCK_WINDOW = 600.0        # seconds a file must get (almost) nothing from peers before Hugging Face steps in
STUCK_RATE = 32 * 1024      # bytes/s from peers below which a file counts as stuck
SCAN_EVERY = 30.0
MAX_BAD = 2                 # pieces failing the torrent's hash before a file stops using Hugging Face
LINK_MARGIN = 300.0         # stop using a download link this long before it expires
SETTLE = 30.0               # seconds to wait for libtorrent to take a piece before moving on
MIN_RATE = 256 * 1024       # never throttle below this, so a request doesn't time out
BACKOFF_FIRST, BACKOFF_MAX = 60.0, 900.0
GATED_RECHECK = 3600.0      # how often to ask again about a model Hugging Face won't serve us
_COMMIT = re.compile(r"[0-9a-f]{40}")


class Refused(Exception):
    """Hugging Face doesn't have this model any more (or disabled it): don't ask again this run."""


class Gated(Exception):
    """Hugging Face serves this model only to accounts let in, and ours isn't (or none is connected)."""

    def __init__(self, kind: str, why: str):
        super().__init__(why)
        self.kind = kind     # "account": no usable token connected; "access": the account isn't let in


class Later(Exception):
    """Hugging Face is busy or unreachable: try again after a while."""

    def __init__(self, seconds: float | None, why: str):
        super().__init__(why)
        self.seconds = seconds


def link_expires(url: str) -> float | None:
    """When a signed download link stops working (CloudFront's Expires, or S3's X-Amz-Date +
    X-Amz-Expires). None if it doesn't say: such a link isn't reused."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    try:
        if "Expires" in q:
            return float(q["Expires"][0])
        if "X-Amz-Date" in q and "X-Amz-Expires" in q:
            start = datetime.strptime(q["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return start.timestamp() + float(q["X-Amz-Expires"][0])
    except (ValueError, IndexError):
        pass
    return None


def file_url(repo: str, revision: str, repo_path: str) -> str:
    return f"{HF_BASE}/{urllib.parse.quote(repo, safe='/')}/resolve/{revision}/{urllib.parse.quote(repo_path, safe='/')}"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class HFPieces:
    """Runs on its own thread (run()). Reads the engine's handles and holdings under its lock;
    libtorrent handle calls are thread-safe, and alerts are left to the main loop."""

    def __init__(self, eng):
        self.eng = eng
        self.conn = Connection(token_path(eng.cfg.path), eng.log)
        self._conn_gen = 0
        self._needs_token: set[str] = set()                  # repos Hugging Face won't serve without one
        self.gated: dict[str, dict] = {}                     # ih -> {repo, kind, why, until}
        self._samples: dict[tuple[str, int], collections.deque] = {}   # (ih, file) -> (t, progress, from HF)
        self.stuck: dict[tuple[str, int], float] = {}       # (ih, file) -> since when
        self.active: list[tuple[str, int]] = []             # stuck files Hugging Face is fetching for
        self.fetched: collections.Counter = collections.Counter()    # (ih, file) -> bytes from HF this run
        self.bad: collections.Counter = collections.Counter()        # (ih, file) -> pieces that failed the hash
        self.off: dict = {}                                  # ih or (ih, file) -> why it isn't fetched
        self._links: dict[tuple[str, int], tuple[str, float]] = {}  # (ih, file) -> (download link, expires)
        self._meta: dict[str, object] = {}
        self._new = collections.Counter()                    # ih -> bytes not yet in the history
        self._recent: collections.deque = collections.deque()  # (monotonic, bytes) for the shown rate
        self._count_lock = threading.Lock()
        self._wait_until = 0.0
        self._backoff = 0.0
        self._said: set = set()
        self._limit, self._limit_at, self._clock = MIN_RATE, 0.0, 0.0

    # ------------------------------------------------------------------ loop

    def run(self) -> None:
        eng = self.eng
        while not eng.stop.is_set():
            deadline = time.monotonic() + SCAN_EVERY
            try:
                for key in self.scan(time.time()):
                    if eng.stop.is_set() or time.monotonic() >= deadline or time.time() < self._wait_until:
                        break
                    self.fetch_file(key, deadline)
            except Exception as e:   # never let this stop seeding
                eng.log(f"hugging face: {e}")
            eng.stop.wait(max(1.0, deadline - time.monotonic()))

    def scan(self, now: float) -> list[tuple[str, int]]:
        """Update which wanted files are stuck. Returns the ones to fetch, most important first."""
        eng = self.eng
        self.conn.refresh()
        if self.conn.generation != self._conn_gen:   # a token connected, replaced or removed:
            self._conn_gen = self.conn.generation    # ask about gated models again
            self._needs_token.clear()
            self._forget_gated(set(self.gated))
        with eng.lock:
            items = [(ih, h, hold, {int(k): e["size"] for k, e in hold["files"].items() if e["status"] == "wanted"})
                     for ih, h in eng.handles.items() if (hold := eng.state.holdings.get(ih))]
            blocked = eng.disk_paused or eng.user_paused
        if blocked or not eng.cfg.hf_fallback or eng.ses.is_paused():
            self._samples.clear()
            self.stuck.clear()
            self.active = []
            self._forget_gated(set(self.gated))
            return []
        peers_rate, progress = 0, {}
        for ih, h, _hold, wanted in items:
            try:
                st = h.status()
                peers_rate += st.download_payload_rate
                if not wanted or st.flags & lt.torrent_flags.paused:
                    continue
                prog, prios = h.file_progress(), h.get_file_priorities()
            except Exception:
                continue
            for i, size in wanted.items():
                if i < len(prog) and i < len(prios) and prios[i] > 0 and prog[i] < size:
                    progress[(ih, i)] = prog[i]
        saturated = peers_rate > 0.8 * eng.cfg.download_bps
        for key, done in progress.items():
            dq = self._samples.setdefault(key, collections.deque())
            dq.append((now, done, self.fetched[key]))
            while len(dq) > 1 and now - dq[1][0] >= STUCK_WINDOW:
                dq.popleft()
            t0, d0, f0 = dq[0]
            if now - t0 < STUCK_WINDOW or now <= t0:
                continue
            from_peers = (done - d0) - (self.fetched[key] - f0)
            if from_peers / (now - t0) < STUCK_RATE and (not saturated or key in self.stuck):
                self.stuck.setdefault(key, now)
            elif self.stuck.pop(key, None) is not None:
                self._said.discard(("start", key))
                if self.fetched[key]:
                    self._say_once(("again", key), f"peers are sending {self._name(key)} again; "
                                                   f"Hugging Face is no longer needed for it")
        for key in set(self._samples) - set(progress):     # finished, dropped or paused
            del self._samples[key]
            self.stuck.pop(key, None)
            self._said -= {("start", key), ("again", key)}
        for ih in set(self._meta) - {ih for ih, _ in self.stuck}:
            del self._meta[ih]
        self._forget_gated(set(self.gated) - {ih for ih, _ in self.stuck})

        prio = {ih: (eng.cand_info.get(ih) or {}).get("priority") or 0 for ih, _ in self.stuck}
        order = sorted(self.stuck, key=lambda k: (-prio[k[0]], self.stuck[k]))
        holds = {ih: hold for ih, _h, hold, _w in items}
        self.active = []
        for key in order:
            why = self.ineligible(key, holds.get(key[0]))
            if why:
                self._say_once(("off", key[0], why), f"{holds[key[0]]['repo'] if key[0] in holds else key[0][:12]}: "
                                                      f"no peer is sending it, and {why}; waiting for peers")
                continue
            self.active.append(key)
            self._say_once(("start", key), f"{self._name(key)}: peers sent it under {STUCK_RATE // 1024} KB/s "
                                           f"for {STUCK_WINDOW / 60:.0f} min; fetching its missing pieces from Hugging Face")
        return list(self.active)

    def ineligible(self, key: tuple[str, int], hold: dict | None) -> str | None:
        ih = key[0]
        if hold is None:
            return "it's no longer held"
        if ih in self.off:
            return self.off[ih]
        if key in self.off:
            return self.off[key]
        g = self.gated.get(ih)
        if g and time.time() < g["until"]:
            return g["why"]
        if not str(hold.get("verify_source") or "").startswith("huggingface"):
            return "Hugging Face doesn't have it at the commit the torrent was made from"
        if not _COMMIT.fullmatch(str(hold.get("revision") or "")):
            return "its torrent doesn't name the Hugging Face commit it was made from"
        return None

    # ------------------------------------------------------------------ fetching

    def fetch_file(self, key: tuple[str, int], deadline: float) -> None:
        """Fetch missing pieces of one stuck file until it's done, the deadline passes, or it
        isn't stuck any more (peers came back)."""
        eng = self.eng
        ih, idx = key
        with eng.lock:
            h = eng.handles.get(ih)
            hold = eng.state.holdings.get(ih)
            entry = (hold or {}).get("files", {}).get(str(idx))
            hold = dict(hold) if hold else None
        if h is None or not entry or entry["status"] != "wanted":
            return
        try:
            ti = h.torrent_file()
            meta = self._meta.get(ih) or parse_torrent((eng.torrent_dir / f"{ih}.torrent").read_bytes())
        except Exception as e:
            self.off[key] = f"its torrent couldn't be read ({e})"
            return
        self._meta[ih] = meta
        if ti is None or not ti.info_hashes().has_v1():
            self.off[ih] = "its torrent has no SHA-1 piece hashes to check Hugging Face's bytes against"
            return
        first, last = meta.files[idx].piece_range(meta.piece_length)
        avail = list(h.piece_availability())
        queued = {q["piece_index"] for q in h.get_download_queue()}
        missing = [p for p in range(first, last + 1) if not h.have_piece(p)]
        missing.sort(key=lambda p: (avail[p] if p < len(avail) else 0, p in queued, p))
        try:
            for p in missing:
                if eng.stop.is_set() or time.monotonic() >= deadline or key not in self.stuck \
                        or not eng.cfg.hf_fallback:
                    return
                if h.have_piece(p):
                    continue
                size = ti.piece_size(p)
                if not self._budget_allows(size):
                    return
                data = self.piece(hold, meta, ti, ih, p)
                if data is None:
                    self.bad[key] += 1
                    if self.bad[key] >= MAX_BAD:
                        self.off[key] = "Hugging Face's copy doesn't match the torrent"
                        eng.log(f"hugging face: {self._name(key)}: {self.bad[key]} pieces from Hugging Face failed "
                                f"the torrent's hash check; not fetching it from there again")
                        return
                    continue
                with eng.lock:
                    if eng.stop.is_set() or eng.handles.get(ih) is not h:
                        return
                    h.add_piece(p, data, 0)
                data = None          # libtorrent has its own copy (pieces can be 64 MiB)
                self._account(ih, ti, p, size)
                self._backoff = 0.0
                if self._forget_gated({ih}):
                    eng.log(f"hugging face: {hold['repo']}: Hugging Face serves it to us now; fetching")
                self._settle(h, p)
        except Refused as e:
            self.off[ih] = str(e)
            eng.log(f"hugging face: {hold['repo']}: {e}; waiting for peers")
        except Gated as e:
            self.gated[ih] = {"repo": hold["repo"], "kind": e.kind, "why": str(e), "until": time.time() + GATED_RECHECK}
            self._say_once(("off", ih, str(e)), f"{hold['repo']}: no peer is sending it, and {e}; waiting for "
                                                f"peers, and asking Hugging Face again every hour")
        except Later as e:
            self._backoff = min(BACKOFF_MAX, self._backoff * 2 if self._backoff else BACKOFF_FIRST)
            wait = max(e.seconds or 0.0, self._backoff)
            self._wait_until = time.time() + wait
            eng.log(f"hugging face: {e}; trying again in {wait / 60:.0f} min")

    def piece(self, hold: dict, meta, ti, ih: str, p: int) -> bytes | None:
        """Piece p's bytes from Hugging Face, or None if they don't match the torrent's hash."""
        parts = []
        for s in ti.map_block(p, 0, ti.piece_size(p)):
            tf = meta.files[s.file_index]
            parts.append(bytes(s.size) if tf.pad else
                         self._range(hold, ih, s.file_index, tf.repo_path, s.offset, s.size))
        data = parts[0] if len(parts) == 1 else b"".join(parts)   # one slice: no second copy
        del parts
        return data if hashlib.sha1(data).digest() == ti.hash_for_piece(p) else None

    def _range(self, hold: dict, ih: str, fi: int, repo_path: str, start: int, length: int) -> bytes:
        key, repo = (ih, fi), hold["repo"]
        cached = self._links.get(key)
        url = cached[0] if cached and cached[1] - LINK_MARGIN > time.time() else \
            file_url(repo, hold["revision"], repo_path)
        # Without the account token unless this model needs it (it's then sent only to huggingface.co).
        auth = None
        if repo in self._needs_token:
            auth = self.conn.auth()
            if auth is None:
                raise Gated("account", self._no_account_why())
        fresh_link = False
        for _ in range(3):
            try:
                data, final = netsafe.fetch_range(url, start, length, allowed=HF_HOSTS, auth=auth,
                                                  on_chunk=self._throttle)
            except UnsafeURL as e:
                raise Refused(f"Hugging Face sent us off the allowlist ({e})") from e
            except FetchError as e:
                host = (urllib.parse.urlsplit(e.url).hostname or "").lower()
                if host != HF_API_HOST and e.status in (400, 401, 403, 404, 410) and not fresh_link:
                    # An expired or revoked download link: ask Hugging Face for a new one.
                    fresh_link = True
                    self._links.pop(key, None)
                    url = file_url(repo, hold["revision"], repo_path)
                    continue
                if host == HF_API_HOST and e.headers.get("x-error-message") == "Access to this resource is disabled.":
                    raise Refused("Hugging Face has disabled it") from e
                if host == HF_API_HOST and e.status in (401, 403):
                    code = e.headers.get("x-error-code")
                    if auth is None:
                        auth = self.conn.auth()
                        if auth is None:
                            raise Gated("account", self._no_account_why()) from e
                        self._needs_token.add(repo)
                        url = file_url(repo, hold["revision"], repo_path)
                        continue
                    if e.status == 401 and code not in ("GatedRepo", "RepoNotFound"):
                        self.conn.rejected()
                        raise Gated("account", self._no_account_why()) from e
                    raise Gated("access", self._no_access_why(repo)) from e
                if host == HF_API_HOST and e.status in (404, 410):
                    raise Refused(f"it's no longer on Hugging Face at commit {hold['revision'][:12]} "
                                  f"(HTTP {e.status})") from e
                raise Later(e.retry_after, f"couldn't fetch from Hugging Face ({e})") from e
            exp = link_expires(final)      # a huggingface.co link has none: only signed CDN links are kept
            if exp:
                self._links[key] = (final, exp)
            return data
        raise Later(None, "Hugging Face kept sending expired download links")

    def _no_account_why(self) -> str:
        st = self.conn.info()
        if st["state"] in ("refused", "error"):
            return "Hugging Face serves it only to accounts let in, and the connected token can't be used right now"
        return "Hugging Face serves it only to accounts let in (gated or private); connect one that has access " \
               "with `flotilla hf connect`"

    def _no_access_why(self, repo: str) -> str:
        st = self.conn.info()
        who = f"{st['name']}'s account" if st["name"] else "the connected account"
        hint = "; a fine-grained token also needs read access to gated repos" if st["role"] == "fineGrained" else ""
        return f"Hugging Face hasn't let {who} in (request access at {HF_BASE}/{repo}{hint})"

    def _forget_gated(self, ihs: set) -> bool:
        """Stop remembering these models as gated (they may be fetched again at once)."""
        gone = False
        for ih in ihs:
            g = self.gated.pop(ih, None)
            if g:
                gone = True
                self._said.discard(("off", ih, g["why"]))
        return gone

    # ------------------------------------------------------------------ limits and accounting

    def _budget_allows(self, size: int) -> bool:
        eng = self.eng
        cap = eng.cfg.hf_gb_per_day * GB
        day = _today()
        with eng.lock:
            hf = eng.state.data.setdefault("hf", {"day": day, "bytes": 0, "total": 0})
            if hf.get("day") != day:
                hf["day"], hf["bytes"] = day, 0
            used = hf["bytes"]
        if cap and used + size > cap:
            self._say_once(("budget", day), f"hugging face: today's {eng.cfg.hf_gb_per_day:g} GB is used up; "
                                            f"waiting for peers until tomorrow (UTC)")
            return False
        return True

    def _account(self, ih: str, ti, p: int, size: int) -> None:
        eng = self.eng
        with eng.lock:
            hf = eng.state.data.setdefault("hf", {"day": _today(), "bytes": 0, "total": 0})
            hf["bytes"] = hf.get("bytes", 0) + size
            hf["total"] = hf.get("total", 0) + size
            hold = eng.state.holdings.get(ih)
            if hold is not None:
                hold["hf_bytes"] = hold.get("hf_bytes", 0) + size
        for s in ti.map_block(p, 0, size):
            self.fetched[(ih, s.file_index)] += s.size
        with self._count_lock:
            self._new[ih] += size
            self._recent.append((time.monotonic(), size))

    def _throttle(self, n: int) -> None:
        """Keep Hugging Face to the download bandwidth peers leave free (Config.download_bps)."""
        now = time.monotonic()
        if now - self._limit_at > 2.0:
            with self.eng.lock:
                handles = list(self.eng.handles.values())
            used = 0
            for h in handles:
                try:
                    used += h.status().download_payload_rate
                except Exception:
                    pass
            self._limit, self._limit_at = max(MIN_RATE, self.eng.cfg.download_bps - used), now
        self._clock = max(self._clock, now - 1.0) + n / self._limit
        if self._clock > now:
            time.sleep(self._clock - now)

    def _settle(self, h, p: int) -> None:
        end = time.monotonic() + SETTLE
        while time.monotonic() < end and not self.eng.stop.is_set():
            try:
                if h.have_piece(p):
                    return
            except Exception:
                return
            time.sleep(0.05)

    def take_new(self) -> dict[str, int]:
        """Bytes per torrent fetched since the last call (for the history)."""
        with self._count_lock:
            new, self._new = dict(self._new), collections.Counter()
        return new

    def info(self) -> dict:
        """What the dashboard shows."""
        eng = self.eng
        now = time.monotonic()
        with self._count_lock:
            while self._recent and now - self._recent[0][0] > 30:
                self._recent.popleft()
            recent = sum(n for _, n in self._recent)
        with eng.lock:
            hf = dict(eng.state.data.get("hf") or {})
            fetching = [self._name(k) for k in list(self.active)]
        return {"enabled": bool(eng.cfg.hf_fallback), "rate": recent / 30.0,
                "today": hf.get("bytes", 0) if hf.get("day") == _today() else 0,
                "cap": int(eng.cfg.hf_gb_per_day * GB), "total": hf.get("total", 0), "fetching": fetching,
                "retry_at": self._wait_until if self._wait_until > time.time() else None,
                "account": self.conn.info(), "gated": self.gated_list()}

    def gated_list(self) -> list[dict]:
        """Models no peer is sending that Hugging Face won't serve us yet, with where to ask for access."""
        return [{"repo": g["repo"], "kind": g["kind"], "url": f"{HF_BASE}/{g['repo']}", "until": g["until"]}
                for g in sorted(list(self.gated.values()), key=lambda g: g["repo"])]

    # ------------------------------------------------------------------ messages

    def _name(self, key: tuple[str, int]) -> str:
        hold = self.eng.state.holdings.get(key[0]) or {}
        entry = hold.get("files", {}).get(str(key[1])) or {}
        return f"{hold.get('repo', key[0][:12])}/{entry.get('path', key[1])}"

    def _say_once(self, tag, msg: str) -> None:
        if tag not in self._said:
            self._said.add(tag)
            self.eng.log(msg if msg.startswith("hugging face:") else f"hugging face: {msg}")
