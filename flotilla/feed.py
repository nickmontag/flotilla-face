"""The editorial priority feed ("call home").

A small JSON document, signed with minisign, that says which models matter most
and why. Editors score each model on a few axes; clients turn the scores into a
0..1 priority with weights that also live in the signed file.

Security properties (see DESIGN.md):
* Authenticity: must verify against a pinned public key; unsigned or badly
  signed feeds are ignored, never "used with a warning".
* Freshness: `expires` bounds how long a frozen/replayed feed is honoured.
* Rollback protection: the client remembers the highest `version` it accepted
  and refuses anything older.
* Pins: an entry may pin `infohash` and `revision`; a Pirate Face listing that
  disagrees with a pin is rejected, so even a compromised index cannot swap the
  torrent for a pinned model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .netsafe import FetchError, UnsafeURL, fetch
from .signing import PublicKey, SignatureError, verify

SCHEMA = 1
MAX_FEED_BYTES = 4 << 20
MAX_SIG_BYTES = 4096
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")

DEFAULT_WEIGHTS = {"capability": 0.35, "cyber": 0.25, "takedown_risk": 0.40}

# Where a model's weights come from, as its publisher describes it. Hash checks prove the bytes
# are the publisher's; this records what the publisher says they did, so people can judge how
# far to trust a model before running it.
#   base       the official release it starts from
#   method     original                   the official release from its maker
#              refusal-removal            weights edited to remove refusals, no training
#              fine-tune                  trained further
#              fine-tune+refusal-removal  both
#              unknown                    the publisher doesn't say whether training was involved
#   data       for fine-tunes: "public" (training data released) or "undisclosed"
#   repack_of  set when these files are a conversion or quantisation of another repo
ORIGIN_METHODS = ("original", "refusal-removal", "fine-tune", "fine-tune+refusal-removal", "unknown")
ORIGIN_DATA = ("public", "undisclosed")
# The project's priority list, signed, shipped inside the package (source: flotilla/data/).
BUNDLED_FEED = Path(__file__).parent / "data" / "priorities.json"


class FeedError(ValueError):
    pass


@dataclass
class FeedModel:
    repo: str
    scores: dict[str, float]
    quant: str | None = None
    infohash: str | None = None
    revision: str | None = None
    target_replicas: int | None = None
    boost: float = 1.0
    note: str = ""
    license: str | None = None
    local: bool = False      # came from the user's own my-priorities.json
    priority_override: float | None = None   # set by the user's drag-to-reorder ranking
    origin: dict | None = None   # see ORIGIN_METHODS


@dataclass
class Feed:
    version: int
    issued: datetime
    expires: datetime
    weights: dict[str, float]
    unlisted_priority: float
    target_replicas: int
    models: list[FeedModel]
    deny: set[str] = field(default_factory=set)
    source: str = ""
    signer: str = ""
    local_source: str = ""   # path of the user's own overrides, if any were applied
    order: list[str] = field(default_factory=list)          # the user's ranking, top first
    allot_gb: dict[str, float] = field(default_factory=dict)  # repo -> GB the user set aside for it
    picks: set[str] = field(default_factory=set)             # repos the user's own file lists as models

    @property
    def manual(self) -> frozenset[str]:
        """Models the user chose by hand: their own picks, and models they set space aside for.
        Reorienting never swaps these out."""
        return frozenset(self.picks) | frozenset(self.allot_gb)

    def by_repo(self) -> dict[str, list[FeedModel]]:
        out: dict[str, list[FeedModel]] = {}
        for m in self.models:
            out.setdefault(m.repo, []).append(m)
        return out

    def priority(self, m: FeedModel) -> float:
        if m.priority_override is not None:
            return max(0.0, min(1.0, m.priority_override))
        total_w = sum(self.weights.values()) or 1.0
        s = sum(self.weights.get(k, 0.0) * max(0.0, min(100.0, v)) / 100.0 for k, v in m.scores.items())
        return max(0.0, min(1.0, s / total_w * m.boost))


def neutral_feed() -> Feed:
    """Used when no trusted feed is available: everything is equally important."""
    now = datetime.now(timezone.utc)
    return Feed(0, now, now, dict(DEFAULT_WEIGHTS), 0.3, 3, [], set(), source="(none: neutral priorities)")


def _parse_time(v, what: str) -> datetime:
    if not isinstance(v, str):
        raise FeedError(f"{what} must be an ISO-8601 string")
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise FeedError(f"bad {what}: {v}") from e
    if t.tzinfo is None:
        raise FeedError(f"{what} needs a timezone")
    return t


def parse_feed(data: bytes) -> Feed:
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FeedError(f"feed is not JSON: {e}") from e
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise FeedError(f"unsupported feed schema (want {SCHEMA})")
    version = doc.get("version")
    if not isinstance(version, int) or version < 1:
        raise FeedError("version must be a positive integer")
    weights = doc.get("weights", DEFAULT_WEIGHTS)
    if not isinstance(weights, dict) or not all(isinstance(v, (int, float)) and v >= 0 for v in weights.values()):
        raise FeedError("weights must map names to non-negative numbers")
    defaults = doc.get("defaults", {})
    models = _parse_models(doc.get("models", []))
    deny = doc.get("deny", [])
    if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
        raise FeedError("deny must be a list of repo ids or infohashes")
    return Feed(
        version=version,
        issued=_parse_time(doc.get("issued"), "issued"),
        expires=_parse_time(doc.get("expires"), "expires"),
        weights={k: float(v) for k, v in weights.items()},
        unlisted_priority=float(defaults.get("unlisted_priority", 0.15)),
        target_replicas=int(defaults.get("target_replicas", 5)),
        models=models,
        deny={d.lower() for d in deny},
    )


def _parse_models(items) -> list[FeedModel]:
    if not isinstance(items, list):
        raise FeedError("models must be a list")
    models = []
    for i, e in enumerate(items):
        if not isinstance(e, dict):
            raise FeedError(f"models[{i}] must be an object")
        repo = e.get("repo")
        if not isinstance(repo, str) or not REPO_RE.match(repo):
            raise FeedError(f"models[{i}].repo is not author/model")
        ih = e.get("infohash")
        if ih is not None and (not isinstance(ih, str) or not HEX40.match(ih)):
            raise FeedError(f"models[{i}].infohash must be 40 lowercase hex")
        rev = e.get("revision")
        if rev is not None and (not isinstance(rev, str) or not HEX40.match(rev)):
            raise FeedError(f"models[{i}].revision must be a 40-hex commit")
        scores = e.get("scores", {})
        if not isinstance(scores, dict) or not all(isinstance(v, (int, float)) for v in scores.values()):
            raise FeedError(f"models[{i}].scores must be numbers")
        tr = e.get("target_replicas")
        if tr is not None and (not isinstance(tr, int) or not 1 <= tr <= 1000):
            raise FeedError(f"models[{i}].target_replicas out of range")
        boost = e.get("boost", 1.0)
        if not isinstance(boost, (int, float)) or not 0 <= boost <= 10:
            raise FeedError(f"models[{i}].boost out of range")
        lic = e.get("license")
        if lic is not None and not isinstance(lic, str):
            raise FeedError(f"models[{i}].license must be a string")
        m = FeedModel(repo, {k: float(v) for k, v in scores.items()}, e.get("quant"), ih, rev, tr, float(boost),
                      str(e.get("note", ""))[:500], lic.lower() if lic else None)
        m.origin = _parse_origin(e.get("origin"), i)
        models.append(m)
    return models


def _parse_origin(o, i: int) -> dict | None:
    if o is None:
        return None
    if not isinstance(o, dict):
        raise FeedError(f"models[{i}].origin must be an object")
    out = {}
    for key in ("base", "repack_of"):
        v = o.get(key)
        if v is not None:
            if not isinstance(v, str) or not REPO_RE.match(v):
                raise FeedError(f"models[{i}].origin.{key} is not author/model")
            out[key] = v
    method = o.get("method", "unknown")
    if method not in ORIGIN_METHODS:
        raise FeedError(f"models[{i}].origin.method must be one of {', '.join(ORIGIN_METHODS)}")
    out["method"] = method
    data = o.get("data")
    if data is not None:
        if data not in ORIGIN_DATA:
            raise FeedError(f"models[{i}].origin.data must be public or undisclosed")
        out["data"] = data
    return out


def origin_view(repo: str, origin: dict | None) -> dict | None:
    """Plain-language origin for people: {"text", "tag", "warn"}. tag is a short label or None."""
    if not origin:
        return None
    method, data = origin.get("method", "unknown"), origin.get("data")
    parts = []
    if origin.get("base"):
        parts.append(f"Based on {origin['base']}.")
    if method == "original":
        parts.append(f"The official release from {repo.split('/')[0]}.")
    elif method == "refusal-removal":
        parts.append("Refusals removed by a weight edit (abliteration), as its publisher describes it.")
    elif method == "unknown":
        parts.append("Its publisher doesn't say whether training was involved.")
    else:
        trained = ("Fine-tuned on data its publisher has released" if data == "public"
                   else "Fine-tuned on data its publisher hasn't released")
        parts.append(trained + (", and refusals removed." if method == "fine-tune+refusal-removal" else "."))
    src = origin.get("repack_of")
    third_party = bool(src) and src.split("/")[0].lower() != repo.split("/")[0].lower()
    if src:
        parts.append(f"A conversion of {src}" + (f" by {repo.split('/')[0]}." if third_party else " by the same publisher."))
    tag, warn = None, False
    if method in ("fine-tune", "fine-tune+refusal-removal") and data != "public":
        tag, warn = "fine-tuned", True
        parts.append("Hash checks can't rule out behaviour hidden in training.")
    elif method == "unknown":
        tag, warn = "method unstated", True
    elif third_party:
        tag = "repack"
    return {"text": " ".join(parts), "tag": tag, "warn": warn}


def verify_feed(data: bytes, sig: str, keys: list[PublicKey], min_version: int, now: datetime | None = None,
                check_expiry: bool = True) -> Feed:
    key, _tc = verify(data, sig, keys)
    feed = parse_feed(data)
    now = now or datetime.now(timezone.utc)
    if check_expiry and feed.expires <= now:
        raise FeedError(f"feed v{feed.version} expired at {feed.expires.isoformat()}")
    if feed.version < min_version:
        raise FeedError(f"feed v{feed.version} is older than v{min_version} already seen (rollback refused)")
    feed.signer = key.key_id_hex
    return feed


def load_feed(
    urls: list[str],
    keys: list[PublicKey],
    min_version: int,
    cache_dir: Path,
    allowed_hosts: tuple[str, ...],
    log=print,
    bundled: Path | None = None,
) -> Feed:
    """Newest valid copy wins: mirrors, else the last verified cache; the copy
    bundled with this release is used if nothing newer verifies. Else neutral.

    The bundled list ships inside the package, so it is exactly as trusted as
    the code: its signature is still checked, but not its expiry (an old
    install shouldn't lose its list just because nobody fetched an update)."""
    if not keys:
        log("feed: no trusted editor keys configured; using neutral priorities")
        return neutral_feed()
    chosen = _load_remote_or_cache(urls, keys, min_version, cache_dir, allowed_hosts, log)
    if bundled is not None and bundled.exists():
        try:
            b = verify_feed(bundled.read_bytes(), Path(str(bundled) + ".minisig").read_text(), keys, min_version,
                            check_expiry=False)
            b.source = "bundled with this release"
            if chosen is None or b.version > chosen.version:
                chosen = b
        except FeedError as e:
            if "rollback" not in str(e):  # an older shipped copy after an update is normal, not an error
                log(f"feed: bundled list unusable: {e}")
        except (OSError, SignatureError) as e:
            log(f"feed: bundled list unusable: {e}")
    if chosen:
        return chosen
    log("feed: no valid feed; using neutral priorities")
    return neutral_feed()


def _load_remote_or_cache(urls, keys, min_version, cache_dir, allowed_hosts, log) -> Feed | None:
    best: Feed | None = None
    for url in urls:
        try:
            data = fetch(url, max_bytes=MAX_FEED_BYTES, allowed=allowed_hosts, allow_file=True).body
            sig = fetch(url + ".minisig", max_bytes=MAX_SIG_BYTES, allowed=allowed_hosts, allow_file=True).body
            feed = verify_feed(data, sig.decode("utf-8", "replace"), keys, min_version)
            feed.source = url
            if best is None or feed.version > best.version:
                best = feed
                cache_dir.mkdir(parents=True, exist_ok=True)
                (cache_dir / "feed.json").write_bytes(data)
                (cache_dir / "feed.json.minisig").write_bytes(sig)
        except (FetchError, UnsafeURL, SignatureError, FeedError, UnicodeDecodeError) as e:
            log(f"feed: rejected {url}: {e}")
    if best:
        return best
    cached = cache_dir / "feed.json"
    if cached.exists():
        try:
            feed = verify_feed(cached.read_bytes(), (cache_dir / "feed.json.minisig").read_text(), keys, min_version)
            feed.source = f"{cached} (cached)"
            log(f"feed: using cached v{feed.version}")
            return feed
        except (OSError, SignatureError, FeedError) as e:
            log(f"feed: cached copy unusable: {e}")
    return None


# ---------------------------------------------------------------- your own changes

LOCAL_EXAMPLE = {
    "_comment": "Your own changes to the priority list. No signature needed: this file is yours. "
                "Entries here replace the published entry for the same repo, or add new models. "
                "Scores are 0-100. Delete this file to go back to the published list.",
    "replace_published": False,
    "weights": {"capability": 0.35, "cyber": 0.25, "takedown_risk": 0.40},
    "models": [
        {"repo": "author/model", "scores": {"capability": 80, "cyber": 50, "takedown_risk": 90},
         "target_replicas": 5, "note": "why I care about this one"}
    ],
    "deny": [],
}


def apply_local(feed: Feed, path: Path, log=print) -> Feed:
    """Layer the user's own my-priorities.json over the published list.

    Unsigned on purpose: it lives on the user's machine, and anyone who can
    write it can already change the program. A broken file is reported and
    ignored, never fatal."""
    if not path.exists():
        return feed
    try:
        doc = json.loads(path.read_text())
        if not isinstance(doc, dict):
            raise FeedError("must be a JSON object")
        models = [m for m in _parse_models(doc.get("models", [])) if m.repo != "author/model"]
        deny = doc.get("deny", [])
        if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
            raise FeedError("deny must be a list of repo ids or infohashes")
        weights = doc.get("weights")
        if weights is not None and (not isinstance(weights, dict)
                                    or not all(isinstance(v, (int, float)) and v >= 0 for v in weights.values())):
            raise FeedError("weights must map names to non-negative numbers")
        order = _check_order(doc.get("order", []))
        allot = _check_allot(doc.get("allot_gb", {}))
    except (OSError, ValueError) as e:
        log(f"feed: ignoring your {path}: {e}")
        return feed
    published = {(m.repo, m.quant): m for m in feed.models}
    for m in models:
        m.local = True
        if m.origin is None and (m.repo, m.quant) in published:
            m.origin = published[(m.repo, m.quant)].origin   # rescoring a model doesn't change where it came from
    keys = {(m.repo, m.quant) for m in models}
    base = [] if doc.get("replace_published") else [m for m in feed.models if (m.repo, m.quant) not in keys]
    feed.models = base + models
    feed.deny |= {d.lower() for d in deny}
    if weights:
        feed.weights = {k: float(v) for k, v in weights.items()}
    if order:
        # Your ranking becomes the priority: top 1.0, falling evenly to 0.3 at the bottom.
        by_repo = feed.by_repo()
        n = len(order)
        for i, repo in enumerate(order):
            p = rank_priority(i, n)
            ms = by_repo.get(repo)
            if not ms:  # e.g. a model you carry that isn't on any list
                ms = [FeedModel(repo, {}, note="ranked by you", local=True)]
                feed.models.extend(ms)
            for m in ms:
                m.priority_override = p
        feed.order = order
    feed.allot_gb = allot
    feed.picks = {m.repo for m in models}
    feed.local_source = str(path)
    return feed


def rank_priority(i: int, n: int) -> float:
    return 1.0 if n <= 1 else round(1.0 - 0.7 * i / (n - 1), 4)


def read_order(path: Path) -> list[str]:
    try:
        return _check_order(_read_local(path).get("order", []))
    except (OSError, ValueError):
        return []


def _check_order(order) -> list[str]:
    if not isinstance(order, list) or len(order) > 2000:
        raise FeedError("order must be a list of up to 2000 repos")
    out = []
    for r in order:
        if not isinstance(r, str) or not REPO_RE.match(r):
            raise FeedError(f"order: {r!r} is not author/model")
        if r not in out:
            out.append(r)
    return out


def _check_allot(allot) -> dict[str, float]:
    if not isinstance(allot, dict):
        raise FeedError("allot_gb must map repos to GB")
    out = {}
    for r, v in allot.items():
        if not isinstance(r, str) or not REPO_RE.match(r):
            raise FeedError(f"allot_gb: {r!r} is not author/model")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 1e6:
            raise FeedError(f"allot_gb[{r}] must be a number of GB")
        if v > 0:
            out[r] = float(v)
    return out


# ---------------------------------------------------------------- picks: models you add by link

_HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co"}
_PF_HOSTS = {"pirateface.co", "www.pirateface.co"}
_NOT_MODELS = {"datasets", "spaces", "collections", "docs", "papers", "organizations", "settings", "api", "models"}


def parse_model_ref(text: str) -> tuple[str, str | None]:
    """Turn what a user pastes into (repo, revision or None).

    Accepts a Hugging Face model URL (optionally .../tree/<commit>), a Pirate Face
    model page, a Pirate Face magnet link, or plain author/model."""
    import urllib.parse

    t = (text or "").strip()
    if not t:
        raise FeedError("paste a Hugging Face or Pirate Face link, or author/model")
    if t.startswith("magnet:?"):
        from .magnet import MagnetError, parse_magnet

        try:
            m = parse_magnet(t)
        except MagnetError as e:
            raise FeedError(f"that magnet link isn't usable: {e}") from e
        for ws in m.web_seeds:
            p = urllib.parse.urlsplit(ws)
            parts = [x for x in p.path.split("/") if x]
            if p.hostname in _PF_HOSTS and len(parts) >= 4 and parts[:2] == ["api", "ws"]:
                repo, rev = f"{parts[2]}/{parts[3]}", (parts[4] if len(parts) > 4 else None)
                break
        else:
            raise FeedError("that magnet link doesn't say which model it is (no Pirate Face web seed)")
    elif t.startswith(("http://", "https://")):
        p = urllib.parse.urlsplit(t)
        host = (p.hostname or "").lower()
        parts = [urllib.parse.unquote(x) for x in p.path.split("/") if x]
        if host not in _HF_HOSTS | _PF_HOSTS:
            raise FeedError("only huggingface.co and pirateface.co links are supported")
        if not parts or parts[0] in _NOT_MODELS:
            kind = parts[0].rstrip("s") if parts else "page"
            raise FeedError(f"that's a {kind} link, not a model page")
        if len(parts) < 2:
            raise FeedError("that link points to a user or organisation, not a model")
        repo, rev = f"{parts[0]}/{parts[1]}", None
        if host in _HF_HOSTS and len(parts) >= 4 and parts[2] in ("tree", "blob", "resolve", "commit"):
            rev = parts[3]
    else:
        repo, rev = t.strip("/"), None
    if not REPO_RE.match(repo):
        raise FeedError(f"{repo!r} doesn't look like author/model")
    if rev is not None and not HEX40.match(rev):
        rev = None  # a branch name like "main" isn't a pin
    return repo, rev


def _read_local(path: Path) -> dict:
    if not path.exists():
        return {"_comment": LOCAL_EXAMPLE["_comment"], "models": [], "deny": []}
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict):
        raise FeedError(f"{path} must be a JSON object")
    doc.setdefault("models", [])
    return doc


