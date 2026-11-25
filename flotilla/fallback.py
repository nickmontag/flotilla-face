"""The fallback list: popular refusal-removed models from Hugging Face, for when the list is seeded.

An editor tool, like worthrescuing.py. Once the hand-picked models have a few copies each, a node
should spend spare space on the models most at risk of takedown that people actually use. Those
are the refusal-removed builds (abliterated, heretic, obliterated, uncensored) of strong models.
Hugging Face has thousands, mostly re-quantisations of a few weights, so this:

1. searches Hugging Face for them, most downloaded first;
2. groups each build with its quantisations (by Hugging Face's "quantized" links, and by name
   within one publisher), and ranks the groups by their combined downloads;
3. keeps at most PER_BASE groups per base model, so one popular base doesn't take every place;
4. from each group picks one copy Pirate Face has a torrent for, preferring the full-precision
   release (anyone can re-quantise from it) over its quantisations;
5. pins it to Pirate Face's commit, checks its license and that Hugging Face still has that
   commit, and writes it into the list with "via": "huggingface/refusal-removed".

Priorities sit below the hand-picked models and around the official releases: an entry's
capability comes from its base model's score on the list, and the boost grows with downloads.
The planner divides a model's value by 1 + its copies, so these fill in once the hand-picked
models have about two copies each, and before Pirate Face's unranked new torrents.

Nodes never search Hugging Face themselves. An editor runs `flotilla editor sync-fallback`,
reviews the changes, then signs the list as usual, so every node sees the same pinned set and
the planner spreads copies across the fleet.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

from .feed import REPO_RE, target_for_risk
from .netsafe import FetchError
from .sources import license_from_card
from .worthrescuing import RELICENSABLE

VIA = "huggingface/refusal-removed"
SEARCH_TERMS = ("abliterated", "uncensored", "heretic", "obliterated")
SEARCH_LIMIT = 200
MAX_ENTRIES = 40
PER_BASE = 2               # fallback entries per base model
TRIES_PER_GROUP = 4        # copies of one group checked against Pirate Face before giving up on it
MAX_PARENT_LOOKUPS = 150   # extra Hugging Face calls to find the full-precision release of a quantisation
CYBER = 40
# General-purpose refusal-removed builds: the lowest tier of takedown risk (README, "How models are
# chosen"). Hugging Face has removed builds named for offensive cyber use; popular general ones stay up.
# A build that doesn't say whether training was involved scores a little lower.
TAKEDOWN_RISK = {"refusal-removal": 40, "unknown": 35}
DEFAULT_CAPABILITY = 60    # base model not scored on the list
# By downloads: 1k or fewer a month -> min, 1M or more -> max. Puts them at a priority of about 0.17-0.30.
BOOST_MIN, BOOST_MAX = 0.38, 0.51

# Language and vision-language models only: image, video and audio generators and LoRA adapters
# turn up in the same searches ("uncensored") but aren't what the list is for.
TEXT_PIPELINES = {"text-generation", "text2text-generation", "image-text-to-text", "any-to-any",
                  "video-text-to-text", "audio-text-to-text", "visual-question-answering"}
NOT_TEXT_TAGS = {"diffusers", "lora", "text-to-image", "image-to-image", "text-to-video", "image-to-video",
                 "text-to-audio", "text-to-speech"}
REFUSAL_RE = re.compile(r"abliterat|uncensor|heretic|obliterat|derestrict|refusal", re.I)
WEIGHT_EDIT_RE = re.compile(r"abliterat|heretic|obliterat", re.I)   # weight edits, no training
_QUANT = (r"(?:i1-)?gguf|awq|gptq|exl[23]|mlx|fp8|nvfp4|mxfp4|int[48]|w4a16|w8a8|bnb|[248]bit|"
          r"[0-9.]+bpw|i?q[0-9](?:_[a-z0-9]+)*")
QUANT_RE = re.compile(rf"(?:^|[-_.])(?:{_QUANT})(?=$|[-_.])", re.I)


PATIENCE = 6                # tries when Hugging Face or Pirate Face says 429 (too many requests)


def patient(call, *args, tries: int = PATIENCE, sleep=time.sleep):
    """call(*args), waiting out 429s (Retry-After, else a minute more each time). Editor tools make
    hundreds of lookups in a row; one 429 shouldn't end the run."""
    for i in range(tries):
        try:
            return call(*args)
        except FetchError as e:
            if e.status != 429 or i == tries - 1:
                raise
            sleep(min(600.0, e.retry_after if e.retry_after is not None else 60.0 * (i + 1)))
    return None


