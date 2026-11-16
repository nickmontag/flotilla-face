"""The dashboard of a node running in another process, through their shared state folder.

When Flotilla runs inside a VPN container (gluetun and the like), its torrents should go through
the VPN but its dashboard should stay reachable on your own network. So the dashboard can run
as a separate process (`flotilla dashboard --serve`) outside the container, sharing only the
state folder:

* the engine writes what the dashboard shows to `dashboard-state.json`, every couple of seconds
  while a dashboard has asked for it in the last half minute;
* the dashboard reads upload and download history straight from `history.db`;
* actions (pause, skip, settings...) go into `inbox/`, one small JSON file each. The engine
  checks them exactly as it checks its own dashboard's requests and writes the answer to
  `outbox/`.

Anyone who can write to the state folder can already change the config, so the folder itself is
the trust boundary; nothing here listens on the network.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

from .history import History

SNAPSHOT = "dashboard-state.json"
WANTED = "dashboard-state.wanted"
WANTED_FOR = 30.0        # the engine keeps writing snapshots this long after the last read
WRITE_EVERY = 2.0        # how often it writes them meanwhile
STALE_AFTER = 30.0       # a snapshot older than this means the engine isn't running
ANSWER_WITHIN = 15.0     # how long the dashboard waits for the engine to answer an action
MAX_REQUEST = 64 * 1024
REQUEST_ID = re.compile(r"^[0-9a-f]{16}$")


class RemoteEngine:
    """What the dashboard needs from an engine (snapshot, history, submit), over files."""

    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.history = History(state_dir / "history.db")

    def snapshot(self) -> dict:
        (self.dir / WANTED).touch()
        path = self.dir / SNAPSHOT
        deadline = time.time() + WRITE_EVERY + 2
        while True:
            try:
                age = time.time() - path.stat().st_mtime
                if age < STALE_AFTER:
                    return json.loads(path.read_text())
            except (OSError, ValueError):
                age = None
            if time.time() > deadline:
                why = f"its last update was {age:.0f} s ago" if age is not None else "it hasn't written any state yet"
                raise RuntimeError(f"the node isn't running ({why})")
            time.sleep(0.2)

    def submit(self, action: str, body: dict) -> str:
        if not isinstance(action, str) or not re.fullmatch(r"[a-z_]{1,24}", action):
            raise ValueError("unknown action")
        rid = secrets.token_hex(8)
        inbox, outbox = self.dir / "inbox", self.dir / "outbox"
        inbox.mkdir(parents=True, exist_ok=True)
        tmp = inbox / f"{rid}.tmp"
        tmp.write_text(json.dumps({"action": action, "body": body}))
        tmp.rename(inbox / f"{rid}.json")
        answer = outbox / f"{rid}.json"
        deadline = time.time() + ANSWER_WITHIN
        while time.time() < deadline:
            if answer.exists():
                try:
                    res = json.loads(answer.read_text())
                except ValueError:
                    time.sleep(0.05)
                    continue
                answer.unlink(missing_ok=True)
                if res.get("ok"):
                    return str(res.get("message", "done"))
                raise ValueError(str(res.get("error", "failed")))
            time.sleep(0.1)
        (inbox / f"{rid}.json").unlink(missing_ok=True)
        raise ValueError("the node didn't answer. Is it running?")


def take_requests(state_dir: Path, submit, limit: int = 20) -> int:
    """Engine side: answer the dashboard's queued actions. Returns how many were handled."""
    inbox, outbox = state_dir / "inbox", state_dir / "outbox"
    if not inbox.is_dir():
        return 0
    n = 0
    for f in sorted(inbox.glob("*.json"))[:limit]:
        rid = f.stem
        try:
            if not REQUEST_ID.match(rid):
                raise ValueError("bad request id")
            if f.stat().st_size > MAX_REQUEST:
                raise ValueError("request too large")
            req = json.loads(f.read_text())
            action, body = req.get("action"), req.get("body") or {}
            if not isinstance(action, str) or not isinstance(body, dict):
                raise ValueError("bad request")
            res = {"ok": True, "message": submit(action, body)}
        except ValueError as e:
            res = {"ok": False, "error": str(e)}
        except Exception as e:   # never let one request take the engine down
            res = {"ok": False, "error": f"failed: {e}"}
        f.unlink(missing_ok=True)
        if REQUEST_ID.match(rid):
            outbox.mkdir(parents=True, exist_ok=True)
            tmp = outbox / f"{rid}.tmp"
            tmp.write_text(json.dumps(res))
            tmp.rename(outbox / f"{rid}.json")
        n += 1
    # Answers nobody collected (the dashboard gave up) don't pile up.
    if outbox.is_dir():
        for f in outbox.glob("*.json"):
            try:
                if time.time() - f.stat().st_mtime > 300:
                    f.unlink()
            except OSError:
                pass
    return n


def snapshot_wanted(state_dir: Path) -> bool:
    try:
        return time.time() - (state_dir / WANTED).stat().st_mtime < WANTED_FOR
    except OSError:
        return False


def write_snapshot(state_dir: Path, doc: dict) -> None:
    tmp = state_dir / (SNAPSHOT + ".tmp")
    tmp.write_text(json.dumps(doc))
    tmp.replace(state_dir / SNAPSHOT)


def dashboard_key(state_dir: Path) -> str:
    """The access key for a dashboard reachable from other computers (made once, kept 0600)."""
    path = state_dir / "dashboard.key"
    try:
        key = path.read_text().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    state_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)      # created fresh below, so it's never readable by others, even briefly
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    tmp.replace(path)
    return key
