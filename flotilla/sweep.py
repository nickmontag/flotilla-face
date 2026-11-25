"""The catalog sweep: once the list is full, spare space goes to Pirate Face's least-seeded models.

When every model on the signed list (spare entries included) has its target number of copies, more
space would only add a 20th or 40th copy of the same models. Pirate Face's whole catalog holds tens of
thousands of models with no seeder at all. Pirate Face has no API that ranks them, and listing them all
is a lookup per model, so the sweep never enumerates the catalog:

1. **The catalog is already here.** Every node keeps Pirate Face's magnet index (magnetindex.py), every
   model it lists, as a torrent it seeds. Reading it costs no request.
2. **Each node takes its own slice.** It ranks the index by a hash of its node id, the week and each
   infohash, and takes the first SLICE (weighted rendezvous hashing, as the planner does with files).
   Nodes get different slices with no coordinator, and the week in the key moves every slice weekly,
   so a fleet of a few dozen nodes covers the whole catalog every week.
3. **The tracker counts the seeders.** One BitTorrent scrape asks Pirate Face's tracker for BATCH
   infohashes at once. A model the tracker doesn't know has had no peer announce it: no seeder.
4. **Only the least seeded go further.** At most `sweep_candidates` of them are looked up on Pirate
   Face and checked like any other model (candidates.build_candidate), at priority PRIORITY with a
   target of TARGET, below everything on the list.

A slice is scraped at most once a day (EVERY); the picks are kept in state.json in between.

The scrape is plain HTTP, as BitTorrent's trackers are, through the same network pin as everything
else. Its counts only choose which catalog models to look at: everything about a model that's fetched
is checked as before, so a forged count can at worst steer the sweep to other models.
"""

from __future__ import annotations

import hashlib
import heapq
import http.client
import urllib.parse

from . import USER_AGENT, netsafe
from .feed import MIN_COPIES
from .magnet import DEFAULT_TRACKER_HOSTS
from .torrentmeta import TorrentError, bdecode

SCRAPE_HOST, SCRAPE_PORT = DEFAULT_TRACKER_HOSTS[0], 6969
BATCH = 74             # infohashes per scrape (the tracker refuses a request for 200)
SLICE = 10 * BATCH     # catalog models a node scrapes a day
PRIORITY = 0.1         # below the whole list, Pirate Face's unranked new torrents (0.15) included
TARGET = MIN_COPIES    # unscored: the floor of feed.target_for_risk
EVERY = 86400.0
WEEK = 7 * 86400
MAX_REPLY = 1 << 20
DEADLINE = 120.0       # seconds for a whole scrape request: a tracker that trickles can't hold the refresh
MAX_COUNT = 1 << 31    # a peer count past this is nonsense: clamped


def week_of(now: float) -> int:
    return int(now // WEEK)


def slice_of(entries: dict, node_id: str, week: int, n: int = SLICE, skip=frozenset()) -> list[tuple[str, object]]:
    """This node's n models of the index this week: (repo, Entry), in the node's own order."""
    def key(item):
        return hashlib.sha256(f"{node_id}\x00sweep\x00{week}\x00{item[1].infohash}".encode()).digest()

    return heapq.nsmallest(n, ((r, e) for r, e in entries.items() if r.lower() not in skip), key=key)


def _get(path: str, timeout: float = 20.0, deadline: float = DEADLINE, host: str = SCRAPE_HOST,
         port: int = SCRAPE_PORT) -> bytes:
    """GET http://<tracker>:6969<path>, through the network pin, at most MAX_REPLY bytes, all of it
    within `deadline` seconds (`timeout` bounds each wait for bytes). Raises OSError only."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    if netsafe.pinned_to():
        conn._create_connection = netsafe.pinned_connection
    with netsafe.Deadline(deadline) as dl:
        dl.watch(conn)
        try:
            conn.request("GET", path, headers={"User-Agent": USER_AGENT})
            resp = conn.getresponse()
            if resp.status != 200:
                raise OSError(f"the tracker answered HTTP {resp.status} to a scrape")
            chunks, total = [], 0
            while total <= MAX_REPLY:
                chunk = resp.read(min(65536, MAX_REPLY + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if dl.passed():
                    break
        except (OSError, http.client.HTTPException) as e:   # BadStatusLine, IncompleteRead, LineTooLong...
            if dl.expired:
                raise OSError(f"the tracker's scrape answer took longer than {deadline:.0f}s") from e
            raise OSError(f"the tracker's scrape answer is broken: {e}") from e
        finally:
            conn.close()
        if dl.passed():
            raise OSError(f"the tracker's scrape answer took longer than {deadline:.0f}s")
    if total > MAX_REPLY:
        raise OSError("the tracker's scrape answer is too large")
    declared = resp.getheader("Content-Length") or ""
    if declared.isdigit() and int(declared) != total:       # read(n) ends quietly at an early close
        raise OSError(f"the tracker's scrape answer was cut off ({total} of {declared} bytes)")
    return b"".join(chunks)


def _count(v) -> int | None:
    """A peer count from a scrape: a non-negative int (not a bool), clamped; None if it isn't one."""
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return None
    return min(v, MAX_COUNT)


def scrape(infohashes: list[str], get=_get) -> dict[str, tuple[int, int]]:
    """{infohash: (seeders, leechers)} from the tracker, BATCH at a time. A hash the tracker doesn't
    know (or gives no usable counts for) is (0, 0): no peer has announced it. Raises OSError only,
    whatever the reply."""
    out: dict[str, tuple[int, int]] = {}
    for i in range(0, len(infohashes), BATCH):
        batch = infohashes[i:i + BATCH]
        q = "&".join("info_hash=" + urllib.parse.quote(bytes.fromhex(h)) for h in batch)
        try:
            doc = bdecode(get("/scrape?" + q))
        except (TorrentError, ValueError, RecursionError) as e:
            raise OSError(f"the tracker's scrape answer isn't bencode: {e}") from e
        except http.client.HTTPException as e:      # a `get` that lets one through
            raise OSError(f"the tracker's scrape answer is broken: {e}") from e
        files = doc.get(b"files") if isinstance(doc, dict) else None
        if not isinstance(files, dict):
            raise OSError("the tracker's scrape answer has no files")
        for h in batch:
            st = files.get(bytes.fromhex(h))
            seeders = _count(st.get(b"complete")) if isinstance(st, dict) else None
            leechers = _count(st.get(b"incomplete", 0)) if isinstance(st, dict) else None
            # Both are kept in state.json: anything but a count (bytes, a list) would stop it saving.
            out[h] = (seeders, leechers or 0) if seeders is not None else (0, 0)
    return out


def pick(entries: dict, node_id: str, now: float, k: int, skip=frozenset(), scrape_fn=scrape,
         n: int = SLICE) -> list[dict]:
    """The k least-seeded models of this node's slice: [{repo, seeders, leechers}], fewest seeders
    first, then the node's own order. `skip`: lower-cased repos not to pick (on the list, say)."""
    chosen = slice_of(entries, node_id, week_of(now), n, skip)
    counts = scrape_fn([e.infohash for _, e in chosen])
    ranked = sorted(range(len(chosen)), key=lambda i: (counts[chosen[i][1].infohash][0], i))
    return [{"repo": chosen[i][0], "seeders": counts[chosen[i][1].infohash][0],
             "leechers": counts[chosen[i][1].infohash][1]} for i in ranked[:max(0, k)]]
