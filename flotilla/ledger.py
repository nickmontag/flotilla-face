"""The hash ledger: a signed, append-only record of every file's hash, per model and revision.

Hugging Face is the authority on a model's hashes while it hosts the model. Once it doesn't,
Pirate Face's manifest used to be the only source, so a compromised Pirate Face could swap a
model's bytes and its hashes together. The ledger closes that gap: editors capture each
model's hashes from Hugging Face while it still serves them, and every install carries them.

Layout (flotilla/data/ledger/, published in the repo and shipped in the package):

    index.json             {"kind", "schema", "version", "issued", "entries": {"repo@rev": {"file", "sha256"}}}
    index.json.minisig     the editor signature over index.json (same keys as the priority list)
    entries/<a>__<m>@<rev>.json        one model at one revision: path, size and hash of every file
    entries/<a>__<m>@<rev>.json.ots    OpenTimestamps proof that the entry existed by a Bitcoin block

An entry file is written once and never edited, so its timestamp keeps its original date. The
index commits to each entry's SHA-256, so the one signature covers every entry.

Append-only on every node: the first digest a node accepts for a repo@revision is remembered in
ledger-seen.json. A later index that lists a different digest for it is a conflict: logged
loudly, and the remembered entry stays. A stolen editor key therefore can't rewrite the hashes
of a model nodes already know; it can only add models. `flotilla ledger --accept` overrides one
entry on purpose. Entries an index stops listing are kept from the local cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .feed import HEX40, REPO_RE
from .netsafe import FetchError, UnsafeURL, fetch
from .signing import PublicKey, SignatureError, verify

KIND = "flotilla-hash-ledger"
ENTRY_KIND = "flotilla-hash-ledger-entry"
SCHEMA = 1
LEDGER_DIR = Path(__file__).parent / "data" / "ledger"
BUNDLED_LEDGER = LEDGER_DIR / "index.json"
# Updates between releases, fetched like the priority list. The signature, not the host, makes it trusted.
DEFAULT_LEDGER_URL = "https://raw.githubusercontent.com/nickmontag/flotilla-face/main/flotilla/data/ledger/index.json"
MAX_INDEX_BYTES = 8 << 20
MAX_ENTRY_BYTES = 16 << 20
MAX_SIG_BYTES = 4096
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ENTRY_FILE_RE = re.compile(r"^entries/[A-Za-z0-9._@-]+\.json$")
SOURCES = ("huggingface", "pirateface")
SEEN_FILE = "ledger-seen.json"


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class LedgerFile:
    path: str
    size: int
    sha256: str | None = None
    git_oid: str | None = None          # small (non-LFS) files: git blob SHA-1
    lfs_pointer_oid: str | None = None  # Hugging Face hid the SHA-256 (gated repo); the pointer commits to it


@dataclass
class Entry:
    repo: str
    revision: str
    captured: str
    source: str              # where the hashes came from: "huggingface" or "pirateface"
    files: dict[str, LedgerFile]
    digest: str              # SHA-256 of the entry file's exact bytes
    file: str                # e.g. entries/author__model@<rev>.json
    stamp: Path | None = None   # a local OpenTimestamps proof, when there is one

    @property
    def key(self) -> str:
        return ledger_key(self.repo, self.revision)


@dataclass
class Ledger:
    entries: dict[str, Entry] = field(default_factory=dict)   # ledger_key -> Entry
    conflicts: list[dict] = field(default_factory=list)       # {"key", "kept", "refused", "source"}
    sources: list[str] = field(default_factory=list)
    version: int = 0

    def get(self, repo: str, revision: str) -> Entry | None:
        return self.entries.get(ledger_key(repo, revision))

    def revisions(self, repo: str) -> list[str]:
        r = repo.lower()
        return sorted(e.revision for e in self.entries.values() if e.repo.lower() == r)


def ledger_key(repo: str, revision: str) -> str:
    return f"{repo.lower()}@{revision.lower()}"


def entry_name(repo: str, revision: str) -> str:
    return f"entries/{repo.replace('/', '__')}@{revision}.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- parsing


def _safe_path(p) -> bool:
    if not isinstance(p, str) or not p or len(p) > 1024 or p.startswith("/") or "\\" in p or "\x00" in p:
        return False
    return all(c not in ("", ".", "..") for c in p.split("/"))


def _hex(v, n: int) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or len(v) != n or any(c not in "0123456789abcdef" for c in v):
        raise LedgerError(f"bad hash {v!r}")
    return v


def parse_entry(data: bytes, file: str = "", dig: str | None = None) -> Entry:
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as e:
        raise LedgerError(f"entry is not JSON: {e}") from e
    if not isinstance(doc, dict) or doc.get("kind") != ENTRY_KIND or doc.get("schema") != SCHEMA:
        raise LedgerError("not a hash-ledger entry (or an unsupported schema)")
    repo, rev = doc.get("repo"), doc.get("revision")
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        raise LedgerError("entry repo is not author/model")
    if not isinstance(rev, str) or not HEX40.match(rev):
        raise LedgerError("entry revision must be a 40-hex commit")
    if doc.get("source") not in SOURCES:
        raise LedgerError(f"entry source must be one of {', '.join(SOURCES)}")
    if not isinstance(doc.get("captured"), str):
        raise LedgerError("entry needs a captured time")
    items = doc.get("files")
    if not isinstance(items, list) or not items:
        raise LedgerError("entry has no files")
    files: dict[str, LedgerFile] = {}
    for i, f in enumerate(items):
        if not isinstance(f, dict) or not _safe_path(f.get("path")):
            raise LedgerError(f"files[{i}] has an unsafe or missing path")
        size = f.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise LedgerError(f"files[{i}].size must be a non-negative integer")
        lf = LedgerFile(f["path"], size, _hex(f.get("sha256"), 64), _hex(f.get("git_oid"), 40),
                        _hex(f.get("lfs_pointer_oid"), 40))
        if not (lf.sha256 or lf.git_oid or lf.lfs_pointer_oid):
            raise LedgerError(f"files[{i}] ({lf.path}) has no hash")
        if lf.path in files:
            raise LedgerError(f"{lf.path} is listed twice")
        files[lf.path] = lf
    return Entry(repo, rev, doc["captured"], doc["source"], files, dig or digest(data), file)


def parse_index(data: bytes) -> tuple[int, dict[str, tuple[str, str]]]:
    """(version, {ledger_key: (entry file, sha256)})."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as e:
        raise LedgerError(f"ledger index is not JSON: {e}") from e
    if not isinstance(doc, dict) or doc.get("kind") != KIND or doc.get("schema") != SCHEMA:
        raise LedgerError("not a hash-ledger index (or an unsupported schema)")
    version = doc.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise LedgerError("ledger version must be a positive integer")
    raw = doc.get("entries")
    if not isinstance(raw, dict):
        raise LedgerError("ledger entries must be an object")
    out: dict[str, tuple[str, str]] = {}
    for key, v in raw.items():
        repo, _, rev = key.rpartition("@") if isinstance(key, str) else ("", "", "")
        if not REPO_RE.match(repo) or not HEX40.match(rev):
            raise LedgerError(f"ledger key {key!r} is not author/model@<40-hex commit>")
        if not isinstance(v, dict) or not isinstance(v.get("file"), str) or not ENTRY_FILE_RE.match(v["file"]) \
                or ".." in v["file"] or not isinstance(v.get("sha256"), str) or not HEX64.match(v["sha256"]):
            raise LedgerError(f"ledger entry {key!r} needs a file under entries/ and a sha256")
        out[ledger_key(repo, rev)] = (v["file"], v["sha256"])
    return version, out


