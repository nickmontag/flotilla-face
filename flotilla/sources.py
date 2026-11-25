"""Read-only clients for Pirate Face and Hugging Face.

Nothing here downloads model weights. These calls fetch listings, .torrent
metadata and hash manifests, all through netsafe.fetch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .netsafe import FetchError, fetch, fetch_json, parse_link_next

PF_BASE = "https://pirateface.co"
HF_BASE = "https://huggingface.co"
MAX_TORRENT_BYTES = 64 << 20


TORRENT_CACHE_DAYS = 30
SERVER_ERRORS_BEFORE_BACKOFF = 3
NEW_TORRENTS_MAX_BYTES = 1 << 20
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}")
# A revision HF.tree accepts: a full commit hash, or a plain branch name such as "main". It ends up
# in a URL and in a cache file name, so nothing that could be a path or a query.
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_DESC = re.compile(r"([0-9.]+)\s*GB.*?(\d+)\s*seeder", re.I)


def is_repo_id(v) -> bool:
    """Whether v is a Hugging Face model name (author/name). Anything from Pirate Face that reaches a
    URL or a file name is checked with this first."""
    return isinstance(v, str) and bool(_REPO.match(v))


def is_revision(v) -> bool:
    return isinstance(v, str) and ((bool(_HEX40.fullmatch(v))) or (bool(_BRANCH.fullmatch(v)) and ".." not in v))


def parse_new_torrents(body: bytes) -> list[dict]:
    """Items of Pirate Face's new-torrents RSS feed. Anything malformed is skipped; a document
    declaring entities is refused outright (no entity expansion from a remote feed).

    Only UTF-8 is read, and the parser is told so: a document in another encoding (UTF-16, say)
    would hide a DOCTYPE from the check below while the parser still read it."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise FetchError("new-torrents.xml", None, "feed isn't UTF-8; refused") from e
    if "\x00" in text:
        raise FetchError("new-torrents.xml", None, "feed isn't UTF-8; refused")
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise FetchError("new-torrents.xml", None, "feed declares a DOCTYPE or entities; refused")
    try:
        parser = ElementTree.XMLParser(encoding="utf-8")   # overrides any encoding the document declares
        parser.feed(text.encode("utf-8"))
        root = parser.close()
    except ElementTree.ParseError as e:
        raise FetchError("new-torrents.xml", None, f"not RSS: {e}") from e
    out = []
    for item in root.iter("item"):
        repo = (item.findtext("title") or "").strip()
        ih = (item.findtext("guid") or "").strip().lower()
        if not _REPO.match(repo) or not _HEX40.fullmatch(ih):
            continue
        m = _DESC.search(item.findtext("description") or "")
        try:
            gb, seeders = (float(m.group(1)), int(m.group(2))) if m else (None, None)
        except ValueError:
            continue
        out.append({"repo": repo, "infohash": ih, "gb": gb, "seeders": seeders})
    return out


# Gentle on Pirate Face, for the whole process (a refresh makes a new client each time):
# requests at least PF_MIN_GAP apart, and none at all while Pirate Face has asked us to wait.
PF_MIN_GAP = 0.25            # seconds
PF_WAIT_DEFAULT = 300.0      # a 429 or 503 without Retry-After: wait this long
PF_WAIT_MAX = 3600.0
_pf_lock = threading.Lock()
_pf_last = 0.0               # when the last request went out
_pf_not_before = 0.0         # no requests before this (Retry-After)


