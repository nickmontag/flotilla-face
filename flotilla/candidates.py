"""Turn listings into *trusted* torrent candidates.

This is where the trust chain is enforced. A torrent only becomes a candidate
when all of these hold:

1. Its magnet parses and every tracker / web seed is on the allowlist.
2. The infohash in the magnet equals the one Pirate Face lists, and equals the
   signed feed's pin when the feed pins one (same for the revision).
3. The .torrent we fetch hashes (SHA-1 of the info dict) to that infohash, and
   its own trackers / web seeds pass the same allowlist.
4. Every file in the torrent exists in the Hugging Face repo at the pinned
   revision with the same size (when HF still has the repo). So a torrent can
   never smuggle in files that are not part of the model.
5. Every file we might download has an expected hash: SHA-256 for LFS files and
   the git blob SHA-1 for small files, from huggingface.co itself. Hugging Face
   is authoritative whenever it still has the repo. Pirate Face's SHA-256
   manifest is cross-checked: a disagreement is accepted (and flagged for
   reporting) only if HF's current main still has the same file as the pinned
   revision; otherwise the torrent is refused as ambiguous.
   When Pirate Face replaced a pinned torrent (its record of the pin names a successor), the
   successor is followed, but only if Hugging Face or the hash ledger vouches for every file:
   the pin's job, an anchor independent of the index, passes to them.
6. The signed hash ledger (ledger.py), when it records this repo at this
   revision: every torrent file must be in the record with the same size, and
   Hugging Face must agree with it. When HF no longer has the repo, the ledger's
   hashes are the expected hashes ("ledger"), and Pirate Face's manifest is only
   cross-checked. A ledger that knows the repo only at other revisions refuses
   the torrent. With no ledger record either, Pirate Face's manifest is the only
   source and the candidate is marked "pirateface-only".

While Pirate Face can't be reached, the listing (magnet, infohash, revision) can come from the node's
copy of its magnet index (magnetindex.py) and the .torrent from the swarm. There's no Pirate Face
manifest then; everything else above applies unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .config import Config
from .feed import Feed, FeedModel
from .ledger import Ledger, LedgerFile
from .magnet import MagnetError, parse_magnet, validate_magnet
from .netsafe import FetchError, UnsafeURL
from .sources import _HEX40, HuggingFace, PirateFace, _hex, is_repo_id, lfs_pointer_oid
from .torrentmeta import TorrentError, TorrentMeta, parse_torrent

WEIGHT_EXT = {".safetensors", ".gguf", ".onnx", ".onnx_data", ".tflite", ".msgpack", ".mlmodel"}
# Weight formats that can run code when a model is loaded: pickle (.bin/.pt/...), Keras HDF5 and
# .keras files (Lambda layers carry Python code) and NumPy files (object arrays are pickled).
# Skipped unless policy.allow_pickle is on.
RUNS_CODE_EXT = {".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle", ".joblib",
                 ".h5", ".hdf5", ".keras", ".npz", ".npy"}
META_EXT = {".json", ".md", ".txt", ".jinja", ".model", ".tiktoken", ".vocab", ".merges", ".yaml", ".yml",
            ".spm", ".cfg", ".csv", ".tsv", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".gitattributes"}
CODE_EXT = {".py", ".pyc", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".exe", ".dll", ".so", ".dylib",
            ".js", ".mjs", ".ts", ".jar", ".ipynb", ".lua", ".rb", ".pl", ".html", ".htm", ".svg", ".cu",
            ".cpp", ".cc", ".c", ".h", ".hpp", ".swift", ".rs", ".go", ".wasm"}
BUNDLE_MAX = 64 << 20      # metadata files up to this size ride along with any shard
NOEXT_MAX = 1 << 20


@dataclass
class FileCand:
    index: int
    repo_path: str
    size: int
    kind: str                      # "weight" | "meta" | "skip"
    first_piece: int
    last_piece: int
    sha256: str | None = None
    git_oid: str | None = None
    availability: float | None = None   # peers (not us) holding every piece of it
    held: bool = False
    held_since: float | None = None
    done: float = 0.0              # share of it this node has (1.0 once verified)
    downloaders: int | None = None # probe: downloaders fetching it (engine.wanted_files). None: no probe
    skip_reason: str = ""


@dataclass
class TorrentCand:
    repo: str
    quant: str | None
    infohash: str
    revision: str
    magnet: str
    license: str | None
    priority: float
    target_replicas: int
    hf_backed: bool | None
    seeders: int
    leechers: int                  # Pirate Face's tracker: anyone without a full copy, Flotilla nodes included
    meta: TorrentMeta = field(repr=False)
    files: list[FileCand] = field(default_factory=list)
    verify_source: str = "huggingface"
    pinned: bool = False
    listed: bool = False
    replaces: str = ""             # the pinned torrent Pirate Face replaced with this one
    index_build: str = ""          # listed from the magnet index copy (Pirate Face unreachable)
    note: str = ""
    # Files where Pirate Face's manifest SHA-256 disagrees with Hugging Face (or, once Hugging Face
    # no longer has the repo, with the hash ledger). We verify against those; this list is for
    # reporting to Pirate Face.
    manifest_mismatches: list[dict] = field(default_factory=list)   # {"path", "pirateface", "huggingface" | "ledger"}
    # Peers the probe saw still fetching, Flotilla nodes left out (engine.probe). None: no probe.
    downloaders: int | None = None
    # target_replicas is the target in use: target_base, or at most BACKFILL_CAP until every model on
    # the list has two copies (planner.set_targets).
    target_base: int = 0
    spare: bool = False            # FeedModel.spare: looked at only once the list is covered

    def __post_init__(self) -> None:
        if not self.target_base:
            self.target_base = self.target_replicas

    @property
    def piece_length(self) -> int:
        return self.meta.piece_length

    @property
    def selectable_bytes(self) -> int:
        return sum(f.size for f in self.files if f.kind != "skip")


@dataclass
class Rejection:
    repo: str
    reason: str
    transient: bool = False   # network trouble: keep what we hold, try again later
    delisted: bool = False    # Pirate Face lists no torrent for it (a 404, or a listing without one)


def classify(repo_path: str, size: int, cfg: Config) -> tuple[str, str]:
    p = PurePosixPath(repo_path)
    name = p.name
    ext = p.suffix.lower() if p.suffix else ("." + name.lstrip(".") if name.startswith(".") else "")
    if ext in WEIGHT_EXT:
        return "weight", ""
    if ext in RUNS_CODE_EXT:
        return ("weight", "") if cfg.allow_pickle else ("skip", "format can run code when loaded (policy.allow_pickle=false)")
    if ext in CODE_EXT:
        return ("meta", "") if cfg.seed_code_files else ("skip", "code file (policy.seed_code_files=false)")
    if ext in META_EXT:
        return ("meta", "") if size <= BUNDLE_MAX else ("weight", "")
    if not ext and size <= NOEXT_MAX:
        return "meta", ""
    return "skip", f"unrecognised file type {ext or '(none)'}"


_warned: set[str] = set()


def _warn_once(log, key: str, msg: str) -> None:
    """A failure that repeats every refresh (an API that changed shape) is logged once per run."""
    if key not in _warned:
        _warned.add(key)
        log(msg)


def check_urls(trackers: list[str], web_seeds: list[str]) -> None:
    from .magnet import Magnet

    validate_magnet(Magnet(btih="0" * 40, trackers=trackers, web_seeds=web_seeds))


def _ledger_disagrees(lf: LedgerFile, sha256: str | None, git_oid: str | None, pointer_oid: str | None) -> str:
    """Why Hugging Face's record of a file contradicts the ledger's, or "" if it doesn't."""
    if lf.sha256 and sha256 and lf.sha256 != sha256.lower():
        return "SHA-256"
    if lf.git_oid and git_oid and lf.git_oid != git_oid.lower():
        return "git blob hash"
    if lf.lfs_pointer_oid and pointer_oid and lf.lfs_pointer_oid != pointer_oid.lower():
        return "LFS pointer"
    if lf.sha256 and not sha256 and pointer_oid and lfs_pointer_oid(lf.sha256, lf.size) != pointer_oid.lower():
        return "LFS pointer"
    return ""


