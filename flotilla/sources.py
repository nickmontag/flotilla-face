"""Read-only clients for Pirate Face and Hugging Face.

Nothing here downloads model weights. These calls fetch listings, .torrent
metadata and hash manifests, all through netsafe.fetch.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .netsafe import FetchError, fetch, fetch_json, parse_link_next

PF_BASE = "https://pirateface.co"
HF_BASE = "https://huggingface.co"
MAX_TORRENT_BYTES = 64 << 20


TORRENT_CACHE_DAYS = 30
SERVER_ERRORS_BEFORE_BACKOFF = 3


class PirateFace:
    """Pirate Face's API, used gently: thousands of nodes may run this.

    * .torrent files are cached by infohash (an infohash pins the content, so a
      cached file never goes stale), and a cached file is re-verified before use.
    * After a few server errors in a row (5xx, 429, timeouts) the client stops
      calling Pirate Face for the rest of this refresh instead of retrying.
    """

    def __init__(self, allowed: tuple[str, ...], base: str = PF_BASE, cache_dir: Path | None = None):
        self.base = base.rstrip("/")
        self.allowed = allowed
        self.cache_dir = cache_dir
        self._server_errors = 0

    def _guard(self) -> None:
        if self._server_errors >= SERVER_ERRORS_BEFORE_BACKOFF:
            raise FetchError(self.base, None, "Pirate Face is having trouble; no more requests until the next refresh")

    def _note(self, e: FetchError) -> None:
        if e.status is None or e.status == 429 or e.status >= 500:
            self._server_errors += 1

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

    def licenses(self) -> list[dict]:
        return self._get("/api/licenses").get("licenses", [])

    def trending(self, pages: int = 2) -> list[dict]:
        """Catalog entries (repo, license, size, seed/leech counts, hfBacked)."""
        out, cursor = [], None
        for _ in range(pages):
            doc = self._get("/api/models", cursor=cursor)
            out.extend(doc.get("models", []))
            cursor = doc.get("nextCursor")
            if not cursor:
                break
        return out

    def rescue(self) -> dict:
        return self._get("/api/rescue")

    def torrent(self, repo: str, quant: str | None = None) -> dict | None:
        try:
            return self._get("/api/torrents", repo=repo, quant=quant)
        except FetchError as e:
            if e.status in (400, 404):
                return None
            raise

    def torrent_file(self, infohash: str) -> bytes:
        if len(infohash) != 40 or any(c not in "0123456789abcdef" for c in infohash):
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
    so a cached tree never goes stale.
    """

    def __init__(self, allowed: tuple[str, ...], cache_dir: Path, base: str = HF_BASE):
        self.base = base.rstrip("/")
        self.allowed = allowed
        self.cache_dir = cache_dir

    LICENSE_TTL = 7 * 86400

    def license(self, repo: str) -> str | None:
        """A repo's license, cached for a week (it rarely changes)."""
        cache = self.cache_dir / "hf-licenses.json"
        try:
            known = json.loads(cache.read_text()) if cache.exists() else {}
        except (OSError, ValueError):
            known = {}
        hit = known.get(repo)
        if (isinstance(hit, list) and len(hit) == 2 and time.time() - hit[1] < self.LICENSE_TTL and hit[0]
                and hit[0] != "other"):   # "other" was cached before license names were read: look again
            return hit[0]
        try:
            doc = fetch_json(f"{self.base}/api/models/{repo}", allowed=self.allowed, max_bytes=4 << 20)
        except FetchError:
            return None
        lic = license_from_card(doc)
        if lic:
            known[repo] = [lic, time.time()]
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(known))
            except OSError:
                pass
        return lic

    def tree(self, repo: str, revision: str) -> dict[str, HFFile] | None:
        """Return {path: HFFile}, or None if the repo/revision is not readable on HF.

        Only trees at a full commit hash are cached: a commit is immutable, a
        branch name like "main" is not.
        """
        immutable = len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)
        cache = self.cache_dir / "hf-trees" / f"{repo.replace('/', '__')}@{revision}.json"
        if immutable and cache.exists():
            try:
                raw = json.loads(cache.read_text())
                # Trees cached before masked hashes were recognised may hold '*' strings; refetch those.
                if not any(isinstance(v.get("sha256"), str) and not _hex(v["sha256"], 64) for v in raw.values()):
                    return {p: HFFile(**v) for p, v in raw.items()}
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        url = f"{self.base}/api/models/{repo}/tree/{revision}?recursive=true"
        entries: list[dict] = []
        try:
            for _ in range(200):  # pagination guard
                resp = fetch(url, max_bytes=32 << 20, allowed=self.allowed, accept="application/json")
                page = resp.json()
                if not isinstance(page, list):
                    return None
                entries.extend(page)
                nxt = parse_link_next(resp.headers.get("link"))
                if not nxt:
                    break
                url = nxt
        except FetchError as e:
            if e.status in (401, 403, 404, 410, 451):
                return None
            raise
        out = {}
        for e in entries:
            if e.get("type") != "file":
                continue
            lfs = e.get("lfs") or {}
            size = int(e.get("size", 0))
            if lfs:
                out[e["path"]] = HFFile(size, _hex(lfs.get("oid"), 64), None, _hex(e.get("oid"), 40))
            else:
                out[e["path"]] = HFFile(size, None, _hex(e.get("oid"), 40))
        if immutable:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({p: vars(v) for p, v in out.items()}))
        return out


def license_from_card(doc: dict) -> str | None:
    """A repo's license id from Hugging Face's model info, lower-cased.

    Hugging Face files custom licenses as "other" and puts the real one in license_name
    (e.g. "openmdw-1.1", "glm-5.3"), so read that whenever the license says "other".
    """
    card = doc.get("cardData") or {}
    lic = card.get("license") if isinstance(card.get("license"), str) else None
    if lic is None:
        for t in doc.get("tags", []):
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
