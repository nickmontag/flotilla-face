"""The catalog list: popular models Pirate Face holds, for spare space once the rest of the list is covered.

An editor tool, like fallback.py and worthrescuing.py. A big fleet covers the hand-picked models, the
official releases and the fallback list long before its disks are full; after that, every extra TB
would go to a 20th or 40th copy of the same few dozen models. A copy of a model nobody else holds
protects far more, so this widens the list instead:

1. reads Pirate Face's magnet index (every model Pirate Face lists, the copy each node keeps and
   seeds: magnetindex.py), so membership costs no Pirate Face lookups;
2. reads Hugging Face's most downloaded language and vision-language models, and keeps those the
   index lists. Refusal-removed builds are left to the fallback list;
3. groups each model with its quantisations and keeps one entry per base model (PER_BASE), preferring
   the full-precision release;
4. checks each pick like the fallback list does (fallback.vet): a license Flotilla seeds by default,
   a published .torrent, and Hugging Face still serving the commit Pirate Face pinned;
5. writes at most MAX_ENTRIES, marked "via": "pirateface/catalog", and removes its own entries that
   dropped out. Entries on the list for another reason are left alone.

Priorities sit between Pirate Face's unranked new torrents (0.15) and everything else on the list:
0.16 for 1k downloads a month, up to 0.19 at 1M. Targets are 2, so the fleet reaches across many
models before deepening any. Models Hugging Face no longer has can't be ranked by downloads; those
come in by hand or through Pirate Face's rescue list.
"""

from __future__ import annotations

from pathlib import Path

from . import fallback
from .fallback import Group, Hit, hit_from, popularity, vet
from .feed import target_for_risk

VIA = "pirateface/catalog"
MAX_ENTRIES = 100
PER_BASE = 1
CYBER = 40
TAKEDOWN_RISK = 20
DEFAULT_CAPABILITY = 60
TARGET_REPLICAS = target_for_risk(TAKEDOWN_RISK)
PRIORITY_MIN, PRIORITY_MAX = 0.16, 0.19     # by downloads: 1k or fewer a month -> min, 1M or more -> max
PIPELINES = ("text-generation", "image-text-to-text")
LIMIT = 1000                                 # Hugging Face results read per pipeline


def read_index(shards: Path) -> set[str]:
    """Lower-cased repos Pirate Face's magnet index lists, from a build's shards folder."""
    from .magnetindex import parse_shards

    found = parse_shards(shards)
    if found is None:
        raise ValueError(f"{shards}: a removals list can't be read, so this copy can't be trusted")
    if not found:
        raise ValueError(f"{shards}: no magnet index shards there")
    return {r.lower() for r in found}


def gather_hits(hf, listed: set[str], pipelines=PIPELINES, limit: int = LIMIT, log=print) -> dict[str, Hit]:
    """Hugging Face's most downloaded models that Pirate Face lists, refusal-removed builds left out."""
    hits: dict[str, Hit] = {}
    for tag in pipelines:
        found = fallback.patient(hf.popular, tag, limit)
        kept = 0
        for doc in found:
            h = hit_from(doc)
            if h and h.repo.lower() in listed and not fallback.REFUSAL_RE.search(h.repo) and h.repo not in hits:
                hits[h.repo] = h
                kept += 1
        log(f"huggingface: {len(found)} most downloaded {tag} models, {kept} of them on Pirate Face")
    return hits


def group_hits(hits: dict[str, Hit]) -> list[Group]:
    """Each model with its quantisations (by Hugging Face's links, whoever made them), most downloaded
    first. A group's base is the model it descends from, if that isn't in the group itself: one entry
    per base keeps a popular model's fine-tunes from taking every place."""
    groups = fallback.group_hits(hits, derived=lambda parent: True)
    for g in groups:
        g.base = root_of(g, hits)
    return groups


def root_of(g: Group, hits: dict[str, Hit]) -> str | None:
    """The nearest ancestor Hugging Face's links name outside the group, or None. The nearest, not the
    furthest: two models built on one vision encoder or base checkpoint are still different models."""
    members = {m.repo for m in g.members}
    for h in g.preference():
        seen = set()
        while h.parent and h.parent not in seen:
            seen.add(h.parent)
            if h.parent not in members:
                return h.parent
            if h.parent not in hits:
                break
            h = hits[h.parent]
    return None


def _priority(boost: float, scores: dict, weights=None) -> float:
    return fallback._priority({"scores": scores, "boost": boost}, weights)


def _entry_for(h: Hit, g: Group, pf, hf, licenses: set[str], caps: dict[str, float], weights=None):
    got = vet(h, g, pf, hf, licenses)
    if isinstance(got, str):
        return None, got
    lic, relabelled, rev, tree = got
    origin = {"method": "unknown"}           # Hugging Face's links don't say whether training was involved
    if g.base:
        origin["base"] = g.base
    if h.quantized:
        full = next((m.repo for m in g.preference() if not m.quantized), None)
        src = h.parent if h.parent in {m.repo for m in g.members} else full
        if src and src != h.repo:
            origin["repack_of"] = src
    cap = caps.get((g.base or h.repo).lower(), DEFAULT_CAPABILITY)
    scores = {"capability": int(round(cap)), "cyber": CYBER, "takedown_risk": TAKEDOWN_RISK}
    want = PRIORITY_MIN + (PRIORITY_MAX - PRIORITY_MIN) * popularity(g.downloads)
    unboosted = _priority(1.0, scores, weights)
    boost = round(want / unboosted, 3) if unboosted > 0 else 0.0
    gb = sum(f.size for f in tree.values()) / 1e9 if tree else None
    size = f", {gb:.1f} GB" if gb else ""
    copies = f" across {len(g.members)} copies" if len(g.members) > 1 else ""
    entry = {
        "repo": h.repo, "revision": rev, "scores": scores, "target_replicas": TARGET_REPLICAS, "boost": boost,
        "note": (f"Popular model Pirate Face holds{size}. Picked automatically for spare space once the rest of "
                 f"the list is covered: {fallback._count(g.downloads)} Hugging Face downloads last month{copies}. "
                 f"{lic}."),
        "origin": origin, "via": VIA, "spare": True,
    }
    if relabelled:
        entry["license"] = lic
    return entry, ""


def sync(doc: dict, groups: list[Group], pf, hf, licenses: set[str], deny: set[str] = frozenset(),
         max_entries: int = MAX_ENTRIES, per_base: int = PER_BASE) -> dict:
    """Bring the list's catalog entries in line with the ranked groups. Changes doc in place; returns a
    summary like fallback.sync."""
    weights = doc.get("weights")

    def entry_for(h, g, pf_, hf_, lic, caps):
        return _entry_for(h, g, pf_, hf_, lic, caps, weights)

    return fallback.sync(doc, groups, pf, hf, licenses, deny, max_entries=max_entries, per_base=per_base,
                         via=VIA, entry_for=entry_for, kind="catalog")