# ---------------------------------------------------------------- loading (nodes)


def _read_seen(path: Path) -> dict[str, str]:
    try:
        doc = json.loads(path.read_text())
        return {k: v for k, v in doc.items() if isinstance(k, str) and isinstance(v, str) and HEX64.match(v)}
    except (OSError, ValueError, AttributeError):
        return {}


def _write_seen(path: Path, seen: dict[str, str], replace: dict[str, str] | None = None) -> None:
    """Merge into what is on disk (another Flotilla process may have added entries meanwhile)."""
    merged = _read_seen(path)
    for k, v in seen.items():
        merged.setdefault(k, v)
    merged.update(replace or {})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def accept_override(state_dir: Path, key: str, sha256: str) -> None:
    """Deliberately replace the digest this node remembers for one repo@revision."""
    repo, _, rev = key.rpartition("@")
    if not REPO_RE.match(repo) or not HEX40.match(rev.lower()):
        raise LedgerError(f"{key!r} is not author/model@<40-hex commit>")
    if not HEX64.match(sha256.lower()):
        raise LedgerError("the digest must be 64 hex characters (an entry's sha256 from the index)")
    _write_seen(state_dir / SEEN_FILE, {}, replace={ledger_key(repo, rev): sha256.lower()})


def load_ledger(keys: list[PublicKey], state_dir: Path, allowed_hosts: tuple[str, ...] = (),
                url: str | None = DEFAULT_LEDGER_URL, bundled: Path | None = BUNDLED_LEDGER, log=print) -> Ledger:
    """Every copy that verifies adds to the ledger: the one shipped in the package first, then the
    last fetched copy, then the published one. The first digest seen for a repo@revision wins,
    here and in every later run. Network failures only mean fewer entries, never an error."""
    led = Ledger()
    if not keys:
        log("hash ledger: no trusted editor keys configured; not used")
        return led
    seen_path = state_dir / SEEN_FILE
    seen = _read_seen(seen_path)
    before = dict(seen)
    cache = state_dir / "cache" / "ledger"
    store = cache / "entries"     # entry files by digest: content-addressed, so never stale

    def from_store(sha: str) -> bytes | None:
        p = store / f"{sha}.json"
        try:
            data = p.read_bytes()
        except OSError:
            return None
        if digest(data) == sha:
            return data
        p.unlink(missing_ok=True)
        return None

    def take(name: str, index: dict[str, tuple[str, str]], get_entry, keep: bool, stamps: Path | None) -> int:
        added = 0
        for key, (file, sha) in index.items():
            known = seen.get(key)
            if known is not None and known != sha:
                c = {"key": key, "kept": known, "refused": sha, "source": name}
                if c not in led.conflicts:
                    led.conflicts.append(c)
                    log(f"HASH LEDGER CONFLICT {key}: {name} lists entry {sha[:12]}…, but this node already "
                        f"accepted {known[:12]}…; keeping the first. If the change is genuine: "
                        f"flotilla ledger --accept {key} {sha}")
                continue
            if key in led.entries:
                continue
            data = from_store(sha)
            stored = data is not None
            data = data if stored else get_entry(file)
            if data is None:
                continue
            if digest(data) != sha:
                log(f"hash ledger: {name}: {file} doesn't match its digest in the index; skipped")
                continue
            try:
                e = parse_entry(data, file, sha)
            except LedgerError as err:
                log(f"hash ledger: {name}: {file} rejected: {err}")
                continue
            if e.key != key:
                log(f"hash ledger: {name}: {file} is for {e.key}, not {key}; skipped")
                continue
            if stamps is not None and (stamps / (file + ".ots")).exists():
                e.stamp = stamps / (file + ".ots")
            if keep and not stored:
                try:
                    store.mkdir(parents=True, exist_ok=True)
                    (store / f"{sha}.json").write_bytes(data)
                except OSError:
                    pass
            led.entries[key] = e
            seen[key] = sha
            added += 1
        return added

    def check(data: bytes, sig: str) -> tuple[int, dict]:
        verify(data, sig, keys)
        return parse_index(data)

    # 1. The copy shipped with this release.
    if bundled is not None and bundled.exists():
        try:
            sig_path = Path(str(bundled) + ".minisig")
            if not sig_path.exists():
                log("hash ledger: the copy bundled with this release isn't signed yet; not used")
            else:
                v, index = check(bundled.read_bytes(), sig_path.read_text())
                led.version = max(led.version, v)
                d = bundled.parent

                def bundled_entry(file: str, d=d) -> bytes | None:
                    try:
                        return (d / file).read_bytes()
                    except OSError:
                        return None

                take("bundled ledger", index, bundled_entry, keep=True, stamps=d)
                led.sources.append(f"bundled v{v}")
        except (OSError, SignatureError, LedgerError, UnicodeDecodeError) as e:
            log(f"hash ledger: bundled copy unusable: {e}")

    # 2. The published copy (or, if it can't be fetched, the last one that verified).
    remote: tuple[int, dict, bytes, bytes] | None = None
    if url:
        try:
            data = fetch(url, max_bytes=MAX_INDEX_BYTES, allowed=allowed_hosts, allow_file=True).body
            sig = fetch(url + ".minisig", max_bytes=MAX_SIG_BYTES, allowed=allowed_hosts, allow_file=True).body
            v, index = check(data, sig.decode("utf-8", "replace"))
            remote = (v, index, data, sig)
        except (FetchError, UnsafeURL, SignatureError, LedgerError, UnicodeDecodeError) as e:
            log(f"hash ledger: published copy unavailable ({e}); using local copies")
    if remote is None and (cache / "index.json").exists():
        try:
            data = (cache / "index.json").read_bytes()
            v, index = check(data, (cache / "index.json.minisig").read_text())
            remote = (v, index, data, b"")
        except (OSError, SignatureError, LedgerError, UnicodeDecodeError) as e:
            log(f"hash ledger: cached copy unusable: {e}")
    if remote is not None:
        v, index, data, sig = remote

        def remote_entry(file: str) -> bytes | None:
            if not url:
                return None
            try:
                return fetch(urllib.parse.urljoin(url, file), max_bytes=MAX_ENTRY_BYTES, allowed=allowed_hosts,
                             allow_file=True).body
            except (FetchError, UnsafeURL) as e:
                log(f"hash ledger: could not fetch {file}: {e}")
                return None

        take(f"published ledger v{v}", index, remote_entry, keep=True, stamps=None)
        led.version = max(led.version, v)
        led.sources.append(f"published v{v}" if sig else f"cached v{v}")
        if sig:
            try:
                cache.mkdir(parents=True, exist_ok=True)
                (cache / "index.json").write_bytes(data)
                (cache / "index.json.minisig").write_bytes(sig)
            except OSError:
                pass

    # 3. Entries this node accepted before that no index lists any more: keep them.
    for key, sha in seen.items():
        if key not in led.entries:
            data = from_store(sha)
            if data is not None:
                try:
                    led.entries[key] = parse_entry(data, "", sha)
                except LedgerError:
                    pass

    if seen != before:
        try:
            _write_seen(seen_path, {k: v for k, v in seen.items() if k not in before})
        except OSError as e:
            log(f"hash ledger: could not record accepted entries: {e}")
    return led


