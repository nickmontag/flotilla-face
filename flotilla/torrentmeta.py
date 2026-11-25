"""Minimal, strict .torrent reader.

We parse .torrent files ourselves (instead of only trusting libtorrent) so we
can (1) recompute the infohash from the exact bytes of the info dict and pin it
to what the signed feed / Pirate Face listing says, and (2) refuse any torrent
whose file list is not a clean subset of the Hugging Face repo at the pinned
revision. Works without libtorrent, which keeps `flotilla plan` light.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field


class TorrentError(ValueError):
    pass


# ---------------------------------------------------------------- bencode


# Canonical bencode only: int() would also take " 5", "1_0", "+5", "-0" and leading zeros.
_INT = re.compile(rb"-?(?:0|[1-9][0-9]{0,18})")
_LEN = re.compile(rb"0|[1-9][0-9]{0,18}")
# Every list, dict, integer and string counts as one element. A real torrent of a model has a few
# elements per file in its v1 file list, and in a hybrid also a file-tree node per path component,
# a pieces-root and a piece-layers entry: about 25 per file, plus pad files. The piece hashes are one
# string each (v1) or one per file (v2), however big the model. 500,000 elements is room for some
# 20,000 files; Hugging Face repos with weights rarely have more than a few hundred. Without a cap
# a 64 MB file of "le" pairs decodes to 32 million Python objects, some 2 GB.
MAX_ELEMENTS = 500_000


class _Budget:
    __slots__ = ("left",)

    def __init__(self, n: int):
        self.left = n

    def take(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise TorrentError(f"bencode has more than {MAX_ELEMENTS} elements")


def _decode(buf: bytes, i: int, depth: int, spans: dict | None, budget: _Budget):
    if depth > 64:
        raise TorrentError("bencode nesting too deep")
    if i >= len(buf):
        raise TorrentError("truncated bencode")
    budget.take()
    c = buf[i : i + 1]
    if c == b"i":
        end = buf.index(b"e", i)
        raw = buf[i + 1 : end]
        if not _INT.fullmatch(raw) or raw == b"-0":
            raise TorrentError("bad integer")
        return int(raw), end + 1
    if c == b"l":
        i += 1
        out = []
        while buf[i : i + 1] != b"e":
            v, i = _decode(buf, i, depth + 1, None, budget)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while buf[i : i + 1] != b"e":
            k, i = _decode(buf, i, depth + 1, None, budget)
            if not isinstance(k, bytes):
                raise TorrentError("dict key must be a string")
            if k in out:
                raise TorrentError("duplicate dict key")
            start = i
            v, i = _decode(buf, i, depth + 1, None, budget)
            if spans is not None and depth == 0:
                spans[k] = (start, i)
            out[k] = v
        return out, i + 1
    if c.isdigit():
        colon = buf.index(b":", i)
        if not _LEN.fullmatch(buf[i:colon]):
            raise TorrentError("bad string length")
        n = int(buf[i:colon])
        start = colon + 1
        if start + n > len(buf):
            raise TorrentError("truncated string")
        return buf[start : start + n], start + n
    raise TorrentError(f"bad bencode byte {c!r} at {i}")


def _decode_top(buf: bytes, spans: dict | None):
    if not isinstance(buf, (bytes, bytearray)):
        raise TorrentError("not bencode bytes")
    try:
        return _decode(bytes(buf), 0, 0, spans, _Budget(MAX_ELEMENTS))
    except TorrentError:
        raise
    except (ValueError, IndexError, RecursionError) as e:
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


# libtorrent truncates a longer path component (to 240 bytes, dropping the extension), so two
# names that differ only past it would land in one file.
MAX_COMPONENT_BYTES = 240


def _safe_component(c) -> str:
    if not isinstance(c, bytes):
        raise TorrentError("path component is not a string")
    try:
        s = c.decode("utf-8")
    except UnicodeDecodeError as e:
        raise TorrentError("non-UTF-8 path") from e
    if s in ("", ".", "..") or "/" in s or "\\" in s or ":" in s or len(c) > MAX_COMPONENT_BYTES:
        raise TorrentError(f"unsafe path component {s!r}")
    # Control and format characters (NUL, C1 controls, zero-width and bidi marks): libtorrent
    # replaces or strips them, so the file it writes wouldn't be the one we checked.
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in s):
        raise TorrentError(f"path component {s!r} has control or format characters")
    return s


def _fold(path: str) -> str:
    """A path as a case-insensitive, normalising file system (macOS, Windows) sees it."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFD", path).casefold())


