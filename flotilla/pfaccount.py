"""Seeding credit: a Pirate Face account key the user chose to connect, for Pirate Face points.

Pirate Face counts seeding through a tracker of each account's own (a `:6970/<token>/announce`
URL that /api/trackers gives the account's key). Torrents that also announce to it earn that
account points. Connecting is opt-in (`flotilla points connect`), because it ties what this node
seeds, from the address peers see, to the account:

* The key is kept in its own file, `pirateface-key` next to config.toml (mode 0600), or the file
  FLOTILLA_PF_KEY_FILE names (a Docker secret, say). Flotilla doesn't read PIRATEFACE_KEY or the
  key the official `pf` CLI saved: connecting is a choice made for Flotilla alone.
* The key is sent only to pirateface.co, only to ask for the seeding tracker, over the same
  network pin as torrent traffic. The tracker URL carries a token of its own, so it's never
  logged or shown: only its host is.
* The tracker is accepted only on the hosts torrents may announce to (tracker.pirateface.co).
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

from .magnet import DEFAULT_TRACKER_HOSTS
from .netsafe import FetchError, host_allowed

KEY_FILE = "pirateface-key"
KEY_RE = re.compile(r"pfct_[A-Za-z0-9_-]{8,200}")
SETTINGS_PAGE = "https://pirateface.co/settings"
# What /api/trackers gives as an account's own tracker (pf's user.SEEDING_TRACKER, tightened).
SEEDING_TRACKER = re.compile(r"https?://[A-Za-z0-9.-]+:6970/[A-Za-z0-9._~-]+/announce")
REFETCH_EVERY = 12 * 3600.0     # ask again this often: the site may rotate the tracker
RETRY_AFTER_ERROR = 900.0


class PFKeyError(ValueError):
    """This key can't be used; the message says why and what to do."""


def key_path(config_path: Path | None) -> Path | None:
    """Where the key is kept: FLOTILLA_PF_KEY_FILE, else pirateface-key next to the config file."""
    env = os.environ.get("FLOTILLA_PF_KEY_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    return config_path.parent / KEY_FILE if config_path else None


def clean_key(raw: str) -> str:
    k = raw.strip()
    if not KEY_RE.fullmatch(k):
        raise PFKeyError(f"that isn't a Pirate Face key (they start with pfct_; yours is at {SETTINGS_PAGE})")
    return k


def read_key(path: Path | None) -> str | None:
    """The key in `path`, or None if there's no file. PFKeyError if the file holds something else."""
    if path is None:
        return None
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    return clean_key(raw)


def tracker_host(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.hostname}:{p.port}" if p.port else str(p.hostname)


def seeding_tracker(lines: list[str], tracker_hosts: tuple[str, ...] = DEFAULT_TRACKER_HOSTS) -> str:
    """The account's own tracker from /api/trackers' answer (the last line). PFKeyError if there's none."""
    mine = lines[-1] if lines else ""
    if not SEEDING_TRACKER.fullmatch(mine):
        raise PFKeyError("Pirate Face gave no seeding tracker for this key: check it at " + SETTINGS_PAGE)
    host = urllib.parse.urlsplit(mine).hostname or ""
    if not host_allowed(host, tracker_hosts):
        raise PFKeyError(f"Pirate Face's seeding tracker is on {host}, which isn't a tracker Flotilla announces to "
                        f"({', '.join(tracker_hosts)})")
    return mine


def check(key: str, pf) -> str:
    """Ask Pirate Face for this key's seeding tracker. PFKeyError if it refuses the key; FetchError
    if it can't be reached. `pf` is a sources.PirateFace."""
    try:
        lines = pf.trackers(key)
    except FetchError as e:
        if e.status in (401, 403):
            raise PFKeyError(f"Pirate Face didn't accept this key: check it at {SETTINGS_PAGE}") from e
        raise
    return seeding_tracker(lines)


class Account:
    """The running node's seeding credit. The key file is re-read when it changes (so `flotilla points
    connect` and `disconnect` take effect without a restart), and the tracker is asked for again
    every REFETCH_EVERY. `generation` goes up whenever the tracker in use changes. Thread-safe."""

    def __init__(self, path: Path | None, pf_factory, log=print):
        self.path = path
        self.pf_factory = pf_factory      # () -> sources.PirateFace
        self.log = log
        self._lock = threading.Lock()
        self._sig: object = None
        self.key: str | None = None
        self.url: str | None = None       # the seeding tracker: secret, never logged
        self.state = "none"               # none / unchecked / ok / refused / error
        self.why: str | None = None
        self._next = 0.0
        self.generation = 0

    def _reread(self) -> bool:
        if self.path is None:
            return False
        try:
            st = self.path.stat()
            sig: object = (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            sig = None
        except OSError as e:
            sig = ("error", str(e))
        if sig == self._sig:
            return False
        self._sig = sig
        key, state, why = None, "none", None
        if sig is not None:
            try:
                key = read_key(self.path)
                state = "unchecked" if key else "none"
            except (PFKeyError, OSError) as e:
                state, why = "refused", f"the key file {self.path} can't be used: {e}"
        with self._lock:
            if key == self.key and state == self.state:
                return False
            had = self.url
            self.key, self.state, self.why, self._next = key, state, why, 0.0
            self.url = None
            if had:
                self.generation += 1
        if state == "none" and had:
            self.log("pirate face: account disconnected; seeding no longer counts toward its points")
        elif why:
            self.log(f"pirate face: {why}")
        return True

    def poll(self, now: float | None = None) -> None:
        """Re-read the key file, and ask for the tracker when it's due. Called from a worker thread."""
        now = time.time() if now is None else now
        self._reread()
        with self._lock:
            key, due = self.key, self.key is not None and now >= self._next
        if not due:
            return
        try:
            url = check(key, self.pf_factory())
        except PFKeyError as e:
            self._set(key, None, "refused", str(e), now + REFETCH_EVERY)
            return
        except FetchError as e:
            # Keep using the tracker we have: Pirate Face being down doesn't stop it counting.
            self._set(key, self.url, "error" if self.url is None else "ok",
                      f"couldn't reach Pirate Face to get your seeding tracker ({e})", now + RETRY_AFTER_ERROR)
            return
        self._set(key, url, "ok", None, now + REFETCH_EVERY)

    def _set(self, key, url, state, why, next_at) -> None:
        with self._lock:
            if key != self.key:          # disconnected or replaced meanwhile
                return
            said = (self.state, self.why)
            changed = url != self.url
            self.url, self.state, self.why, self._next = url, state, why, next_at
            if changed:
                self.generation += 1
        if changed and url:
            self.log(f"pirate face: seeding counts toward your account's points (announcing to {tracker_host(url)})")
        elif changed:
            self.log("pirate face: seeding no longer counts toward your account's points")
        if why and (state, why) != said:
            self.log(f"pirate face: {why}")

    def tracker(self) -> tuple[int, str | None]:
        with self._lock:
            return self.generation, self.url

    def info(self) -> dict:
        with self._lock:
            return {"state": self.state, "why": self.why, "tracker": tracker_host(self.url) if self.url else None,
                    "path": str(self.path) if self.path else None,
                    "from_env": bool(os.environ.get("FLOTILLA_PF_KEY_FILE", "").strip())}