# ---------------------------------------------------------------- checking files


def check_file(path: Path, lf: LedgerFile) -> tuple[bool, str]:
    """(ok, why) for one file on disk against its ledger record."""
    from .sources import git_blob_sha1, lfs_pointer_oid, sha256_file

    try:
        size = path.stat().st_size
        if size != lf.size:
            return False, f"size {size}, expected {lf.size}"
        if lf.sha256:
            got = sha256_file(path)
            return (True, "") if got == lf.sha256 else (False, f"SHA-256 {got[:16]}…, expected {lf.sha256[:16]}…")
        if lf.git_oid:
            got = git_blob_sha1(path, size)
            return (True, "") if got == lf.git_oid else (False, "git blob hash differs")
        got = lfs_pointer_oid(sha256_file(path), size)
        return (True, "") if got == lf.lfs_pointer_oid else (False, "SHA-256 doesn't match the recorded LFS pointer")
    except OSError as e:
        return False, str(e)


PARTIAL_SUFFIXES = (".parts", ".part", ".!qb", ".!ut", ".crdownload", ".aria2", ".tmp")


def match_folder(folder: Path, led: Ledger, repo: str | None = None, revision: str | None = None):
    """Find the ledger entry a downloaded folder holds. Returns (entry, root, present) or raises LedgerError.

    Matching uses paths and sizes only (no hashing): the folder itself and each folder directly
    inside it are tried as the model's root, since torrent clients add a top folder."""
    if not folder.is_dir():
        raise LedgerError(f"{folder} is not a folder")
    entries = [e for e in led.entries.values()
               if (repo is None or e.repo.lower() == repo.lower())
               and (revision is None or e.revision == revision.lower())]
    if not entries:
        raise LedgerError("the hash ledger has no entry for " + (f"{repo}" if repo else "any model") +
                          (f" at {revision}" if revision else ""))
    roots = [folder] + sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))
    best, ties = None, []
    for e in entries:
        for root in roots:
            present = sum(1 for lf in e.files.values() if (root / lf.path).is_file())
            sized = sum(1 for lf in e.files.values()
                        if (root / lf.path).is_file() and (root / lf.path).stat().st_size == lf.size)
            score = (sized, present)
            if present == 0:
                continue
            if best is None or score > best[0]:
                best, ties = (score, e, root), []
            elif score == best[0] and e is not best[1]:
                ties.append(e)
    if best is None:
        raise LedgerError("no file in this folder matches a model in the hash ledger" +
                          ("" if repo else "; name the model with --repo"))
    if ties:
        names = ", ".join(sorted({x.key for x in ties + [best[1]]}))
        raise LedgerError(f"the folder matches several ledger entries equally ({names}); pick one with --revision")
    return best[1], best[2]


