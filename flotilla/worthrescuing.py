"""Pirate Face's "Worth rescuing" page, turned into entries for the published list.

An editor tool. The page has no API, so this reads its HTML: the historic shortlist and the
capability ranking (Artificial Analysis's Intelligence Index). Nodes never read the page. An
editor runs `flotilla editor sync-worth-rescuing`, reviews what changed, then signs and
publishes the list as usual, so every model a node carries is still in the signed list,
pinned to a commit.

Entries this tool manages carry "via": "pirateface/worth-rescuing". It adds, updates and
removes only those. A model the list already has for another reason is left alone.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .feed import REPO_RE, target_for_risk

PAGE_URL = "https://pirateface.co/worth-rescuing"
VIA = "pirateface/worth-rescuing"

# Official releases: not at risk from content rules, but from policy (export controls, a lab
# withdrawing a release). Scored below the takedown-risk models, so those still come first.
CYBER = 40
TAKEDOWN_RISK = 30
# Official releases are safe on Hugging Face for now; a node should spend space on them only once
# the at-risk models' files have a couple of copies. The planner divides a file's value by 1 + its
# copies, so an official release at under half the weight of the lowest at-risk model loses to any
# at-risk file with one copy, and fills in once those have two.
OFFICIAL_BOOST = 0.45
HISTORIC_CAPABILITY = 75
TARGET_REPLICAS = target_for_risk(TAKEDOWN_RISK)
RELICENSABLE = {"mit", "apache-2.0", "wtfpl"}


@dataclass
class PageModel:
    repo: str
    historic: bool = False        # on the editorial shortlist
    rank: int | None = None       # place in the capability ranking
    index: float | None = None    # Artificial Analysis Intelligence Index score


def _flight(html: str) -> dict[str, object]:
    """The page's embedded React data ("flight" rows: `id:json`), keyed by row id."""
    rows: dict[str, object] = {}
    pushes = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    text = "".join(json.loads('"' + p + '"') for p in pushes)
    for line in text.splitlines():
        m = re.match(r"^([0-9a-f]+):(.*)$", line)
        if not m:
            continue
        try:
            rows[m.group(1)] = json.loads(m.group(2))
        except ValueError:
            pass   # not JSON (module and hint rows)
    return rows


def _elements(node, refs, depth=0):
    """Every React element ["$", type, key, props] under node, following "$L" references."""
    if depth > 200:
        return
    if isinstance(node, str) and node.startswith("$L") and node[2:] in refs:
        yield from _elements(refs[node[2:]], refs, depth + 1)
    elif isinstance(node, list):
        if len(node) == 4 and node[0] == "$" and isinstance(node[3], dict):
            yield node
            yield from _elements(node[3].get("children"), refs, depth + 1)
        else:
            for x in node:
                yield from _elements(x, refs, depth + 1)


def _text(node, refs) -> str:
    if isinstance(node, (str, int, float)) and not (isinstance(node, str) and node.startswith("$")):
        return str(node)
    if isinstance(node, str) and node.startswith("$L") and node[2:] in refs:
        return _text(refs[node[2:]], refs)
    if isinstance(node, list):
        if len(node) == 4 and node[0] == "$" and isinstance(node[3], dict):
            return _text(node[3].get("children"), refs)
        return "".join(_text(x, refs) for x in node)
    return ""


def _cls(el) -> str:
    c = el[3].get("className")
    return c if isinstance(c, str) else ""


def parse_page(html: str) -> list[PageModel]:
    """Models on the page, in page order; a model in both lists appears once.

    Raises ValueError if the page no longer looks the way this expects.
    """
    refs = _flight(html)
    out: dict[str, PageModel] = {}
    seen_tables = set()
    for root in refs.values():
        for table in _elements(root, refs):
            if table[1] != "table" or "worth-rescuing-table" not in _cls(table):
                continue
            kind = "historic" if "archive" in _cls(table) else "ranked" if "capability" in _cls(table) else None
            if not kind or kind in seen_tables:
                continue
            seen_tables.add(kind)
            for tr in _elements(table[3].get("children"), refs):
                if tr[1] != "tr":
                    continue
                repo = rank = index = None
                for td in _elements(tr[3].get("children"), refs):
                    if td[1] != "td":
                        continue
                    c = _cls(td)
                    if "worth-rescuing-model-cell" in c:
                        for a in _elements(td[3].get("children"), refs):
                            href = a[3].get("href")
                            if isinstance(href, str) and REPO_RE.match(href.strip("/")):
                                repo = href.strip("/")
                                break
                    elif "c-rank" in c:
                        rank = _num(_text(td, refs), int)
                    elif "worth-rescuing-score" in c:
                        index = _num(_text(td, refs), float)
                if not repo:
                    continue
                m = out.setdefault(repo, PageModel(repo))
                if kind == "historic":
                    m.historic = True
                else:
                    m.rank, m.index = rank, index
    if not out or "ranked" not in seen_tables or "historic" not in seen_tables:
        raise ValueError("couldn't read the Worth Rescuing page; has it changed?")
    return list(out.values())