@dataclass
class Hit:
    repo: str
    downloads: int = 0
    likes: int = 0
    license: str | None = None
    parent: str | None = None
    relation: str | None = None
    quantized: bool = False

    @property
    def author(self) -> str:
        return self.repo.split("/")[0].lower()

    @property
    def stem(self) -> str:
        """The name without quantisation tags: a build and its quantisations by one publisher share it."""
        return QUANT_RE.sub("", self.repo.split("/")[1]).strip("-_.").lower()


def is_language_model(doc: dict) -> bool:
    pipeline = doc.get("pipeline_tag")
    tags = {t.lower() for t in doc.get("tags") or [] if isinstance(t, str)}
    bm = doc.get("baseModels") if isinstance(doc.get("baseModels"), dict) else {}
    if bm.get("relation") == "adapter" or tags & NOT_TEXT_TAGS:
        return False
    return pipeline is None or pipeline in TEXT_PIPELINES


def hit_from(doc: dict) -> Hit | None:
    repo = doc.get("id")
    if not isinstance(repo, str) or not REPO_RE.match(repo) or not is_language_model(doc):
        return None
    bm = doc.get("baseModels") or {}
    parents = [m.get("id") for m in bm.get("models", []) if isinstance(m, dict)] if isinstance(bm, dict) else []
    parent = parents[0] if parents and isinstance(parents[0], str) and REPO_RE.match(parents[0]) else None
    relation = bm.get("relation") if isinstance(bm, dict) and isinstance(bm.get("relation"), str) else None
    has_st, has_gguf = bool(doc.get("safetensors")), bool(doc.get("gguf"))
    quantized = (relation == "quantized" and parent is not None) or bool(QUANT_RE.search(repo.split("/")[1])) \
        or (has_gguf and not has_st)
    return Hit(repo, int(doc.get("downloads") or 0), int(doc.get("likes") or 0), license_from_card(doc),
               parent, relation, quantized)


@dataclass
class Group:
    members: list[Hit] = field(default_factory=list)
    base: str | None = None

    @property
    def downloads(self) -> int:
        return sum(h.downloads for h in self.members)

    def preference(self) -> list[Hit]:
        """Full-precision releases first, then quantisations; most downloaded first within each."""
        return sorted(self.members, key=lambda h: (h.quantized, -h.downloads, h.repo))


def popularity(downloads: int) -> float:
    """0 at 1k downloads a month or fewer, 1 at 1M or more, logarithmic between."""
    return max(0.0, min(1.0, (math.log10(downloads + 1) - 3) / 3))


def gather_hits(hf, terms=SEARCH_TERMS, limit=SEARCH_LIMIT, log=print) -> dict[str, Hit]:
    """Search Hugging Face, then look up the full-precision release behind popular quantisations."""
    hits: dict[str, Hit] = {}
    for term in terms:
        found = patient(hf.search, term, limit)
        log(f"huggingface: {len(found)} models matching {term!r}")
        for doc in found:
            h = hit_from(doc)
            if h and REFUSAL_RE.search(h.repo) and h.repo not in hits:
                hits[h.repo] = h
    wanted = sorted({h.parent for h in hits.values()
                     if h.quantized and h.parent and h.parent not in hits and REFUSAL_RE.search(h.parent)},
                    key=lambda p: -sum(h.downloads for h in hits.values() if h.parent == p))
    for parent in wanted[:MAX_PARENT_LOOKUPS]:
        doc = patient(hf.model_info, parent)
        h = hit_from(doc) if doc else None
        if h:
            hits[h.repo] = h
    return hits


def _refusal_removed(repo: str) -> bool:
    return bool(REFUSAL_RE.search(repo))


def group_hits(hits: dict[str, Hit], derived=_refusal_removed) -> list[Group]:
    """Each build with its quantisations, most downloaded group first. `derived(parent)`: whether a
    quantisation of `parent` joins its group (for the fallback list, only refusal-removed parents)."""
    up = {r: r for r in hits}

    def find(r):
        while up[r] != r:
            up[r] = up[up[r]]
            r = up[r]
        return r

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            up[rb] = ra

    by_stem: dict[tuple[str, str], str] = {}
    for h in hits.values():
        if h.quantized and h.parent in hits and derived(h.parent):
            union(h.parent, h.repo)
        key = (h.author, h.stem)
        if key in by_stem:
            union(by_stem[key], h.repo)
        else:
            by_stem[key] = h.repo
    groups: dict[str, Group] = {}
    for h in hits.values():
        groups.setdefault(find(h.repo), Group()).members.append(h)
    for g in groups.values():
        g.base = base_of(g, hits, derived)
    return sorted(groups.values(), key=lambda g: (-g.downloads, g.preference()[0].repo))