def folder_extras(root: Path, e: Entry) -> list[str]:
    """Files under root that aren't part of the entry (hidden and partial-download files ignored)."""
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(part.startswith(".") for part in PurePosixPath(rel).parts) or rel.lower().endswith(PARTIAL_SUFFIXES):
            continue
        if rel not in e.files:
            out.append(rel)
    return out


# ---------------------------------------------------------------- capturing (editors)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def capture(repo: str, revision: str, hf, pf) -> tuple[dict | None, str]:
    """Build an entry document for repo@revision. Returns (doc, note) or (None, why not).

    Hugging Face's tree at the revision is the source whenever it still has it. On a gated repo
    (where Hugging Face hides LFS SHA-256s) Pirate Face's hash is used only when it reproduces
    the LFS pointer Hugging Face lists; otherwise the pointer's ID is recorded, which still pins
    the content. When Hugging Face no longer has the repo, Pirate Face's manifest is recorded,
    labelled as such, so at least it can't change later."""
    from .sources import lfs_pointer_oid
    from .torrentmeta import TorrentError, parse_torrent

    tree = hf.tree(repo, revision)
    pf_info = None

    def pf_listing() -> dict:
        nonlocal pf_info
        if pf_info is None:
            try:
                info = pf.torrent(repo) or {}
            except (FetchError, UnsafeURL):
                info = {}
            pf_info = info if str(info.get("revision") or "") == revision else {}
        return pf_info

    def pf_hashes() -> dict[str, str]:
        return {f["path"]: str(f["sha256"]).lower() for f in pf_listing().get("files", [])
                if isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("sha256"), str)
                and HEX64.match(str(f["sha256"]).lower())}

    files, skipped = [], []
    if tree is not None:
        source = "huggingface"
        for path, f in sorted(tree.items()):
            rec: dict = {"path": path, "size": f.size}
            if f.sha256:
                rec["sha256"] = f.sha256
            elif f.git_oid:
                rec["git_oid"] = f.git_oid
            elif f.pointer_oid:
                pfh = pf_hashes().get(path)
                if pfh and lfs_pointer_oid(pfh, f.size) == f.pointer_oid:
                    rec["sha256"] = pfh
                rec["lfs_pointer_oid"] = f.pointer_oid
            else:
                skipped.append(path)
                continue
            files.append(rec)
    else:
        source = "pirateface"
        info = pf_listing()
        ih = str(info.get("infohash") or "").lower()
        if not info or len(ih) != 40:
            return None, "Hugging Face no longer has it and Pirate Face lists no torrent at this revision"
        try:
            meta = parse_torrent(pf.torrent_file(ih))
        except (FetchError, UnsafeURL, TorrentError, ValueError) as e:
            return None, f"Hugging Face no longer has it and Pirate Face's .torrent is unusable: {e}"
        if meta.infohash_v1 != ih:
            return None, "Pirate Face's .torrent doesn't hash to its listed infohash"
        hashes = pf_hashes()
        for tf in sorted(meta.real_files(), key=lambda t: t.repo_path):
            h = hashes.get(tf.repo_path)
            if h:
                files.append({"path": tf.repo_path, "size": tf.size, "sha256": h})
            else:
                skipped.append(tf.repo_path)
    if not files:
        return None, "no file has a published hash"
    doc = {"kind": ENTRY_KIND, "schema": SCHEMA, "repo": repo, "revision": revision, "captured": _now(),
           "source": source, "files": files}
    note = f"{len(files)} files from {source}" + (f", {len(skipped)} without a hash left out" if skipped else "")
    return doc, note


