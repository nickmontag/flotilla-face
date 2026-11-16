"""Decide which files this node should hold.

Pure function of (candidates, budget, node id, tuning): no I/O, fully testable.

The unit of assignment is one weight file (a safetensors/GGUF shard), or a
whole small model. Each unit gets a value *per GB*:

    rate = priority × (scarcity(availability, target) × risk + throughput)
    value = rate × size_GB  (× 1.25 for a whole model: a complete copy is
                             independently useful and shows up as a seeder)

Because value is per GB, model size neither helps nor hurts: a GB of a
priority-0.9 model is worth 0.9 whether the model is 8 GB or 1.5 TB. Editors
steer with priority; the swarm state steers with scarcity.

* priority     - editorial score from the signed feed, 0..1.
* scarcity     - 1/(1+a) where a = peers already holding every piece of that
                 file; ×0.1 once a reaches the model's target replica count.
* risk         - ×2.5 if the Hugging Face original is gone (the swarm is the
                 only copy), ×1.5 if unknown, ×1 if HF still serves it.
* throughput   - small extra term for swarms with many leechers, scaled by
                 how much upload this node has. Low-upload nodes therefore
                 lean toward pure preservation (rare files), high-upload
                 nodes also help busy swarms.

Units are ranked by value per GB, then *weighted rendezvous hashing* turns the
ranking into a per-node draw: key = density^spread × (−1/ln u), u = H(node, file).
Two nodes that see the same swarm state pick *different* shards with
probability proportional to need, with no coordinator. A node's draw is stable
across runs, so replans don't churn.

What a node holds stays put. A plan keeps every held file that still fits, fills
free space, and only then *swaps*: the held unit worth least per GB goes out for
an unheld unit worth more, until `churn_bytes` have been swapped out. The engine
allows that on the user's schedule (daily, weekly, monthly, or only when asked)
and passes 0 in between. Files held for less than min_hold_days are never
swapped out, a held unit counts for hold_bonus times its value (so a swap has to
be clearly better), and models the user chose by hand (`manual`) are never
swapped out and never wait for the schedule to come in.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

from .candidates import FileCand, TorrentCand

GB = 1_000_000_000


@dataclass
class Params:
    budget_bytes: int
    upload_bps: float
    download_bps: float
    max_torrents: int = 16
    spread: float = 3.0
    hold_bonus: float = 1.5
    min_hold_days: float = 7.0
    whole_model_under_bytes: int = 25 * GB
    now: float = 0.0
    min_rate: float = 1e-4
    # repo -> bytes the user set aside for it. Reserved first (its neediest pieces),
    # and never exceeded; everything else shares what's left.
    allot: dict[str, int] = field(default_factory=dict)
    # Reorienting: the most held bytes this plan may swap out for pieces the swarm needs more.
    # None = no limit; 0 = swap nothing (keep what's held, fill free space).
    churn_bytes: int | None = None
    # Repos the user chose by hand (their own picks, models with space set aside). Their pieces
    # are never swapped out, and bringing them in isn't held back by churn_bytes.
    manual: frozenset[str] = frozenset()


@dataclass
class Unit:
    cand: TorrentCand
    files: list[FileCand]
    size: int
    overhead: int
    availability: float
    rate: float
    value: float = 0.0
    density: float = 0.0      # value per GB of disk it takes
    key: float = 0.0
    held: bool = False
    forced: bool = False
    whole: bool = False

    @property
    def label(self) -> str:
        return "(whole model)" if self.whole else self.files[0].repo_path


@dataclass
class TorrentPick:
    cand: TorrentCand
    units: list[Unit] = field(default_factory=list)
    meta_files: list[FileCand] = field(default_factory=list)

    @property
    def files(self) -> list[FileCand]:
        seen, out = set(), []
        for f in self.meta_files + [f for u in self.units for f in u.files]:
            if f.index not in seen:
                seen.add(f.index)
                out.append(f)
        return out

    @property
    def bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def new_bytes(self) -> int:
        return sum(f.size for f in self.files if not f.held)

    @property
    def value(self) -> float:
        return sum(u.value for u in self.units)

    @property
    def whole(self) -> bool:
        return any(u.whole for u in self.units)


@dataclass
class Plan:
    picks: dict[str, TorrentPick]
    budget_bytes: int
    total_bytes: int
    new_bytes: int
    drops: list[tuple[str, FileCand]]
    considered_units: int
    est_fill_seconds: float
    # Reorienting. swap_out are held units this plan drops for the swap_in units, which the swarm
    # needs more. lowest and top are the two ends it chose from: the held units worth least (the
    # user's own models left out), and the unheld units this node would fetch first.
    swap_out: list[Unit] = field(default_factory=list)
    swap_in: list[Unit] = field(default_factory=list)
    lowest: list[Unit] = field(default_factory=list)
    top: list[Unit] = field(default_factory=list)
    # Bytes the smallest swap that was worth making, but bigger than churn_bytes allowed, would
    # have dropped (0: nothing was held back by the limit).
    churn_short: int = 0

    def urgent(self, f: FileCand) -> bool:
        return (f.availability or 0) == 0


def scarcity(a: float, target: int) -> float:
    base = 1.0 / (1.0 + max(0.0, a))
    return base if a < target else base * 0.1


def risk(hf_backed: bool | None) -> float:
    return {True: 1.0, False: 2.5, None: 1.5}[hf_backed]


def throughput(c: TorrentCand, upload_bps: float) -> float:
    demand = min(1.0, c.leechers / (c.seeders + 1))
    return 0.5 * demand * min(1.0, upload_bps / 12_500_000)  # 100 Mbit/s → full weight


def hrw_u(node_id: str, infohash: str, path: str) -> float:
    h = hashlib.sha256(f"{node_id}\x00{infohash}\x00{path}".encode()).digest()
    u = (int.from_bytes(h[:8], "big") + 0.5) / 2**64
    return min(max(u, 1e-15), 1 - 1e-15)


def _edge_overhead(c: TorrentCand, f: FileCand) -> int:
    """Bytes of neighbouring files we must fetch because pieces straddle files."""
    pl, total = c.piece_length, c.meta.num_pieces * c.piece_length
    tf = c.meta.files[f.index]
    head = tf.offset - f.first_piece * pl
    tail = min((f.last_piece + 1) * pl, total) - (tf.offset + tf.size)
    return max(0, head) + max(0, tail)


def _avail(f: FileCand, c: TorrentCand) -> float:
    if f.availability is not None:
        return f.availability
    # No swarm probe: every complete seeder has every file.
    return max(0, c.seeders - (1 if f.held else 0))


WHOLE_BONUS = 1.25


def build_units(cands: list[TorrentCand], p: Params, node_id: str) -> list[Unit]:
    units: list[Unit] = []
    for c in cands:
        live = [f for f in c.files if f.kind != "skip"]
        weights = [f for f in live if f.kind == "weight"]
        if not live:
            continue
        r, t = risk(c.hf_backed), throughput(c, p.upload_bps)
        if c.selectable_bytes <= p.whole_model_under_bytes or not weights:
            a = min(_avail(f, c) for f in live)
            held = all(f.held for f in live)
            rate = c.priority * (scarcity(a, c.target_replicas) * r + t)
            units.append(Unit(c, live, sum(f.size for f in live), 0, a, rate, held=held, whole=True))
            continue
        for f in weights:
            a = _avail(f, c)
            rate = c.priority * (scarcity(a, c.target_replicas) * r + t)
            units.append(Unit(c, [f], f.size, _edge_overhead(c, f), a, rate, held=f.held))

    for u in units:
        u.value = u.rate * (u.size / GB) * (WHOLE_BONUS if u.whole else 1.0)
        if u.held:
            u.value *= p.hold_bonus
            since = [f.held_since for f in u.files if f.held_since is not None]
            if since and (p.now - min(since)) < p.min_hold_days * 86400:
                u.forced = True
        u.density = u.value / max((u.size + u.overhead) / GB, 1e-3)
        uu = hrw_u(node_id, u.cand.infohash, u.files[0].repo_path)
        u.key = (u.density ** p.spread) * (-1.0 / math.log(uu))
    return units


SHOWN = 8     # units kept in Plan.lowest and Plan.top


def plan(cands: list[TorrentCand], p: Params, node_id: str) -> Plan:
    units = build_units(cands, p, node_id)
    order = sorted(units, key=lambda u: (not u.forced, -u.key))
    picks: dict[str, TorrentPick] = {}
    used = 0
    per_repo: dict[str, int] = {}
    taken: set[int] = set()

    def meta_bytes(u: Unit) -> int:
        """The torrent's metadata files, which come along with its first unit."""
        if u.whole or u.cand.infohash in picks:
            return 0
        return sum(f.size for f in u.cand.files if f.kind == "meta")

    def take(u: Unit, cap: int | None = None, limit: bool = True) -> bool:
        nonlocal used
        c = u.cand
        opening = c.infohash not in picks
        extra = u.size + u.overhead + meta_bytes(u)
        if limit:
            if opening and len(picks) >= p.max_torrents:
                return False
            if used + extra > p.budget_bytes:
                return False
            if cap is not None and per_repo.get(c.repo, 0) + extra > cap:
                return False
        pick = picks.setdefault(c.infohash, TorrentPick(c))
        if opening and not u.whole:
            pick.meta_files = [f for f in c.files if f.kind == "meta"]
        pick.units.append(u)
        used += extra
        per_repo[c.repo] = per_repo.get(c.repo, 0) + extra
        taken.add(id(u))
        return True

    def release(u: Unit) -> None:
        nonlocal used
        c = u.cand
        pick = picks[c.infohash]
        pick.units = [x for x in pick.units if x is not u]
        freed = u.size + u.overhead
        if not pick.units:
            freed += sum(f.size for f in pick.meta_files)
            del picks[c.infohash]
        used -= freed
        per_repo[c.repo] -= freed
        taken.discard(id(u))

    def open_to(u: Unit) -> bool:
        return id(u) not in taken and u.rate >= p.min_rate

    # 1. Files held for less than min_hold_days stay, whatever else happens.
    for u in order:
        if u.forced:
            take(u, limit=False)
    # 2. Space you set aside for a model: what's held of it, then its neediest pieces, up to that much.
    for repo in sorted(p.allot, key=lambda r: -max((u.cand.priority for u in units if u.cand.repo == r), default=0)):
        for u in sorted((u for u in order if u.cand.repo == repo), key=lambda u: not u.held):
            if open_to(u):
                take(u, p.allot[repo])
    # 3. What's held stays: your own models first, then the rest, the most valuable per GB first
    #    (so if the space shrank, the least valuable goes).
    for u in sorted((u for u in units if u.held), key=lambda u: (u.cand.repo not in p.manual, -u.density, -u.key)):
        if open_to(u):
            take(u, p.allot.get(u.cand.repo))
    # 4. Free space goes to what the swarm needs most. Models with an allotment never go past it.
    for u in order:
        if open_to(u):
            take(u, p.allot.get(u.cand.repo))

    # 5. Reorient: swap the least valuable of what's held for unheld pieces worth more per GB,
    #    lowest out first, until churn_bytes have gone out.
    victims = sorted((u for u in units if id(u) in taken and u.held and not u.forced and u.cand.repo not in p.manual),
                     key=lambda u: (u.density, u.key))
    lowest = victims[:SHOWN]
    top = [u for u in order if open_to(u) and not u.held][:SHOWN]
    limit = math.inf if p.churn_bytes is None else p.churn_bytes
    swap_out: list[Unit] = []
    swap_in: list[Unit] = []
    gone: set[int] = set()
    churned = short = 0
    for a in order:
        if len(gone) == len(victims):
            break
        if not open_to(a):
            continue
        c = a.cand
        mine = c.repo in p.manual            # your own model: comes in whatever the schedule says
        if not mine and churned >= limit:
            continue
        cap = p.allot.get(c.repo)
        opening = c.infohash not in picks
        need = a.size + a.overhead + meta_bytes(a)
        if cap is not None and per_repo.get(c.repo, 0) + need > cap:
            continue
        deficit = need - (p.budget_bytes - used)
        chosen: list[Unit] = []
        freed = dropped = 0
        if deficit > 0:
            for v in victims:
                if id(v) in gone:
                    continue
                if v.density >= a.density:      # nothing left that's worth less than this
                    break
                chosen.append(v)
                freed += v.size + v.overhead
                dropped += v.size
                if freed >= deficit:
                    break
            if freed < deficit:
                continue
            if not mine and churned + dropped > limit:
                short = min(short or dropped, dropped)
                continue
        if opening:
            leaving: dict[str, int] = {}
            for v in chosen:
                leaving[v.cand.infohash] = leaving.get(v.cand.infohash, 0) + 1
            closing = sum(1 for ih, n in leaving.items() if n == len(picks[ih].units))
            if len(picks) - closing >= p.max_torrents:
                continue
        for v in chosen:
            release(v)
        if not take(a, cap):                    # shouldn't happen; never drop for nothing
            for v in chosen:
                take(v, limit=False)
            continue
        gone.update(id(v) for v in chosen)
        swap_out += chosen
        swap_in.append(a)
        if not mine:
            churned += dropped

    selected = {(ih, f.index) for ih, pk in picks.items() for f in pk.files}
    drops = [
        (c.infohash, f) for c in cands for f in c.files
        if f.held and (c.infohash, f.index) not in selected
    ]
    total = sum(pk.bytes for pk in picks.values())
    new = sum(pk.new_bytes for pk in picks.values())
    return Plan(picks, p.budget_bytes, total, new, drops, len(units),
                new / p.download_bps if p.download_bps > 0 else float("inf"),
                swap_out=swap_out, swap_in=swap_in, lowest=lowest, top=top, churn_short=short)
