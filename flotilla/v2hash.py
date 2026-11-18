"""BitTorrent v2 (BEP 52) hash trees: a file's piece layer and pieces root.

Every file in a v2 torrent has a Merkle tree over SHA-256 hashes of its 16 KiB blocks. Its root
(the "pieces root") names the file's bytes whatever torrent carries them, and the layer of the
tree one hash per piece (the "piece layer") is what a peer needs to check each piece it gets.
Pirate Face's .torrent files leave the piece layers out, so Flotilla supplies them: from the
hash tree libtorrent saved while it downloaded the file, or by hashing the file on disk. Either
way the layer is accepted only if it hashes up to the pieces root the torrent names.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

BLOCK = 16 * 1024
ZERO = bytes(32)
# An empty saved tree. libtorrent stops reading the saved trees at the first entry without
# "hashes", so a bare {} there throws away the trees of every file after it.
NO_TREE = {b"hashes": b""}


def _pow2(n: int) -> int:
    return 1 << max(0, (n - 1).bit_length())


def fold(hashes: list[bytes], width: int, pad: bytes = ZERO) -> bytes:
    """The root of a Merkle tree over `hashes`, padded with `pad` to `width` leaves (a power of two)."""
    layer = list(hashes) + [pad] * (width - len(hashes))
    while len(layer) > 1:
        layer = [hashlib.sha256(layer[i] + layer[i + 1]).digest() for i in range(0, len(layer), 2)]
    return layer[0] if layer else pad


def pad_hash(piece_length: int) -> bytes:
    """The hash of a piece past the end of a file: a tree of zero leaves, one per block."""
    return fold([], piece_length // BLOCK)


def root_from_layer(layer: bytes, piece_length: int) -> bytes:
    """The pieces root that a piece layer (32 bytes per piece) hashes up to."""
    pieces = [layer[i:i + 32] for i in range(0, len(layer), 32)]
    return fold(pieces, _pow2(len(pieces)), pad_hash(piece_length))


def needs_layer(size: int, piece_length: int) -> bool:
    """A file of one piece or less has no piece layer: its root is its only piece hash."""
    return size > piece_length


def hash_file(path: Path, size: int, piece_length: int, stop: threading.Event | None = None) -> tuple[bytes, bytes]:
    """(pieces root, piece layer) of the file at `path`, read from disk. The layer is b"" for a
    file of one piece or less. Raises OSError if the file is short or `stop` is set meanwhile."""
    per_piece = piece_length // BLOCK
    pieces: list[bytes] = []
    leaves: list[bytes] = []
    buf = bytearray(piece_length)
    view = memoryview(buf)
    left = size
    with open(path, "rb") as f:
        while left > 0:
            if stop is not None and stop.is_set():
                raise OSError("stopped")
            want = min(piece_length, left)
            got = f.readinto(view[:want])
            if got != want:
                raise OSError(f"{path} is shorter than {size} bytes")
            leaves = [hashlib.sha256(view[i:min(i + BLOCK, want)]).digest() for i in range(0, want, BLOCK)]
            if needs_layer(size, piece_length):
                pieces.append(fold(leaves, per_piece))
            left -= want
    if not needs_layer(size, piece_length):
        return fold(leaves, _pow2(len(leaves))), b""
    layer = b"".join(pieces)
    return root_from_layer(layer, piece_length), layer


# ------------------------------------------------------------------ libtorrent's saved trees


def bits_set(mask: bytes, lo: int, hi: int) -> int:
    """Set bits in [lo, hi) of a resume-data bitfield (bit 0 is the top bit of byte 0)."""
    total = len(mask) * 8
    hi = min(hi, total)
    if hi <= lo:
        return 0
    return ((int.from_bytes(mask, "big") >> (total - hi)) & ((1 << (hi - lo)) - 1)).bit_count()


def _layer_span(size: int, piece_length: int) -> tuple[int, int]:
    pieces = -(-size // piece_length)
    start = (1 << (pieces - 1).bit_length()) - 1      # first node of the piece layer
    return start, start + pieces


def _stored(tree: dict, start: int, end: int) -> bytes | None:
    """Nodes [start, end) of a saved tree (masked, or full: every node in order), if all are there."""
    hashes = tree.get(b"hashes") or b""
    mask = tree.get(b"mask")
    if mask:
        if bits_set(mask, start, end) != end - start:
            return None
        first = bits_set(mask, 0, start)               # hashes are stored for set bits only
    else:
        first = start
    nodes = hashes[first * 32:(first + end - start) * 32]
    return nodes if len(nodes) == (end - start) * 32 else None


def saved_piece_layer(tree: dict, size: int, piece_length: int) -> bytes | None:
    """The piece layer as one of libtorrent's saved hash trees holds it (what peers send), or None."""
    if not needs_layer(size, piece_length) or piece_length <= BLOCK or not isinstance(tree, dict):
        return None
    return _stored(tree, *_layer_span(size, piece_length))


def saved_layer(tree: dict, size: int, piece_length: int, saved_piece_length: int | None = None) -> bytes | None:
    """The piece layer for `piece_length` from a saved hash tree (of a torrent whose pieces are
    `saved_piece_length`, the same by default): held as such, or worked out from the block hashes
    libtorrent keeps for a file it downloaded or checked. Block hashes don't depend on the piece
    length, so they serve a torrent with other pieces too. None if the tree holds neither whole."""
    if not needs_layer(size, piece_length) or not isinstance(tree, dict):
        return None
    if saved_piece_length in (None, piece_length):
        layer = saved_piece_layer(tree, size, piece_length)
        if layer is not None:
            return layer
    blocks = -(-size // BLOCK)
    start = _pow2(blocks) - 1                          # first leaf
    leaves = _stored(tree, start, start + blocks)
    if leaves is None:
        return None
    per_piece = piece_length // BLOCK
    hashes = [leaves[i:i + 32] for i in range(0, len(leaves), 32)]
    return b"".join(fold(hashes[i:i + per_piece], per_piece) for i in range(0, blocks, per_piece))


def piece_layer_tree(tree: dict, size: int, piece_length: int) -> dict:
    """A saved v2 hash tree cut down to its piece layer, or NO_TREE if it doesn't hold all of it.

    libtorrent loads a tree holding exactly the piece layer at 32 bytes a piece (2 KB per GB with
    16 MB pieces), checked against the file's root hash; any other saved tree is rebuilt at full
    size, 4-8 MB per GB."""
    layer = saved_piece_layer(tree, size, piece_length)
    if layer is None:
        return dict(NO_TREE)
    start, end = _layer_span(size, piece_length)
    new_mask = bytearray((end + 7) // 8)
    for i in range(start, end):
        new_mask[i >> 3] |= 0x80 >> (i & 7)
    return {b"hashes": layer, b"mask": bytes(new_mask)}


def layer_from_resume(resume: bytes, index: int, size: int, piece_length: int, root: bytes,
                      saved_piece_length: int | None = None) -> bytes | None:
    """File `index`'s piece layer for `piece_length` from a torrent's saved resume data (that torrent's
    pieces are `saved_piece_length`), if it holds what's needed and the layer hashes up to `root`.
    None otherwise (hash the file instead)."""
    import libtorrent as lt

    try:
        d = lt.bdecode(resume)
    except RuntimeError:
        return None
    if not isinstance(d, dict) or d.get(b"file-version") != 2:
        return None
    trees = d.get(b"trees")
    if not isinstance(trees, list) or index >= len(trees):
        return None
    layer = saved_layer(trees[index], size, piece_length, saved_piece_length)
    if layer is None or root_from_layer(layer, piece_length) != root:
        return None
    return layer