def _count(v) -> int:
    """A peer count from Pirate Face's listing; anything that isn't a count is 0."""
    if isinstance(v, str) and v.isascii() and v.isdigit() and len(v) < 10:
        return int(v)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def build_candidate(
    repo: str,
    quant: str | None,
    feed: Feed,
    entry: FeedModel | None,
    pf: PirateFace,
    hf: HuggingFace,
    cfg: Config,
    license_hint: str | None = None,
    catalog_hf_backed: bool | None = None,
    ledger: Ledger | None = None,
    index=None,
) -> TorrentCand | Rejection:
    if not is_repo_id(repo):
        return Rejection(repo, "not a model name")   # Pirate Face's rescue list and feed supply these
    if repo.lower() in feed.deny:
        return Rejection(repo, "on the feed's deny list")
    from_index = ""
    try:
        info = pf.torrent(repo, quant)
    except (FetchError, UnsafeURL) as e:
        info = index.listing(repo) if index is not None and quant is None else None
        if info is None:
            return Rejection(repo, f"Pirate Face lookup failed: {e}", transient=True)
        from_index = info["index_build"]
    # A 404, or a listing without a torrent: Pirate Face doesn't offer one (any more). A delisting
    # is Pirate Face's call, as the magnet index's removals are, so this isn't transient.
    if not isinstance(info, dict) or not info.get("magnet") or not info.get("infohash"):
        return Rejection(repo, "no magnet listed (no torrent yet, or terms must be accepted first)", delisted=True)
    if not isinstance(info["magnet"], str) or not isinstance(info["infohash"], str):
        return Rejection(repo, "listing's magnet or infohash isn't a string")

    try:
        mag = validate_magnet(parse_magnet(info["magnet"]))
    except MagnetError as e:
        return Rejection(repo, f"magnet rejected: {e}")
    ih = mag.btih
    if ih != str(info["infohash"]).lower():
        return Rejection(repo, "magnet infohash differs from the listed infohash")
    if ih in feed.deny:
        return Rejection(repo, "infohash on the feed's deny list")
    # The revision ends up in Hugging Face URLs and cache file names: only a full commit hash.
    revision = info.get("revision")
    revision = revision.lower() if isinstance(revision, str) else ""
    pinned, replaces = False, ""
    if entry and entry.infohash:
        if entry.infohash != ih:
            if from_index:
                return Rejection(repo, f"the magnet index lists {ih[:12]}…, not the signed pin {entry.infohash[:12]}…, "
                                       f"and Pirate Face can't be asked whether it replaced the pin", transient=True)
            try:
                successor = pf.replacement(entry.infohash)
            except (FetchError, UnsafeURL) as e:
                return Rejection(repo, f"listed infohash {ih[:12]}… isn't the signed pin {entry.infohash[:12]}…, "
                                       f"and Pirate Face couldn't be asked whether it replaced it: {e}", transient=True)
            if successor != ih:
                return Rejection(repo, f"listed infohash {ih[:12]}… does not match the signed pin {entry.infohash[:12]}…")
            replaces = entry.infohash
        pinned = True
    if entry and entry.revision and entry.revision != revision:
        return Rejection(repo, "listed revision does not match the signed pin")
    if not _HEX40.fullmatch(revision):
        return Rejection(repo, "listing has no pinned revision")

    if license_hint == "other":   # a placeholder for a custom license: ask Hugging Face for its name
        license_hint = None
    license_ = (entry.license if entry and entry.license else None) or license_hint
    if license_ is None:
        license_, how = hf.license_lookup(repo)
        if license_ is None and how == "unreachable":
            # No answer isn't "no license": keep what we hold and ask again next refresh.
            return Rejection(repo, "license unknown: Hugging Face couldn't be asked", transient=True)
        if license_ is None and how == "gone":
            # Hugging Face took it down before this node read its license. A copy held here was
            # taken on with a license read then, so it's kept, but no new copy is started.
            return Rejection(repo, "license unknown: Hugging Face no longer has it, and its license "
                                   "was never read here", transient=True)
    if license_ is None:
        return Rejection(repo, "license unknown")
    if license_ not in cfg.licenses:
        return Rejection(repo, f"license {license_!r} not in policy.licenses")

    try:
        raw = pf.torrent_file(ih)
    except ValueError as e:      # not an infohash (validated above; a hostile listing never gets here)
        return Rejection(repo, f".torrent rejected: {e}")
    except FetchError as e:
        if index is None or not from_index:
            return Rejection(repo, f".torrent download failed: {e}", transient=True)
        from .magnetindex import Unavailable

        try:
            raw = index.torrent(info["magnet"])     # libtorrent checks it against the infohash
        except (Unavailable, MagnetError) as e2:
            return Rejection(repo, f"Pirate Face can't be reached and the swarm didn't send the .torrent: {e2}",
                             transient=True)
        pf.remember_torrent(ih, raw)
    try:
        meta = parse_torrent(raw)
    except (TorrentError, ValueError) as e:
        return Rejection(repo, f".torrent rejected: {e}")
    if meta.infohash_v1 != ih:
        return Rejection(repo, ".torrent does not hash to the listed infohash")
    try:
        check_urls(meta.trackers, meta.web_seeds)
    except MagnetError as e:
        return Rejection(repo, f".torrent rejected: {e}")

    try:
        tree = hf.tree(repo, revision)
    except (FetchError, UnsafeURL) as e:
        return Rejection(repo, f"Hugging Face tree lookup failed: {e}", transient=True)
    except ValueError as e:
        return Rejection(repo, f"Hugging Face tree lookup refused: {e}")
    # Pirate Face's SHA-256 manifest: lower-cased, and only what is 64 hex digits.
    pf_files = info.get("files")
    pf_sha = {f["path"]: _hex(f.get("sha256"), 64) for f in (pf_files if isinstance(pf_files, list) else [])
              if isinstance(f, dict) and isinstance(f.get("path"), str)}
    record = ledger.get(repo, revision) if ledger else None
    if record is None and tree is None and ledger is not None and ledger.revisions(repo):
        known = ", ".join(r[:12] for r in ledger.revisions(repo))
        return Rejection(repo, f"Hugging Face no longer has it, and the hash ledger records it at {known}, "
                               f"not at the listed {revision[:12]} (refused)")
    if replaces and tree is None and record is None:
        return Rejection(repo, f"Pirate Face replaced the signed pin {replaces[:12]}… with {ih[:12]}…, but with Hugging "
                               f"Face gone and no hash-ledger record nothing independent vouches for the new torrent")

    files: list[FileCand] = []
    mismatches: list[dict] = []
    main_tree: dict | None = None
    main_checked = False
    for tf in meta.real_files():
        first, last = tf.piece_range(meta.piece_length)
        kind, why = classify(tf.repo_path, tf.size, cfg)
        fc = FileCand(tf.index, tf.repo_path, tf.size, kind, first, last, skip_reason=why)
        pf_hash = pf_sha.get(tf.repo_path)
        lf = record.files.get(tf.repo_path) if record else None
        if record is not None:
            if lf is None:
                return Rejection(repo, f"torrent contains {tf.repo_path!r}, which the hash ledger's record of "
                                       f"{revision[:12]} doesn't list")
            if lf.size != tf.size:
                return Rejection(repo, f"size of {tf.repo_path!r} differs from the hash ledger")
        if tree is not None:
            hf_file = tree.get(tf.repo_path)
            if hf_file is None:
                return Rejection(repo, f"torrent contains {tf.repo_path!r}, which is not in the HF repo at {revision[:12]}")
            if hf_file.size != tf.size:
                return Rejection(repo, f"size of {tf.repo_path!r} differs from Hugging Face")
            # Hugging Face is the authority whenever it still has the repo.
            fc.sha256, fc.git_oid = hf_file.sha256, hf_file.git_oid
            if lf is not None:
                what = _ledger_disagrees(lf, hf_file.sha256, hf_file.git_oid, hf_file.pointer_oid)
                if what:
                    return Rejection(repo, f"Hugging Face and the hash ledger disagree on the {what} of "
                                           f"{tf.repo_path!r} at {revision[:12]} (refused)")
                if not fc.sha256 and not fc.git_oid:
                    fc.sha256 = lf.sha256   # HF hides it (gated repo); the ledger's matches HF's LFS pointer
            if not fc.sha256 and hf_file.pointer_oid and pf_hash:
                # Hugging Face hides this file's SHA-256 (it does on gated repos) but lists its LFS
                # pointer, which commits to the SHA-256: Pirate Face's hash must reproduce it.
                if lfs_pointer_oid(pf_hash, tf.size) != hf_file.pointer_oid:
                    return Rejection(repo, f"Pirate Face's hash for {tf.repo_path!r} doesn't match the file "
                                           f"Hugging Face lists at {revision[:12]}")
                fc.sha256 = pf_hash.lower()
            if pf_hash and fc.sha256 and pf_hash.lower() != fc.sha256.lower():
                # Pirate Face's manifest disagrees. Accept HF's hash only if HF's
                # current main still has the same file: then HF is self-consistent
                # and the disagreement can't be Pirate Face having packaged a newer
                # upload under an old revision label. The downloaded bytes are
                # graded against HF either way, so a bad swarm is caught and blocked.
                if not main_checked:
                    try:
                        main_tree = hf.tree(repo, "main")
                    except (FetchError, UnsafeURL) as e:
                        return Rejection(repo, f"Pirate Face's hash for {tf.repo_path!r} disagrees with Hugging Face, "
                                               f"and Hugging Face main could not be checked: {e}", transient=True)
                    except ValueError as e:
                        return Rejection(repo, f"Hugging Face main could not be checked: {e}")
                    main_checked = True
                on_main = main_tree.get(tf.repo_path) if main_tree else None
                if on_main is None or (on_main.sha256 or "").lower() != fc.sha256.lower():
                    return Rejection(repo, f"Pirate Face and Hugging Face disagree on the SHA-256 of {tf.repo_path!r}, "
                                           f"and Hugging Face main differs from the pinned revision (ambiguous; refused)")
                mismatches.append({"path": tf.repo_path, "pirateface": pf_hash.lower(),
                                   "huggingface": fc.sha256.lower()})
            if not (fc.sha256 or fc.git_oid) and pf_hash:
                fc.sha256 = pf_hash  # HF lists the file but publishes no hash: fall back to Pirate Face
        elif lf is not None:
            # HF no longer has the repo, but the ledger recorded it: its hashes are the expected ones.
            fc.sha256, fc.git_oid = lf.sha256, lf.git_oid
            if not fc.sha256 and lf.lfs_pointer_oid and pf_hash:
                if lfs_pointer_oid(pf_hash, tf.size) != lf.lfs_pointer_oid:
                    return Rejection(repo, f"Pirate Face's hash for {tf.repo_path!r} doesn't match the file the "
                                           f"hash ledger recorded at {revision[:12]}")
                fc.sha256 = pf_hash.lower()
            if pf_hash and fc.sha256 and pf_hash.lower() != fc.sha256:
                mismatches.append({"path": tf.repo_path, "pirateface": pf_hash.lower(), "ledger": fc.sha256})
        else:
            fc.sha256 = pf_hash  # HF no longer has the repo: Pirate Face's manifest is all there is
        if fc.kind != "skip" and not (fc.sha256 or fc.git_oid):
            fc.kind, fc.skip_reason = "skip", "no published hash to verify against"
        files.append(fc)

    hf_backed = bool(info.get("hf")) and tree is not None
    if tree is None:
        hf_backed = False if (info.get("hf") is False or catalog_hf_backed is False) else None
    target = (entry.target_replicas if entry and entry.target_replicas else None) or feed.target_replicas
    priority = feed.priority(entry) if entry else feed.unlisted_priority
    if tree is None and record is not None:
        source = "ledger" + (f" (pirateface manifest disagreed on {len(mismatches)} file(s))" if mismatches else "")
    elif tree is None:
        source = "pirateface-only"
    elif mismatches:
        source = f"huggingface (pirateface manifest disagreed on {len(mismatches)} file(s))"
    else:
        source = "huggingface"
    return TorrentCand(
        repo=repo, quant=info.get("quant") if isinstance(info.get("quant"), str) else None, infohash=ih,
        revision=revision, magnet=info["magnet"],
        license=license_, priority=priority, target_replicas=target, hf_backed=hf_backed,
        seeders=_count(info.get("seeders")), leechers=_count(info.get("leechers")), meta=meta, files=files,
        verify_source=source, pinned=pinned, listed=entry is not None and not entry.sweep,
        note=entry.note if entry else "",
        manifest_mismatches=mismatches, replaces=replaces, index_build=from_index,
        spare=bool(entry and entry.spare),
    )