def _no_collisions(files: list[TFile]) -> None:
    """Refuse two files that would be one on disk, and a file that is also another's folder.

    libtorrent renames the second copy (model.1.safetensors) and verification would read the first
    file for both, so an unchecked file could be seeded as verified."""
    seen: set[str] = set()
    folders: set[str] = set()
    for f in files:
        if f.pad:
            continue   # libtorrent never writes pad files, and their names repeat (.pad/16384)
        key = _fold(f.repo_path)
        if key in seen:
            raise TorrentError(f"two files at {f.repo_path!r} (paths must differ by more than case or Unicode form)")
        seen.add(key)
        parts = key.split("/")
        folders.update("/".join(parts[:i]) for i in range(1, len(parts)))
    clash = seen & folders
    if clash:
        raise TorrentError(f"{sorted(clash)[0]!r} is both a file and a folder")


def _str(v, what: str) -> str:
    if not isinstance(v, bytes):
        raise TorrentError(f"{what} is not a string")
    try:
        return v.decode("utf-8")
    except UnicodeDecodeError as e:
        raise TorrentError(f"{what} is not UTF-8") from e


def _int(v, what: str) -> int:
    if not isinstance(v, int) or v < 0:
        raise TorrentError(f"bad {what}")
    return v


def _same_utf8(d: dict, key: bytes) -> None:
    """libtorrent prefers "name.utf-8" / "path.utf-8" when present: they must say the same."""
    alt = key + b".utf-8"
    if alt in d and d[alt] != d.get(key):
        raise TorrentError(f"{alt.decode()} differs from {key.decode()}")


def parse_torrent(raw: bytes) -> TorrentMeta:
    """Parse and check a .torrent. Anything wrong with it, of any shape, is a TorrentError."""
    spans: dict = {}
    top, end = _decode_top(raw, spans)
    if end != len(raw) or not isinstance(top, dict) or not isinstance(top.get(b"info"), dict):
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
    if not isinstance(pieces, bytes) or len(pieces) % 20:
        raise TorrentError("bad pieces field")
    _same_utf8(info, b"name")
    name = _safe_component(info.get(b"name", b""))
    hybrid = info.get(b"meta version") == 2

    files: list[TFile] = []
    offset = 0
    if b"files" in info:
        flist = info[b"files"]
        if not isinstance(flist, list) or not flist:
            raise TorrentError("bad file list")
        for idx, f in enumerate(flist):
            if not isinstance(f, dict):
                raise TorrentError("bad file entry")
            _same_utf8(f, b"path")
            path_list = f.get(b"path")
            if not isinstance(path_list, list) or not path_list:
                raise TorrentError("file with empty path")
            comps = [_safe_component(c) for c in path_list]
            size = _int(f.get(b"length"), "file length")
            attr = f.get(b"attr", b"")
            if not isinstance(attr, bytes):
                raise TorrentError("bad file attributes")
            if b"l" in attr or b"symlink path" in f:
                raise TorrentError("symlinks are not allowed")   # libtorrent would create them (BEP 47)
            pad = b"p" in attr
            path = "/".join([name] + comps)
            files.append(TFile(idx, path, "/".join(comps), size, offset, pad))
            offset += size
        _no_collisions(files)
    else:
        size = _int(info.get(b"length"), "length")
        attr = info.get(b"attr", b"")
        if not isinstance(attr, bytes) or b"l" in attr or b"symlink path" in info:
            raise TorrentError("symlinks are not allowed")
        files.append(TFile(0, name, name, size, 0))
        offset = size

    num_pieces = len(pieces) // 20
    if (offset + piece_length - 1) // piece_length != num_pieces:
        raise TorrentError("piece count does not match total size")

    # Strict about shape: libtorrent skips what it can't read, and a tracker we skipped but it
    # used would get around the allowlist.
    trackers: list[str] = []
    if b"announce" in top:
        trackers.append(_str(top[b"announce"], "announce"))
    tiers = top.get(b"announce-list", [])
    if not isinstance(tiers, list):
        raise TorrentError("bad announce-list")
    for tier in tiers:
        if not isinstance(tier, list):
            raise TorrentError("bad announce-list tier")
        for t in tier:
            t = _str(t, "tracker")
            if t not in trackers:
                trackers.append(t)
    url_list = top.get(b"url-list", [])
    if isinstance(url_list, bytes):
        url_list = [url_list]
    if not isinstance(url_list, list):
        raise TorrentError("bad url-list")
    web_seeds = [u for u in (_str(x, "web seed") for x in url_list) if u]
    if top.get(b"httpseeds"):
        raise TorrentError("BEP 17 httpseeds are not allowed")
    trackers = [t for t in trackers if t]
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