def entry_bytes(doc: dict) -> bytes:
    return (json.dumps(doc, indent=1, ensure_ascii=False) + "\n").encode()


def read_index_doc(path: Path) -> dict:
    if not path.exists():
        return {"kind": KIND, "schema": SCHEMA, "version": 0, "issued": _now(), "entries": {}}
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict) or doc.get("kind") != KIND:
        raise LedgerError(f"{path} is not a hash-ledger index")
    return doc


def add_entry(index_path: Path, doc: dict) -> str:
    """Write a new entry file and list it in the index (unsigned until `flotilla editor sign`).

    Refuses to touch an entry that already exists: entries are written once."""
    index = read_index_doc(index_path)
    key = f"{doc['repo']}@{doc['revision']}"
    if any(ledger_key(*k.rsplit("@", 1)) == ledger_key(doc["repo"], doc["revision"]) for k in index["entries"]):
        raise LedgerError(f"{key} is already in the ledger; entries are never rewritten")
    file = entry_name(doc["repo"], doc["revision"])
    target = index_path.parent / file
    if target.exists():
        raise LedgerError(f"{target} already exists; entries are never rewritten")
    data = entry_bytes(doc)
    parse_entry(data, file)   # never write something nodes would reject
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "xb") as f:
        f.write(data)
    index["entries"][key] = {"file": file, "sha256": digest(data)}
    index["entries"] = dict(sorted(index["entries"].items(), key=lambda kv: kv[0].lower()))
    index["version"] = int(index.get("version", 0)) + 1
    index["issued"] = _now()
    out = (json.dumps(index, indent=1, ensure_ascii=False) + "\n").encode()
    parse_index(out)
    tmp = index_path.with_suffix(".tmp")
    tmp.write_bytes(out)
    os.replace(tmp, index_path)
    return file


def check_index_files(index_path: Path) -> tuple[int, int]:
    """Before signing: every entry the index lists exists, parses and matches its digest.
    Returns (version, number of entries)."""
    version, index = parse_index(index_path.read_bytes())
    for key, (file, sha) in index.items():
        p = index_path.parent / file
        try:
            data = p.read_bytes()
        except OSError as e:
            raise LedgerError(f"{key}: {e}") from e
        if digest(data) != sha:
            raise LedgerError(f"{key}: {file} doesn't match its digest in the index (entries are never edited)")
        e = parse_entry(data, file, sha)
        if e.key != key:
            raise LedgerError(f"{file} is for {e.key}, not {key}")
    return version, len(index)