def base_of(g: Group, hits: dict[str, Hit], derived=_refusal_removed) -> str | None:
    """The model the group's builds start from: the first ancestor `derived` doesn't claim (for the
    fallback list, the first that isn't refusal-removed)."""
    for h in g.preference():
        seen = set()
        while h.parent and h.parent not in seen:
            seen.add(h.parent)
            if not derived(h.parent):
                return h.parent
            if h.parent not in hits:
                break
            h = hits[h.parent]
    return None


def base_capabilities(doc: dict) -> dict[str, float]:
    """Capability scores the list already gives base models: the official entry's own score, else the
    highest of the entries built on it."""
    out: dict[str, float] = {}
    derived: dict[str, float] = {}
    for e in doc.get("models", []):
        cap = (e.get("scores") or {}).get("capability")
        if not isinstance(cap, (int, float)):
            continue
        origin = e.get("origin") or {}
        if origin.get("method") == "original":
            out[e["repo"].lower()] = float(cap)
        elif origin.get("base"):
            b = origin["base"].lower()
            derived[b] = max(derived.get(b, 0.0), float(cap))
    return {**derived, **out}


def note_for(repo: str, g: Group, method: str, gb: float | None, license_: str) -> str:
    who = repo.split("/")[0]
    kind = "Refusal-removed" if method == "refusal-removal" else "Uncensored"
    on = f" of {g.base}" if g.base else ""
    size = f", {gb:.1f} GB" if gb else ""
    copies = f" across {len(g.members)} copies" if len(g.members) > 1 else ""
    return (f"{kind} build{on} by {who}{size}. Picked automatically as a fallback: {_count(g.downloads)} "
            f"Hugging Face downloads last month{copies}. {license_}.")


def _count(n: int) -> str:
    for div, unit in ((1e6, "M"), (1e3, "k")):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


def sync(doc: dict, groups: list[Group], pf, hf, licenses: set[str], deny: set[str] = frozenset(),
         max_entries: int = MAX_ENTRIES, per_base: int = PER_BASE, via: str = VIA, entry_for=None,
         kind: str = "fallback", spare_after: int | None = None) -> dict:
    """Bring the list's fallback entries (or, with via and entry_for, another automatically chosen
    set: catalog.py) in line with the ranked groups. Changes doc in place.

    Returns a summary like worthrescuing.sync: added, updated, removed, kept and skipped, each a
    list of lines for the editor to review. With spare_after, entries ranked past that many are
    written "spare": nodes look at them only once the rest of the list has its copies.
    """
    entry_for = entry_for or _entry_for
    summary = {"added": [], "updated": [], "removed": [], "kept": [], "skipped": []}
    models = doc.setdefault("models", [])
    others = {e["repo"].lower() for e in models if e.get("via") != via}
    mine = {e["repo"]: e for e in models if e.get("via") == via}
    deny = {d.lower() for d in deny} | {d.lower() for d in doc.get("deny", []) if isinstance(d, str)}
    caps = base_capabilities(doc)
    per: dict[str, int] = {}
    chosen: dict[str, dict] = {}

    for g in groups:
        if len(chosen) >= max_entries:
            break
        top = g.preference()[0].repo
        if any(h.repo.lower() in others for h in g.members):
            listed = next(h.repo for h in g.members if h.repo.lower() in others)
            summary["kept"].append(f"{top}: {listed} is already on the list")
            continue
        if any(h.repo.lower() in deny for h in g.members):
            summary["skipped"].append(f"{top}: on the deny list")
            continue
        base_key = (g.base or top).lower()
        if per.get(base_key, 0) >= per_base:
            summary["skipped"].append(f"{top}: already {per_base} {kind} build{'' if per_base == 1 else 's'} of "
                                     f"{g.base or 'this model'}")
            continue
        why = []
        for h in g.preference()[:TRIES_PER_GROUP]:
            entry, reason = entry_for(h, g, pf, hf, licenses, caps)
            if entry:
                chosen[h.repo] = entry
                per[base_key] = per.get(base_key, 0) + 1
                break
            why.append(f"{h.repo}: {reason}")
        else:
            summary["skipped"].append(f"{top} ({_count(g.downloads)} downloads): " + "; ".join(why))

    for i, (repo, entry) in enumerate(chosen.items()):
        if spare_after is not None and i >= spare_after:
            entry["spare"] = True
        existing = mine.get(repo)
        if existing is None:
            models.append(entry)
            summary["added"].append(f"{repo} @ {entry['revision'][:12]} "
                                    f"(priority {_priority(entry, doc.get('weights')):.2f})")
            continue
        material = ("revision", "scores", "boost", "origin", "target_replicas", "spare")
        changed = {k: existing.get(k) for k in material} != {k: entry.get(k) for k in material}
        moved = existing.get("revision") != entry["revision"]
        existing.clear()
        existing.update(entry)   # the note's download count is refreshed either way
        if changed:
            summary["updated"].append(repo + (f": now pinned to {entry['revision'][:12]}" if moved else ""))

    for e in list(models):
        if e.get("via") == via and e["repo"] not in chosen:
            models.remove(e)
            summary["removed"].append(f"{e['repo']} (no longer among the top {max_entries})")
    return summary


