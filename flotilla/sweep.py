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


def week_of(now: float) -> int:
    return int(now // WEEK)


def slice_of(entries: dict, node_id: str, week: int, n: int = SLICE, skip=frozenset()) -> list[tuple[str, object]]:
    """This node's n models of the index this week: (repo, Entry), in the node's own order."""
    def key(item):
        return hashlib.sha256(f"{node_id}\x00sweep\x00{week}\x00{item[1].infohash}".encode()).digest()

    return heapq.nsmallest(n, ((r, e) for r, e in entries.items() if r.lower() not in skip), key=key)


def _get(path: str, timeout: float = 20.0) -> bytes:
    """GET http://<tracker>:6969<path>, through the network pin, at most MAX_REPLY bytes."""
    conn = http.client.HTTPConnection(SCRAPE_HOST, SCRAPE_PORT, timeout=timeout)
    if netsafe.pinned_to():
        conn._create_connection = netsafe.pinned_connection
    try:
        conn.request("GET", path, headers={"User-Agent": USER_AGENT})
        resp = conn.getresponse()
        body = resp.read(MAX_REPLY + 1)
        if resp.status != 200:
            raise OSError(f"the tracker answered HTTP {resp.status} to a scrape")
        if len(body) > MAX_REPLY:
            raise OSError("the tracker's scrape answer is too large")
        return body
    finally:
        conn.close()


def scrape(infohashes: list[str], get=_get) -> dict[str, tuple[int, int]]:
    """{infohash: (seeders, leechers)} from the tracker, BATCH at a time. A hash the tracker doesn't
    know is (0, 0): no peer has announced it."""
    out: dict[str, tuple[int, int]] = {}
    for i in range(0, len(infohashes), BATCH):
        batch = infohashes[i:i + BATCH]
        q = "&".join("info_hash=" + urllib.parse.quote(bytes.fromhex(h)) for h in batch)
        try:
            doc = bdecode(get("/scrape?" + q))
        except TorrentError as e:
            raise OSError(f"the tracker's scrape answer isn't bencode: {e}") from e
        files = doc.get(b"files") if isinstance(doc, dict) else None
        if not isinstance(files, dict):
            raise OSError("the tracker's scrape answer has no files")
        for h in batch:
            st = files.get(bytes.fromhex(h))
            ok = isinstance(st, dict) and isinstance(st.get(b"complete"), int)
            out[h] = (st[b"complete"], st.get(b"incomplete") or 0) if ok else (0, 0)
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