def _num(s, kind):
    try:
        return kind(re.sub(r"[^0-9.]", "", s or "")) if s else None
    except ValueError:
        return None


def capability_from_index(index: float | None) -> int:
    """Artificial Analysis's index (roughly 10-50 for open models) on the list's 0-100 scale."""
    if index is None:
        return HISTORIC_CAPABILITY
    return int(max(50, min(95, round(55 + 0.9 * index))))


def note_for(m: PageModel, license_: str) -> str:
    where = []
    if m.historic:
        where.append("historic shortlist")
    if m.rank is not None:
        where.append(f"#{m.rank} by capability" + (f", Artificial Analysis index {m.index:g}" if m.index else ""))
    return (f"Official release from {m.repo.split('/')[0]}. On Pirate Face's Worth Rescuing page "
            f"({'; '.join(where)}). {license_}.")


def sync(doc: dict, page: list[PageModel], pf, hf, licenses: set[str]) -> dict:
    """Bring the list's Worth Rescuing entries in line with the page. Changes doc in place.

    Returns a summary: added, updated, removed, kept (listed for another reason) and skipped
    (with the reason), each a list of strings for the editor to review.
    """
    summary = {"added": [], "updated": [], "removed": [], "kept": [], "skipped": []}
    models = doc.setdefault("models", [])
    by_repo = {e["repo"]: e for e in models}
    on_page = {m.repo for m in page}

    for m in page:
        existing = by_repo.get(m.repo)
        if existing is not None and existing.get("via") != VIA:
            summary["kept"].append(f"{m.repo} (already on the list)")
            continue
        info = pf.torrent(m.repo)
        if not isinstance(info, dict) or not info.get("revision"):
            summary["skipped"].append(f"{m.repo}: no Pirate Face torrent")
            continue
        rev = info["revision"]
        # It is pinned into the list and used in Hugging Face URLs and cache file names.
        if not isinstance(rev, str) or not re.fullmatch(r"[0-9a-f]{40}", rev):
            summary["skipped"].append(f"{m.repo}: Pirate Face pins no full commit ({str(rev)[:20]!r})")
            continue
        lic = hf.license(m.repo)
        if lic not in licenses:
            summary["skipped"].append(f"{m.repo}: license {lic or 'unknown'} isn't allowed by default")
            continue
        if hf.tree(m.repo, rev) is None:
            summary["skipped"].append(f"{m.repo}: Hugging Face no longer has commit {rev[:12]}")
            continue
        note = note_for(m, lic)
        if not info.get("torrent"):
            note += " Pirate Face hasn't published its .torrent file yet."
        entry = {
            "repo": m.repo, "revision": rev,
            "scores": {"capability": capability_from_index(m.index), "cyber": CYBER, "takedown_risk": TAKEDOWN_RISK},
            "target_replicas": TARGET_REPLICAS, "boost": OFFICIAL_BOOST, "note": note,
            "origin": {"method": "original"}, "via": VIA,
        }
        if existing is None:
            models.append(entry)
            by_repo[m.repo] = entry
            summary["added"].append(f"{m.repo} @ {rev[:12]} ({lic})")
        elif {k: existing.get(k) for k in entry} != entry:
            moved = existing.get("revision") != rev
            existing.clear()
            existing.update(entry)
            summary["updated"].append(f"{m.repo}" + (f": now pinned to {rev[:12]}" if moved else ""))

    for e in list(models):
        if e.get("via") == VIA and e["repo"] not in on_page:
            models.remove(e)
            summary["removed"].append(f"{e['repo']} (no longer on the page)")
    return summary


def license_mismatches(doc: dict, hf) -> list[str]:
    """Entries whose license differs from the license of the model they're built on.

    Derivatives sometimes relabel a custom license as MIT or Apache-2.0; the list should
    carry the real one (an entry's "license" field overrides what Hugging Face says).
    """
    out = []
    for e in doc.get("models", []):
        base = (e.get("origin") or {}).get("base")
        if not base:
            continue
        theirs = (hf.license(base) or "unknown").lower()
        if theirs in RELICENSABLE:
            continue   # a derivative of an MIT/Apache/WTFPL model may carry another license
        mine = (e.get("license") or hf.license(e["repo"]) or "unknown").lower()
        if mine != theirs:
            out.append(f"{e['repo']}: {mine}, but its base {base} is {theirs}")
    return out