def sweep_entry(repo: str) -> FeedModel:
    """A catalog model the sweep picked (sweep.py): last in priority, two copies wanted."""
    from .sweep import PRIORITY, TARGET

    return FeedModel(repo, {}, target_replicas=TARGET, note="Least-seeded model in Pirate Face's catalog (sweep)",
                     priority_override=PRIORITY, sweep=True)


def gather(cfg: Config, feed: Feed, pf: PirateFace, hf: HuggingFace, held_repos: set[str], log=print,
           ledger: Ledger | None = None, index=None, covered: bool = False, sweep: list[str] = (),
           swept: set[str] = frozenset()):
    """Decide which repos to look at, then build candidates for each.

    Order: the signed feed's models, models we already hold, then (if enabled)
    Pirate Face's rescued models and its new torrents that need seeders, at most
    policy.max_candidates of those, then the catalog sweep's picks (`sweep`). A held model
    the sweep once picked (`swept`) keeps the sweep's priority and target.
    """
    wanted: list[tuple[str, str | None, FeedModel | None]] = []
    seen: set[tuple[str, str | None]] = set()
    by_repo = feed.by_repo()

    def add(repo: str, quant: str | None, entry: FeedModel | None):
        key = (repo, quant)
        if key not in seen:
            seen.add(key)
            wanted.append((repo, quant, entry))

    for m in feed.models:
        if m.spare and not covered and m.repo not in held_repos:
            continue          # only once the rest of the list has its copies (planner.set_targets)
        add(m.repo, m.quant, m)
    for repo in sorted(held_repos):
        listed_as = (by_repo.get(repo) or [None])[0]
        add(repo, None, listed_as or (sweep_entry(repo) if repo in swept else None))

    fresh: list[dict] = []
    if cfg.include_unlisted:
        try:
            fresh = pf.new_torrents()
        except (FetchError, UnsafeURL) as e:
            _warn_once(log, "new-torrents", f"catalog: could not read Pirate Face's new-torrents feed ({e}); "
                                            f"carrying on with the priority list. Not repeated until Flotilla restarts.")
    rescued: list[str] = []
    if cfg.include_unlisted and cfg.include_rescue:
        try:
            r = pf.rescue()
            got = r.get("rescued") if isinstance(r, dict) else None
            rescued = [x["repoId"] for x in (got if isinstance(got, list) else [])
                       if isinstance(x, dict) and is_repo_id(x.get("repoId"))]
        except (FetchError, UnsafeURL) as e:
            _warn_once(log, "rescue", f"catalog: could not read Pirate Face's rescue list ({e}); "
                                    f"carrying on with the priority list. Not repeated until Flotilla restarts.")
    # Everything on the list and everything held is always looked at; policy.max_candidates caps
    # only the unlisted models added below (each costs Pirate Face lookups on every refresh).
    listed = len(wanted)
    if cfg.include_unlisted:
        for repo in rescued:
            add(repo, None, None)
        # Least-seeded first, newest first among equals: that's where a new seeder helps most.
        fresh = [m for m in fresh if isinstance(m, dict) and is_repo_id(m.get("repo"))]
        for m in sorted(fresh, key=lambda m: _count(m.get("seeders"))):
            add(m["repo"], None, None)

    wanted = wanted[: listed + max(0, cfg.max_candidates)]
    for repo in sweep:
        if repo.lower() not in feed.deny:
            add(repo, None, sweep_entry(repo))
    cands: list[TorrentCand] = []
    rejected: list[Rejection] = []
    for repo, quant, entry in wanted:
        c = build_candidate(repo, quant, feed, entry, pf, hf, cfg, ledger=ledger, index=index)
        (cands if isinstance(c, TorrentCand) else rejected).append(c)
    return cands, rejected
