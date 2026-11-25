"""Signed releases: what `flotilla update` installs and the update notice checks for.

Each GitHub release carries `release.json`, signed with the project key (the same key as the priority
list, kept offline by the maintainer, never in CI). It names the version, the SHA-256 of the wheel and
source archive, and the Docker image by digest. So:

* a native update installs only a wheel whose hash the signed manifest names;
* a Docker update pulls the image by that digest, never a tag someone could move;
* someone who takes over the GitHub account, or a CI run, can publish files but can't sign them.

An old signed manifest replayed by a mirror can only say "nothing newer", never downgrade: updates
only ever move to a higher version.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

REPO = "nickmontag/flotilla-face"
IMAGE_REPO = "ghcr.io/nickmontag/flotilla-face"
MANIFEST, SIGNATURE = "release.json", "release.json.minisig"
KIND = "flotilla-release"
# GitHub serves release files from github.com, redirecting to *.githubusercontent.com.
RELEASE_HOSTS: tuple[str, ...] = ("github.com", "githubusercontent.com")
MAX_MANIFEST = 256 * 1024
MAX_FILE = 64 << 20
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_DIGEST = re.compile(r"^[a-z0-9./-]+@sha256:[0-9a-f]{64}$")


class ReleaseError(Exception):
    pass


def version_tuple(v: str) -> tuple[int, ...]:
    if not _VERSION.match(v):
        raise ReleaseError(f"not a release version: {v!r}")
    return tuple(int(x) for x in v.split("."))


def newer(candidate: str, current: str) -> bool:
    """Is candidate a later release than current? A development version (0.2.0.dev1 etc.) never is."""
    try:
        return version_tuple(candidate) > version_tuple(current)
    except ReleaseError:
        return False


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    image: str                                      # ghcr.io/nickmontag/flotilla-face@sha256:...
    files: dict[str, dict] = field(default_factory=dict)   # name -> {"sha256", "size"}

    @property
    def wheel(self) -> str:
        names = [n for n in self.files if n.endswith(".whl")]
        if len(names) != 1:
            raise ReleaseError("the release names no single wheel")
        return names[0]

    def url(self, name: str) -> str:
        return f"https://github.com/{REPO}/releases/download/{self.tag}/{name}"

    @property
    def page(self) -> str:
        return f"https://github.com/{REPO}/releases/tag/{self.tag}"


def trusted_comment(version: str) -> str:
    return f"{KIND} {version}"


def parse(manifest: bytes, signature: str, keys) -> Release:
    """Check the signature (and that it was made for a release of this version), then read it."""
    from .signing import SignatureError, verify

    try:
        _, comment = verify(manifest, signature, keys)
    except SignatureError as e:
        raise ReleaseError(f"release signature: {e}") from e
    try:
        doc = json.loads(manifest)
    except ValueError as e:
        raise ReleaseError(f"release manifest isn't JSON: {e}") from e
    if doc.get("kind") != KIND:
        raise ReleaseError("not a Flotilla release manifest")
    version = str(doc.get("version", ""))
    version_tuple(version)
    if comment != trusted_comment(version):
        raise ReleaseError("the signature was made for something else")
    image = str(doc.get("image", ""))
    if not _DIGEST.match(image) or not image.startswith(IMAGE_REPO + "@"):
        raise ReleaseError(f"release image isn't {IMAGE_REPO} by digest: {image!r}")
    files = doc.get("files") or {}
    for name, f in files.items():
        if "/" in name or not re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256", ""))):
            raise ReleaseError(f"bad file entry {name!r}")
    if doc.get("tag") != f"v{version}":
        raise ReleaseError("release tag and version don't match")
    rel = Release(version, doc["tag"], str(doc.get("notes", "")), image, files)
    _ = rel.wheel       # not used here: reading it raises ReleaseError unless the release names exactly one wheel
    return rel


def make_manifest(version: str, notes: str, image: str, dist: Path) -> bytes:
    """release.json for the files in dist (the wheel and the source archive). For `flotilla editor release`."""
    files = {}
    for path in sorted(dist.iterdir()):
        if path.suffix == ".whl" or path.name.endswith(".tar.gz"):
            data = path.read_bytes()
            files[path.name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    doc = {"kind": KIND, "version": version, "tag": f"v{version}", "notes": notes.strip(),
           "image": image, "files": files}
    return (json.dumps(doc, indent=2) + "\n").encode()


def builtin_keys():
    from .config import BUILTIN_EDITOR_KEYS
    from .signing import PublicKey

    return [PublicKey.parse(k) for k in BUILTIN_EDITOR_KEYS]


def latest(base: str | None = None, keys=None) -> Release:
    """The newest release, checked against the project key. Raises ReleaseError or FetchError."""
    from . import netsafe

    base = base or f"https://github.com/{REPO}/releases/latest/download"
    manifest = netsafe.fetch(f"{base}/{MANIFEST}", max_bytes=MAX_MANIFEST, allowed=RELEASE_HOSTS).body
    sig = netsafe.fetch(f"{base}/{SIGNATURE}", max_bytes=8192, allowed=RELEASE_HOSTS).body
    return parse(manifest, sig.decode("utf-8", "replace"), keys if keys is not None else builtin_keys())


def download(rel: Release, name: str, into: Path | None = None) -> Path:
    """Fetch one of the release's files and check it against the signed manifest. With no `into`, it goes
    in a new temporary folder that the caller removes (its parent is the returned path's parent)."""
    from . import netsafe

    want = rel.files.get(name)
    if not want:
        raise ReleaseError(f"{name} isn't part of release {rel.version}")
    data = netsafe.fetch(rel.url(name), max_bytes=MAX_FILE, allowed=RELEASE_HOSTS, timeout=120).body
    if len(data) != want.get("size", len(data)) or hashlib.sha256(data).hexdigest() != want["sha256"]:
        raise ReleaseError(f"{name} doesn't match the signed release: not installing it")
    made = into is None
    into = into or Path(tempfile.mkdtemp(prefix="flotilla-update-"))
    path = into / name
    try:
        path.write_bytes(data)
    except BaseException:
        if made:        # don't leave the folder we just made behind when the file can't be written
            shutil.rmtree(into, ignore_errors=True)
        raise
    return path


def current() -> str:
    return __version__