def vet(h: Hit, g: Group, pf, hf, licenses: set[str]):
    """The checks every automatically chosen entry passes: a license Flotilla seeds by default (the
    base's, when a build relabels a custom one), a Pirate Face torrent with its .torrent published,
    and Hugging Face still serving that commit. Returns (license, relabelled, revision, tree), or the
    reason it fails as a string."""
    lic = h.license if h.license and h.license != "other" else patient(hf.license, h.repo)
    if lic not in licenses:
        return f"license {lic or 'unknown'} isn't allowed by default"
    # Derivatives sometimes relabel a custom license as MIT or Apache-2.0: the base's license is the real one.
    base_lic = patient(hf.license, g.base) if g.base else None
    relabelled = bool(base_lic) and base_lic not in RELICENSABLE and base_lic != lic
    if relabelled:
        if base_lic not in licenses:
            return f"labelled {lic}, but its base {g.base} is {base_lic}, which isn't allowed by default"
        lic = base_lic
    info = patient(pf.torrent, h.repo)
    if not isinstance(info, dict) or not info.get("revision") or not info.get("magnet"):
        return "no Pirate Face torrent"
    if not info.get("torrent"):
        return "Pirate Face hasn't published its .torrent file"
    rev = info["revision"]
    if not isinstance(rev, str) or not re.fullmatch(r"[0-9a-f]{40}", rev):
        return f"Pirate Face pins no full commit ({str(rev)[:20]!r})"
    tree = patient(hf.tree, h.repo, rev)
    if tree is None:
        return f"Hugging Face no longer has commit {rev[:12]}"
    return lic, relabelled, rev, tree


def _entry_for(h: Hit, g: Group, pf, hf, licenses: set[str], caps: dict[str, float]):
    got = vet(h, g, pf, hf, licenses)
    if isinstance(got, str):
        return None, got
    lic, relabelled, rev, tree = got
    # As the name describes it; a merge may have involved training, so its method stays unstated.
    method = "refusal-removal" if WEIGHT_EDIT_RE.search(h.repo) and h.relation != "merge" else "unknown"
    origin = {"method": method}
    if g.base:
        origin["base"] = g.base
    if h.quantized:
        full = next((m.repo for m in g.preference() if not m.quantized), None)
        src = h.parent if h.parent in {m.repo for m in g.members} else full
        if src and src != h.repo:
            origin["repack_of"] = src
    cap = caps.get((g.base or "").lower(), DEFAULT_CAPABILITY)
    boost = round(BOOST_MIN + (BOOST_MAX - BOOST_MIN) * popularity(g.downloads), 3)
    gb = sum(f.size for f in tree.values()) / 1e9 if tree else None
    entry = {
        "repo": h.repo, "revision": rev,
        "scores": {"capability": int(round(cap)), "cyber": CYBER, "takedown_risk": TAKEDOWN_RISK[method]},
        "target_replicas": target_for_risk(TAKEDOWN_RISK[method]), "boost": boost,
        "note": note_for(h.repo, g, method, gb, lic),
        "origin": origin, "via": VIA,
    }
    if relabelled:
        entry["license"] = lic
    return entry, ""


def _priority(entry: dict, weights=None) -> float:
    from .feed import DEFAULT_WEIGHTS

    w = weights or DEFAULT_WEIGHTS
    s = sum(w.get(k, 0) * v / 100 for k, v in entry["scores"].items())
    return s / (sum(w.values()) or 1) * entry["boost"]
