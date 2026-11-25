"""What a refresh is doing right now, for the dashboard and `flotilla status`.

A refresh takes minutes and, on a new node, nothing is downloaded until it ends: it checks the
signed list and the hash ledger, keeps Pirate Face's magnet index, looks up every model, watches
the swarms (tuning.probe_seconds), then plans. Without this the dashboard said "Idle" over an empty
list for those minutes. The refresh thread writes; the dashboard and status threads read view().
"""

from __future__ import annotations

import collections
import threading
import time

STEPS = ("list", "index", "lookup", "probe", "plan")
PLAN_GUESS = 15.0      # seconds from the end of the probe until the plan is applied, about


def skip_kind(reason: str) -> str:
    """A rejection's reason, as one of a few short kinds the dashboard counts."""
    r = reason.lower()
    if ".torrent download failed" in r:
        return "no .torrent on Pirate Face" if "http 404" in r else "couldn't fetch its .torrent"
    if "no magnet listed" in r:
        return "not on Pirate Face yet"
    if "license" in r:
        return "license not allowed" if "not in policy.licenses" in r else "license unknown"
    if "deny list" in r:
        return "on the deny list"
    if "differs" in r or "does not match" in r or "doesn't match" in r or "mismatch" in r:
        return "doesn't match its hashes"
    if "lookup failed" in r or "couldn't be asked" in r or "can't be asked" in r:
        return "lookup failed"
    return "other"


class RefreshProgress:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._cur: dict | None = None
        self._kinds: collections.Counter = collections.Counter()

    def begin(self, first: bool, probe_seconds: float) -> None:
        """A refresh starts. first: the node holds nothing yet, so nothing moves until it ends."""
        with self._lock:
            self._cur = {"first": bool(first), "started": self._clock(), "probe_seconds": float(probe_seconds),
                         "step": None, "steps": {}}
            self._kinds = collections.Counter()

    def step(self, name: str, **info) -> None:
        """The refresh moves on to step `name` (one of STEPS); `info` is shown with it. A step
        outside a refresh (a replan the dashboard asked for) starts one that isn't a first run."""
        now = self._clock()
        with self._lock:
            if self._cur is None:
                self._cur = {"first": False, "started": now, "probe_seconds": 0.0, "step": None, "steps": {}}
            cur = self._cur
            prev = cur["steps"].get(cur["step"])
            if prev is not None and prev.get("took") is None:
                prev["took"] = now - prev["started"]
            cur["step"] = name
            cur["steps"][name] = {"started": now, "took": None, **info}

    def note(self, name: str, **info) -> None:
        """Add to what step `name` shows (once it has a result: the list's version, say)."""
        with self._lock:
            if self._cur is not None and name in self._cur["steps"]:
                self._cur["steps"][name].update(info)

    def looked_up(self, done: int, total: int, result=None) -> None:
        """One more model looked up (gather's progress callback). result: a TorrentCand, a
        Rejection (it has a `reason`), or None before the first."""
        with self._lock:
            if self._cur is None or "lookup" not in self._cur["steps"]:
                return
            s = self._cur["steps"]["lookup"]
            s["done"], s["total"] = done, total
            if result is not None:
                if hasattr(result, "reason"):
                    s["skipped"] = s.get("skipped", 0) + 1
                    self._kinds[skip_kind(result.reason)] += 1
                else:
                    s["usable"] = s.get("usable", 0) + 1

    @property
    def running(self) -> bool:
        return self._cur is not None

    def end(self) -> None:
        with self._lock:
            self._cur = None

    def view(self) -> dict | None:
        """None when no refresh is running. Times are seconds, relative to now, so a dashboard on
        another machine needn't share this one's clock."""
        now = self._clock()
        with self._lock:
            if self._cur is None:
                return None
            cur = self._cur
            steps = {}
            for name, s in cur["steps"].items():
                v = {k: val for k, val in s.items() if k not in ("started", "ends_at")}
                v["took"] = s["took"] if s["took"] is not None else now - s["started"]
                v["done_step"] = s["took"] is not None
                if "ends_at" in s:
                    v["left"] = max(0.0, s["ends_at"] - now)
                steps[name] = v
            if "lookup" in steps:
                steps["lookup"]["kinds"] = dict(self._kinds.most_common())
            return {"first": cur["first"], "step": cur["step"], "index": STEPS.index(cur["step"]) + 1
                    if cur["step"] in STEPS else 0, "of": len(STEPS), "elapsed": now - cur["started"],
                    "probe_seconds": cur["probe_seconds"], "eta": self._eta(cur, now), "steps": steps}

    @staticmethod
    def _eta(cur: dict, now: float) -> float | None:
        """Seconds until the plan is applied, about: None until the lookups give a rate."""
        step, s = cur["step"], cur["steps"].get(cur["step"], {})
        if step == "plan":
            return max(0.0, PLAN_GUESS - (now - s["started"]))
        if step == "probe":
            return max(0.0, s.get("ends_at", now) - now) + PLAN_GUESS
        if step == "lookup" and s.get("done") and s.get("total"):
            rate = (now - s["started"]) / s["done"]
            return rate * (s["total"] - s["done"]) + cur["probe_seconds"] + PLAN_GUESS
        return None