def _write_local(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(path)


def add_pick(path: Path, text: str, priority: float = 100, note: str = "") -> tuple[str, bool]:
    """Add (or update) a model in the user's own list. Returns (repo, was_new)."""
    repo, rev = parse_model_ref(text)
    if not 0 <= priority <= 100:
        raise FeedError("priority must be between 0 and 100")
    doc = _read_local(path)
    models = [m for m in doc["models"] if isinstance(m, dict)]
    entry = {"repo": repo, "scores": {"capability": priority, "cyber": priority, "takedown_risk": priority},
             "note": note or "added by you"}
    if rev:
        entry["revision"] = rev
    was_new = not any(m.get("repo") == repo for m in models)
    doc["models"] = [m for m in models if m.get("repo") != repo and m.get("repo") != "author/model"] + [entry]
    doc["deny"] = [d for d in doc.get("deny", []) if isinstance(d, str) and d.lower() != repo.lower()]
    _parse_models(doc["models"])  # never write something apply_local would reject
    _write_local(path, doc)
    return repo, was_new


def remove_pick(path: Path, text: str) -> str | None:
    """Remove a model from the user's own list. Returns the repo, or None if it wasn't there."""
    repo, _ = parse_model_ref(text)
    if not path.exists():
        return None
    doc = _read_local(path)
    keep = [m for m in doc["models"] if not (isinstance(m, dict) and m.get("repo") == repo)]
    if len(keep) == len(doc["models"]):
        return None
    doc["models"] = keep
    _write_local(path, doc)
    return repo


def list_picks(path: Path) -> list[dict]:
    try:
        doc = _read_local(path)
    except (OSError, ValueError):
        return []
    return [m for m in doc.get("models", []) if isinstance(m, dict) and m.get("repo") not in (None, "author/model")]


def set_order(path: Path, repos: list[str]) -> list[str]:
    """Save the user's ranking (top first) to their own list file."""
    order = _check_order(repos)
    doc = _read_local(path)
    doc["order"] = order
    _write_local(path, doc)
    return order


def set_allot(path: Path, repo: str, gb: float | None) -> dict[str, float]:
    """Set (or with 0/None clear) the GB the user sets aside for one model."""
    if not REPO_RE.match(repo or ""):
        raise FeedError(f"{repo!r} is not author/model")
    doc = _read_local(path)
    allot = _check_allot(doc.get("allot_gb", {}))
    if gb:
        allot.update(_check_allot({repo: gb}))
    else:
        allot.pop(repo, None)
    doc["allot_gb"] = allot
    _write_local(path, doc)
    return allot


def read_allot(path: Path) -> dict[str, float]:
    try:
        return _check_allot(_read_local(path).get("allot_gb", {}))
    except (OSError, ValueError):
        return {}