class PirateFace:
    """Pirate Face's API, used gently: thousands of nodes may run this.

    * .torrent files are cached by infohash (an infohash pins the content, so a
      cached file never goes stale), and a cached file is re-verified before use.
    * Requests go out at least PF_MIN_GAP apart. A 429 or 503 stops every request
      from this node for as long as its Retry-After says (PF_WAIT_DEFAULT if it
      doesn't say, at most PF_WAIT_MAX).
    * After a few server errors in a row (5xx, 429, timeouts) the client stops
      calling Pirate Face for the rest of this refresh instead of retrying.
    """

    def __init__(self, allowed: tuple[str, ...], base: str = PF_BASE, cache_dir: Path | None = None):
        self.base = base.rstrip("/")
        self.allowed = allowed
        self.cache_dir = cache_dir
        self._server_errors = 0

    def _guard(self) -> None:
        global _pf_last
        if self._server_errors >= SERVER_ERRORS_BEFORE_BACKOFF:
            raise FetchError(self.base, None, "Pirate Face is having trouble; no more requests until the next refresh")
        with _pf_lock:
            now = time.time()
            if now < _pf_not_before:
                raise FetchError(self.base, 429, f"Pirate Face asked for a pause; no requests for "
                                                 f"{_pf_not_before - now:.0f} s more")
            wait = _pf_last + PF_MIN_GAP - now
            if wait > 0:
                time.sleep(wait)
            _pf_last = time.time()

    def _note(self, e: FetchError) -> None:
        global _pf_not_before
        if e.status is None or e.status == 429 or e.status >= 500:
            self._server_errors += 1
        if e.status in (429, 503):
            wait = min(PF_WAIT_MAX, e.retry_after if e.retry_after is not None else PF_WAIT_DEFAULT)
            with _pf_lock:
                _pf_not_before = max(_pf_not_before, time.time() + wait)

    def _get(self, path: str, **params):
        self._guard()
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        try:
            doc = fetch_json(f"{self.base}{path}{'?' + q if q else ''}", allowed=self.allowed)
        except FetchError as e:
            self._note(e)
            raise
        self._server_errors = 0
        return doc

    def _get_dict(self, path: str, **params) -> dict:
        """_get for an endpoint that answers with a JSON object. Anything else is a FetchError, as
        for an answer that isn't JSON at all."""
        doc = self._get(path, **params)
        if not isinstance(doc, dict):
            raise FetchError(f"{self.base}{path}", None, f"expected a JSON object, got {type(doc).__name__}")
        return doc

    def licenses(self) -> list[dict]:
        """[{"id", "label", "acceptFirst"}]; entries without a string id are left out."""
        lics = self._get_dict("/api/licenses").get("licenses")
        out = []
        for x in lics if isinstance(lics, list) else []:
            if isinstance(x, dict) and isinstance(x.get("id"), str):
                out.append({**x, "label": x["label"] if isinstance(x.get("label"), str) else x["id"]})
        return out

    def new_torrents(self) -> list[dict]:
        """Pirate Face's feed of new torrents that need seeders (the one its own seeding agents
        follow): [{"repo", "infohash", "gb", "seeders"}], newest first. Its /api/models catalog
        became a web page, so this RSS feed is the machine-readable list left."""
        self._guard()
        try:
            body = fetch(f"{self.base}/api/feeds/new-torrents.xml", max_bytes=NEW_TORRENTS_MAX_BYTES,
                         allowed=self.allowed, accept="application/rss+xml").body
        except FetchError as e:
            self._note(e)
            raise
        self._server_errors = 0
        return parse_new_torrents(body)

    def rescue(self) -> dict:
        return self._get_dict("/api/rescue")

    def torrent(self, repo: str, quant: str | None = None) -> dict | None:
        """Pirate Face's listing for a repo, or None if it lists none (404). A 400 isn't taken to
        mean "not listed": it says the request was wrong, not that the model is gone, so it is a
        FetchError like any other failed lookup and a held model is kept."""
        try:
            return self._get_dict("/api/torrents", repo=repo, quant=quant)
        except FetchError as e:
            if e.status == 404:
                return None
            raise

    def torrent_file(self, infohash: str) -> bytes:
        if not isinstance(infohash, str) or not _HEX40.fullmatch(infohash):
            raise ValueError("bad infohash")
        from .torrentmeta import TorrentError, parse_torrent

        cached = self.cache_dir / "torrents" / f"{infohash}.torrent" if self.cache_dir else None
        if cached and cached.exists():
            raw = cached.read_bytes()
            try:
                if parse_torrent(raw).infohash_v1 == infohash:
                    os.utime(cached)  # keep recently used files out of the pruning
                    return raw
            except (TorrentError, ValueError):
                pass
            cached.unlink(missing_ok=True)
        self._guard()
        try:
            raw = fetch(f"{self.base}/api/torrents/{infohash}.torrent", max_bytes=MAX_TORRENT_BYTES,
                        allowed=self.allowed, accept="application/x-bittorrent").body
        except FetchError as e:
            self._note(e)
            raise
        self._server_errors = 0
        if cached:
            try:
                if parse_torrent(raw).infohash_v1 == infohash:  # only cache what's genuinely this torrent
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_bytes(raw)
            except (TorrentError, ValueError):
                pass
        return raw

    def torrent_status(self, infohash: str) -> dict | None:
        """Pirate Face's record of one torrent (GET /api/torrents/<infohash>): {"infohash", "repo",
        "status", "supersededBy"}, or None if it has none."""
        if not re.fullmatch(r"[0-9a-f]{40}", infohash):
            raise ValueError(f"not an infohash: {infohash!r}")
        try:
            doc = self._get(f"/api/torrents/{infohash}")
        except FetchError as e:
            if e.status in (400, 404):
                return None
            raise
        return doc if isinstance(doc, dict) else None

    def replacement(self, infohash: str, hops: int = 3) -> str | None:
        """The torrent Pirate Face replaced this one with (following a chain of replacements), or None."""
        seen, ih = {infohash}, infohash
        for _ in range(hops):
            doc = self.torrent_status(ih) or {}
            nxt = str(doc.get("supersededBy") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{40}", nxt) or nxt in seen:
                break
            seen.add(nxt)
            ih = nxt
        return ih if ih != infohash else None

    def request_rescue(self, repo: str) -> dict:
        """Ask Pirate Face to add a model it doesn't have yet (POST /api/rescue, as its own CLI does).
        Sends only the model's name; no account."""
        if not _REPO.match(repo):
            raise ValueError(f"not a model name: {repo!r}")
        from .netsafe import post_json

        resp = post_json(f"{self.base}/api/rescue", {"repoId": repo}, allowed=self.allowed)
        try:
            doc = json.loads(resp.body or b"{}")
        except ValueError:
            doc = {}
        return doc if isinstance(doc, dict) else {}

    def remember_torrent(self, infohash: str, raw: bytes) -> None:
        """Cache a .torrent obtained elsewhere (from the swarm, while Pirate Face is down)."""
        from .torrentmeta import TorrentError, parse_torrent

        if not self.cache_dir or not isinstance(infohash, str) or not _HEX40.fullmatch(infohash):
            return
        try:
            if parse_torrent(raw).infohash_v1 != infohash:
                return
        except (TorrentError, ValueError):
            return
        cached = self.cache_dir / "torrents" / f"{infohash}.torrent"
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(raw)

    def file_swarm(self, root: str) -> dict | None:
        """The per-file swarm for one file, by its BEP 52 pieces root (GET /api/files/<root>):
        {"root", "name", "length", "infohashV2", "torrent", "siblings", ...}, or None if Pirate Face
        doesn't list one. Raises FetchError when it can't be reached."""
        if not re.fullmatch(r"[0-9a-f]{64}", root):
            raise ValueError(f"not a pieces root: {root!r}")
        self._guard()
        try:
            doc = fetch_json(f"{self.base}/api/files/{root}", allowed=self.allowed, max_bytes=1 << 20)
        except FetchError as e:
            if e.status in (404, 410):
                return None
            self._note(e)
            raise
        self._server_errors = 0
        return doc if isinstance(doc, dict) else None

    def file_torrent(self, path: str) -> bytes:
        """A per-file .torrent, by the path /api/files gave for it."""
        if not re.fullmatch(r"/api/files/[0-9a-f]{64}\.torrent", path):
            raise ValueError(f"unexpected per-file torrent path {path!r}")
        self._guard()
        try:
            raw = fetch(f"{self.base}{path}", max_bytes=1 << 20, allowed=self.allowed,
                        accept="application/x-bittorrent").body
        except FetchError as e:
            self._note(e)
            raise
        self._server_errors = 0
        return raw

    def trackers(self, key: str) -> list[str]:
        """The trackers Pirate Face gives this account (GET /api/trackers with its key): the public
        ones, then the account's own seeding tracker. The key goes to pirateface.co only."""
        host = (urllib.parse.urlsplit(self.base).hostname or "").lower()
        body = fetch(f"{self.base}/api/trackers", max_bytes=16 << 10, allowed=self.allowed,
                     auth=(host, f"Bearer {key}")).body
        return body.decode("utf-8", "replace").split()

    def prune_cache(self, days: float = TORRENT_CACHE_DAYS) -> None:
        if not self.cache_dir or not (self.cache_dir / "torrents").exists():
            return
        cutoff = time.time() - days * 86400
        for f in (self.cache_dir / "torrents").glob("*.torrent"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


@dataclass
class HFFile:
    size: int
    sha256: str | None   # LFS files (None when Hugging Face hides it, as it does on gated repos)
    git_oid: str | None  # non-LFS files: git blob SHA-1
    pointer_oid: str | None = None  # LFS files: git blob SHA-1 of the LFS pointer, which commits to the SHA-256


def _hex(v, n: int) -> str | None:
    """v lower-cased if it is n hex digits, else None (Hugging Face masks hidden hashes with '*')."""
    if isinstance(v, str) and len(v) == n and all(c in "0123456789abcdef" for c in v.lower()):
        return v.lower()
    return None


def lfs_pointer_oid(sha256: str, size: int) -> str:
    """Git blob SHA-1 of the Git LFS pointer for a file with this SHA-256 and size.

    On gated repos Hugging Face hides LFS SHA-256s from anonymous clients but still lists
    each pointer's git ID. The pointer commits to the SHA-256, so a SHA-256 from elsewhere
    (Pirate Face's manifest) can still be checked against Hugging Face.
    """
    ptr = f"version https://git-lfs.github.com/spec/v1\noid sha256:{sha256.lower()}\nsize {size}\n".encode()
    return hashlib.sha1(b"blob %d\0" % len(ptr) + ptr).hexdigest()


class HuggingFace:
    """Hash manifests straight from huggingface.co (an independent second source).

    Trees are cached on disk per (repo, revision): a commit hash is immutable,
    so a cached tree never goes stale. A branch's tree (main) is kept for an hour. A repo or
    revision Hugging Face says is gone or closed to us (401/403/404/410/451), and a repo with no
    license, is remembered for a day, so a refresh doesn't ask about it again each time.
    """

    def __init__(self, allowed: tuple[str, ...], cache_dir: Path, base: str = HF_BASE):
        self.base = base.rstrip("/")
        self.allowed = allowed
        self.cache_dir = cache_dir

    LICENSE_TTL = 7 * 86400
    MISSING_TTL = 86400          # a gone, gated or unlicensed answer: asked again after a day
    BRANCH_TTL = 3600            # a branch's tree (main): it moves, but not every refresh
    GONE = (401, 403, 404, 410, 451)

    def license(self, repo: str) -> str | None:
        """A repo's license (see license_lookup), or None if none is known."""
        return self.license_lookup(repo)[0]

    def license_lookup(self, repo: str) -> tuple[str | None, str]:
        """(license, how): a repo's license and where it came from.

        how is "huggingface" (Hugging Face has the repo; license None means its card names none),
        "gone" (Hugging Face says the repo is gone or closed to us) or "unreachable" (no answer:
        timeout, 429, 5xx). For the last two the license is the last one Hugging Face named for
        it, however old, or None if it never named one here. Taken-down models are what Flotilla
        keeps, so a license once read isn't forgotten because the repo went away.

        Hugging Face's answer is cached for a week (a license rarely changes); no license, or no
        repo, for a day. An answer that didn't arrive is never cached.
        """
        if not is_repo_id(repo):
            return None, "huggingface"
        cache = self.cache_dir / "hf-licenses.json"
        try:
            known = json.loads(cache.read_text()) if cache.exists() else {}
        except (OSError, ValueError):
            known = {}
        if not isinstance(known, dict):
            known = {}
        lic, at, kept, gone = self._cached_license(known.get(repo))
        if at is not None:
            age = time.time() - at
            if lic is None and age < self.MISSING_TTL:
                return (kept, "gone") if gone else (None, "huggingface")
            if lic and lic != "other" and age < self.LICENSE_TTL:
                return lic, "huggingface"   # ("other" was cached before license names were read: look again)
        try:
            doc = fetch_json(f"{self.base}/api/models/{repo}", allowed=self.allowed, max_bytes=4 << 20)
        except FetchError as e:
            if e.status not in self.GONE:
                return kept, "unreachable"   # no answer to remember
            doc, gone = None, True
        else:
            gone = False
        if gone:
            lic = None                      # kept stays what Hugging Face last named
        else:
            lic = license_from_card(doc) if isinstance(doc, dict) else None
            kept = lic
        known[repo] = {"license": lic, "at": time.time(), "kept": kept, "gone": gone}
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(known))
        except OSError:
            pass
        return (kept, "gone") if gone else (lic, "huggingface")

    @staticmethod
    def _cached_license(hit) -> tuple[str | None, float | None, str | None, bool]:
        """(license, when, kept, gone) from a cache entry; when is None if there's no usable entry.
        Entries written before "kept" existed are [license, when]; an old [None, when] can't say
        whether the repo was gone or had no license, so it is asked about again."""
        def _lic(v):
            return v if isinstance(v, str) and v else None

        if isinstance(hit, list) and len(hit) == 2 and isinstance(hit[1], (int, float)) and _lic(hit[0]):
            return _lic(hit[0]), float(hit[1]), _lic(hit[0]), False
        if isinstance(hit, dict) and isinstance(hit.get("at"), (int, float)):
            return _lic(hit.get("license")), float(hit["at"]), _lic(hit.get("kept")), hit.get("gone") is True
        return None, None, None, False

    def tree(self, repo: str, revision: str) -> dict[str, HFFile] | None:
        """Return {path: HFFile}, or None if the repo/revision is not readable on HF.

        A tree at a full commit hash is cached until unused for a while (a commit is immutable);
        a branch's, like "main", for BRANCH_TTL. A None is remembered for MISSING_TTL.
        """
        # Both end up in a URL and in a cache file name; they come from Pirate Face's listing.
        if not is_repo_id(repo) or not is_revision(revision):
            raise ValueError(f"not a model name and revision: {repo!r} @ {revision!r}")
        immutable = bool(_HEX40.fullmatch(revision))
        folder = self.cache_dir / "hf-trees"
        cache = folder / f"{repo.replace('/', '__')}@{revision}.json"
        gone = cache.with_suffix(".gone")       # when Hugging Face last said it isn't there
        if cache.resolve().parent != folder.resolve() or gone.resolve().parent != folder.resolve():
            raise ValueError(f"tree cache path for {repo!r} @ {revision!r} leaves the cache")
        try:
            if time.time() - float(gone.read_text()) < self.MISSING_TTL:
                return None
        except (OSError, ValueError):
            pass
        fresh = immutable or (cache.exists() and time.time() - cache.stat().st_mtime < self.BRANCH_TTL)
        if fresh and cache.exists():
            try:
                raw = json.loads(cache.read_text())
                if immutable:
                    os.utime(cache)    # in use: keep it out of prune_cache
                # Trees cached before masked hashes were recognised may hold '*' strings; refetch those.
                if not any(isinstance(v.get("sha256"), str) and not _hex(v["sha256"], 64) for v in raw.values()):
                    return {p: HFFile(**v) for p, v in raw.items()}
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        url = f"{self.base}/api/models/{repo}/tree/{urllib.parse.quote(revision, safe='')}?recursive=true"
        entries: list[dict] = []
        try:
            for _ in range(200):  # pagination guard
                resp = fetch(url, max_bytes=32 << 20, allowed=self.allowed, accept="application/json")
                page = resp.json()
                if not isinstance(page, list):
                    return None
                entries.extend(e for e in page if isinstance(e, dict))
                nxt = parse_link_next(resp.headers.get("link"))
                if not nxt:
                    break
                url = nxt
        except FetchError as e:
            if e.status in self.GONE:
                try:
                    gone.parent.mkdir(parents=True, exist_ok=True)
                    gone.write_text(str(time.time()))
                except OSError:
                    pass
                return None
            raise
        out = {}
        for e in entries:
            # An entry we can't read is left out: a torrent file it would have vouched for is refused.
            size, lfs = e.get("size", 0), e.get("lfs") or {}
            if e.get("type") != "file" or not isinstance(e.get("path"), str) or not isinstance(lfs, dict) \
                    or not isinstance(size, int) or isinstance(size, bool) or size < 0:
                continue
            if lfs:
                out[e["path"]] = HFFile(size, _hex(lfs.get("oid"), 64), None, _hex(e.get("oid"), 40))
            else:
                out[e["path"]] = HFFile(size, None, _hex(e.get("oid"), 40))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({p: vars(v) for p, v in out.items()}))
        gone.unlink(missing_ok=True)
        return out


    SEARCH_FIELDS = ("downloads", "likes", "cardData", "tags", "baseModels", "safetensors", "gguf", "pipeline_tag")

    def search(self, term: str, limit: int = 100) -> list[dict]:
        """Models whose names contain `term`, most downloaded (last 30 days) first. Editor tools only."""
        q = urllib.parse.urlencode([("search", term), ("sort", "downloads"), ("limit", str(limit))]
                                   + [("expand[]", f) for f in self.SEARCH_FIELDS])
        doc = fetch_json(f"{self.base}/api/models?{q}", allowed=self.allowed, max_bytes=64 << 20)
        return [m for m in doc if isinstance(m, dict) and isinstance(m.get("id"), str)] if isinstance(doc, list) else []

    def popular(self, pipeline_tag: str, limit: int = 1000) -> list[dict]:
        """The most downloaded models (last 30 days) with this pipeline tag, as `search` returns them.
        Editor tools only."""
        q = urllib.parse.urlencode([("pipeline_tag", pipeline_tag), ("sort", "downloads"), ("limit", str(limit))]
                                   + [("expand[]", f) for f in self.SEARCH_FIELDS])
        doc = fetch_json(f"{self.base}/api/models?{q}", allowed=self.allowed, max_bytes=64 << 20)
        return [m for m in doc if isinstance(m, dict) and isinstance(m.get("id"), str)] if isinstance(doc, list) else []

    def model_info(self, repo: str) -> dict | None:
        """One model's search fields (as `search` returns them), or None if Hugging Face doesn't have it."""
        if not is_repo_id(repo):
            return None
        q = urllib.parse.urlencode([("expand[]", f) for f in self.SEARCH_FIELDS])
        try:
            doc = fetch_json(f"{self.base}/api/models/{repo}?{q}", allowed=self.allowed, max_bytes=16 << 20)
        except FetchError as e:
            if e.status in (401, 403, 404, 410, 451):
                return None
            raise
        return doc if isinstance(doc, dict) else None

    def prune_cache(self, days: float = TORRENT_CACHE_DAYS) -> None:
        """Forget file lists not used for `days` (models no longer on the list, old revisions)."""
        folder = self.cache_dir / "hf-trees"
        if not folder.exists():
            return
        cutoff = time.time() - days * 86400
        for f in (*folder.glob("*.json"), *folder.glob("*.gone")):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


def license_from_card(doc: dict) -> str | None:
    """A repo's license id from Hugging Face's model info, lower-cased.

    Hugging Face files custom licenses as "other" and puts the real one in license_name
    (e.g. "openmdw-1.1", "glm-5.3"), so read that whenever the license says "other".
    """
    card = doc.get("cardData") if isinstance(doc.get("cardData"), dict) else {}
    lic = card.get("license") if isinstance(card.get("license"), str) else None
    if lic is None:
        tags = doc.get("tags")
        for t in tags if isinstance(tags, list) else []:
            if isinstance(t, str) and t.startswith("license:"):
                lic = t.split(":", 1)[1]
                break
    if lic and lic.lower() == "other" and isinstance(card.get("license_name"), str) and card["license_name"].strip():
        lic = card["license_name"].strip()
    return lic.lower() if lic else None


def git_blob_sha1(path: Path, size: int) -> str:
    h = hashlib.sha1(b"blob %d\0" % size)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
