"""Minimal, strict .torrent reader.

We parse .torrent files ourselves (instead of only trusting libtorrent) so we
can (1) recompute the infohash from the exact bytes of the info dict and pin it
to what the signed feed / Pirate Face listing says, and (2) refuse any torrent
whose file list is not a clean subset of the Hugging Face repo at the pinned
revision. Works without libtorrent, which keeps `flotilla plan` light.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


class TorrentError(ValueError):
    pass


# ---------------------------------------------------------------- bencode


def _decode(buf: bytes, i: int, depth: int, spans: dict | None):
    if depth > 64:
        raise TorrentError("bencode nesting too deep")
    if i >= len(buf):
        raise TorrentError("truncated bencode")
    c = buf[i : i + 1]
    if c == b"i":
        end = buf.index(b"e", i)
        raw = buf[i + 1 : end]
        if raw in (b"", b"-") or raw.startswith(b"-0") or (raw.startswith(b"0") and raw != b"0"):
            raise TorrentError("bad integer")
        return int(raw), end + 1
    if c == b"l":
        i += 1
        out = []
        while buf[i : i + 1] != b"e":
            v, i = _decode(buf, i, depth + 1, None)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while buf[i : i + 1] != b"e":
            k, i = _decode(buf, i, depth + 1, None)
            if not isinstance(k, bytes):
                raise TorrentError("dict key must be a string")
            if k in out:
                raise TorrentError("duplicate dict key")
            start = i
            v, i = _decode(buf, i, depth + 1, None)
            if spans is not None and depth == 0:
                spans[k] = (start, i)
            out[k] = v
        return out, i + 1
    if c.isdigit():
        colon = buf.index(b":", i)
        n = int(buf[i:colon])
        start = colon + 1
        if start + n > len(buf):
            raise TorrentError("truncated string")
        return buf[start : start + n], start + n
    raise TorrentError(f"bad bencode byte {c!r} at {i}")


def _decode_top(buf: bytes, spans: dict | None):
    try:
        return _decode(buf, 0, 0, spans)
    except TorrentError:
        raise
    except (ValueError, IndexError) as e:
        raise TorrentError(f"malformed bencode: {e}") from e


def bdecode(buf: bytes):
    v, end = _decode_top(buf, None)
    if end != len(buf):
        raise TorrentError("trailing data after bencode")
    return v


def bencode(v) -> bytes:
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(bencode(x) for x in v) + b"e"
    if isinstance(v, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, x) for k, x in v.items())
        return b"d" + b"".join(bencode(k) + bencode(x) for k, x in items) + b"e"
    raise TypeError(type(v))


# ---------------------------------------------------------------- model


@dataclass
class TFile:
    index: int          # index in the torrent's file list (libtorrent uses the same order)
    path: str           # path inside the torrent, '/'-joined, including the root folder
    repo_path: str      # path inside the HF repo (root folder stripped)
    size: int
    offset: int
    pad: bool = False

    def piece_range(self, piece_length: int) -> tuple[int, int]:
        """Inclusive [first, last] piece indices this file touches (size>0)."""
        first = self.offset // piece_length
        last = (self.offset + max(self.size, 1) - 1) // piece_length
        return first, last


@dataclass
class TorrentMeta:
    infohash_v1: str
    name: str
    piece_length: int
    num_pieces: int
    files: list[TFile]
    hybrid: bool
    raw: bytes = field(repr=False)
    trackers: list[str] = field(default_factory=list)
    web_seeds: list[str] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files if not f.pad)

    def real_files(self) -> list[TFile]:
        return [f for f in self.files if not f.pad]


def _safe_component(c: bytes) -> str:
    try:
        s = c.decode("utf-8")
    except UnicodeDecodeError as e:
        raise TorrentError("non-UTF-8 path") from e
    if s in ("", ".", "..") or "/" in s or "\\" in s or "\x00" in s or ":" in s:
        raise TorrentError(f"unsafe path component {s!r}")
    return s


def parse_torrent(raw: bytes) -> TorrentMeta:
    spans: dict = {}
    top, end = _decode_top(raw, spans)
    if end != len(raw) or not isinstance(top, dict) or b"info" not in top:
        raise TorrentError("not a torrent file")
    s, e = spans[b"info"]
    info_bytes = raw[s:e]
    infohash = hashlib.sha1(info_bytes).hexdigest()
    info = top[b"info"]

    if b"pieces" not in info:
        raise TorrentError("pure BitTorrent v2 torrents are not supported yet (no v1 piece layer)")
    piece_length = info.get(b"piece length")
    if not isinstance(piece_length, int) or piece_length <= 0:
        raise TorrentError("bad piece length")
    pieces = info[b"pieces"]
    if len(pieces) % 20:
        raise TorrentError("bad pieces field")
    name = _safe_component(info.get(b"name", b""))
    hybrid = info.get(b"meta version") == 2

    files: list[TFile] = []
    offset = 0
    if b"files" in info:
        for idx, f in enumerate(info[b"files"]):
            comps = [_safe_component(c) for c in f.get(b"path", [])]
            if not comps:
                raise TorrentError("file with empty path")
            size = f.get(b"length")
            if not isinstance(size, int) or size < 0:
                raise TorrentError("bad file length")
            pad = b"p" in f.get(b"attr", b"")
            path = "/".join([name] + comps)
            files.append(TFile(idx, path, "/".join(comps), size, offset, pad))
            offset += size
    else:
        size = info.get(b"length")
        if not isinstance(size, int) or size < 0:
            raise TorrentError("bad length")
        files.append(TFile(0, name, name, size, 0))
        offset = size

    num_pieces = len(pieces) // 20
    if (offset + piece_length - 1) // piece_length != num_pieces:
        raise TorrentError("piece count does not match total size")

    def _s(v) -> str | None:
        return v.decode("utf-8", "replace") if isinstance(v, bytes) else None

    trackers: list[str] = []
    if _s(top.get(b"announce")):
        trackers.append(_s(top[b"announce"]))
    for tier in top.get(b"announce-list", []) or []:
        for t in tier if isinstance(tier, list) else []:
            if _s(t) and _s(t) not in trackers:
                trackers.append(_s(t))
    url_list = top.get(b"url-list", [])
    if isinstance(url_list, bytes):
        url_list = [url_list]
    web_seeds = [_s(u) for u in url_list if _s(u)]
    if top.get(b"httpseeds"):
        raise TorrentError("BEP 17 httpseeds are not allowed")
    return TorrentMeta(infohash, name, piece_length, num_pieces, files, hybrid, raw, trackers, web_seeds)


def make_torrent(files: list[tuple[str, bytes]], name: str, piece_length: int = 16384) -> bytes:
    """Build a small v1 multi-file torrent in memory (used by tests)."""
    blob = b"".join(d for _, d in files)
    pieces = b"".join(
        hashlib.sha1(blob[i : i + piece_length]).digest() for i in range(0, len(blob), piece_length)
    )
    info = {
        "name": name,
        "piece length": piece_length,
        "pieces": pieces,
        "files": [{"length": len(d), "path": p.split("/")} for p, d in files],
    }
    return bencode({"info": info})
