"""Upload and download history, kept across restarts for as long as the node exists.

Every minute the engine adds up the bytes each torrent moved (from libtorrent's own counters,
not a snapshot of the current speed, so nothing between samples is lost). They go into a small
SQLite file in the state folder:

* per minute, kept for 3 days (for the last hour and day);
* per hour, kept for good (about 9,000 rows a year per model).

The node's own row ("*") also counts the seconds it was online and seeding, so a gap
while the computer slept shows as a gap, not as zero traffic. Rates are bytes per bucket
divided by its length: averages, so long ranges come out smooth.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

MINUTE, HOUR, DAY = 60, 3600, 86400
KEEP_MINUTES = 3 * DAY
NODE = "*"

# range name -> (seconds, bucket seconds); None means "since the first record"
RANGES = {
    "1h": (HOUR, MINUTE),
    "24h": (DAY, 10 * MINUTE),
    "7d": (7 * DAY, HOUR),
    "30d": (30 * DAY, 6 * HOUR),
    "90d": (90 * DAY, DAY),
    "1y": (365 * DAY, DAY),
    "all": (None, DAY),
}


class History:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        with self.lock, self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("""CREATE TABLE IF NOT EXISTS traffic (
                res INTEGER NOT NULL, t INTEGER NOT NULL, ih TEXT NOT NULL,
                up INTEGER NOT NULL DEFAULT 0, down INTEGER NOT NULL DEFAULT 0,
                online INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (res, ih, t)) WITHOUT ROWID""")
            self.db.execute("CREATE TABLE IF NOT EXISTS names (ih TEXT PRIMARY KEY, repo TEXT NOT NULL)")
        self._pruned = 0.0

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # ------------------------------------------------------------------ writing

    def record(self, now: float, moved: dict[str, tuple[int, int]], online: float,
               names: dict[str, str] | None = None) -> None:
        """Add bytes moved since the last call ({infohash: (up, down)}) and seconds online."""
        rows = [(NODE, sum(u for u, _ in moved.values()), sum(d for _, d in moved.values()), int(round(online)))]
        rows += [(ih, int(u), int(d), 0) for ih, (u, d) in moved.items() if u or d]
        with self.lock, self.db:
            for res in (MINUTE, HOUR):
                t = int(now // res * res)
                self.db.executemany(
                    """INSERT INTO traffic (res, t, ih, up, down, online) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT (res, ih, t) DO UPDATE SET up = up + excluded.up,
                       down = down + excluded.down, online = online + excluded.online""",
                    [(res, t, ih, u, d, o) for ih, u, d, o in rows])
            if names:
                self.db.executemany("INSERT INTO names (ih, repo) VALUES (?, ?) "
                                    "ON CONFLICT (ih) DO UPDATE SET repo = excluded.repo", list(names.items()))
            if now - self._pruned > HOUR:
                self.db.execute("DELETE FROM traffic WHERE res = ? AND t < ?", (MINUTE, int(now - KEEP_MINUTES)))
                self._pruned = now

    # ------------------------------------------------------------------ reading

    def first(self) -> int | None:
        with self.lock:
            row = self.db.execute("SELECT MIN(t) FROM traffic WHERE res = ? AND ih = ?", (HOUR, NODE)).fetchone()
        return row[0]

    def repos(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT DISTINCT repo FROM names ORDER BY repo")]

    def query(self, range_: str = "24h", repo: str | None = None, tz_offset: int = 0,
              now: float | None = None) -> dict:
        """Traffic over a range, in buckets aligned to the viewer's local time.

        tz_offset is seconds east of UTC (the browser's, so "a day" is a local day).
        Returns {"range", "bucket", "start", "end", "points": [[t, up, down, online], ...],
        "up", "down", "online", "models": [{"repo", "up", "down"}]}. points cover the
        whole range: a bucket with online 0 is time the node wasn't seeding.
        """
        now = time.time() if now is None else now
        if range_ not in RANGES:
            raise ValueError(f"range must be one of {', '.join(RANGES)}")
        span, bucket = RANGES[range_]
        first = self.first() if span is None else None
        if span is None:
            span = max(DAY, now - first) if first else DAY
            if span > 2 * 365 * DAY:
                bucket = 7 * DAY
        res = MINUTE if bucket < HOUR and span <= KEEP_MINUTES else HOUR
        align = tz_offset if bucket >= DAY else 0

        def floor(t):
            return int((t + align) // bucket * bucket - align)

        end = floor(now) + bucket                       # the current bucket is the last one
        start = floor(first) if first else end - -(-span // bucket) * bucket
        repo_clause, args = "", []
        if repo:
            repo_clause = "ih IN (SELECT ih FROM names WHERE repo = ?)"
            args = [repo]
        else:
            repo_clause = "ih = ?"
            args = [NODE]
        sql = f"""SELECT ((t + ?) / ?) * ? - ? AS b,
                         SUM(CASE WHEN {repo_clause} THEN up ELSE 0 END),
                         SUM(CASE WHEN {repo_clause} THEN down ELSE 0 END),
                         SUM(CASE WHEN ih = ? THEN online ELSE 0 END)
                  FROM traffic WHERE res = ? AND t >= ? AND t < ? AND (ih = ? OR {repo_clause})
                  GROUP BY b"""
        params = [align, bucket, bucket, align, *args, *args, NODE, res, start, end, NODE, *args]
        with self.lock:
            got = {int(b): (u, d, o) for b, u, d, o in self.db.execute(sql, params)}
            models = [{"repo": r, "up": u, "down": d} for r, u, d in self.db.execute(
                """SELECT n.repo, SUM(x.up), SUM(x.down) FROM traffic x JOIN names n ON n.ih = x.ih
                   WHERE x.res = ? AND x.t >= ? AND x.t < ? GROUP BY n.repo
                   HAVING SUM(x.up) + SUM(x.down) > 0 ORDER BY SUM(x.up) DESC, SUM(x.down) DESC""",
                (res, start, end))]
        points = []
        for b in range(start, end, bucket):
            u, d, o = got.get(b, (0, 0, 0))
            points.append([b, int(u), int(d), int(o)])
        return {
            "range": range_, "bucket": bucket, "start": start, "end": end, "repo": repo or None,
            "points": points, "up": sum(p[1] for p in points), "down": sum(p[2] for p in points),
            "online": sum(p[3] for p in points), "models": models,
        }
