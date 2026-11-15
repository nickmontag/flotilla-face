"""Ed25519 signatures in minisign format.

Public keys and signatures are byte-compatible with `minisign` (and therefore
with `rsign2` and friends), so feed editors can sign with the standard
`minisign -S -m priorities.json` instead of this tool if they prefer.

Public key line : base64("Ed" || key_id[8] || pk[32])
Signature file  : untrusted comment line
                  base64("ED" || key_id[8] || ed25519(BLAKE2b-512(msg)))   (or "Ed": raw msg, legacy)
                  trusted comment line
                  base64(ed25519(sig[64] || trusted_comment))

Secret keys use a small Flotilla-only format (unencrypted, file mode 0600):
                  base64("Eq" || key_id[8] || seed[32])
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class SignatureError(ValueError):
    pass


@dataclass(frozen=True)
class PublicKey:
    key_id: bytes
    key: Ed25519PublicKey

    @classmethod
    def parse(cls, text: str) -> "PublicKey":
        """Accept a bare base64 line or a full minisign .pub file."""
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        b64 = next((ln for ln in lines if not ln.startswith("untrusted comment:")), "")
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception as e:
            raise SignatureError(f"public key is not base64: {e}") from e
        if len(raw) != 42 or raw[:2] != b"Ed":
            raise SignatureError("not a minisign Ed25519 public key")
        return cls(raw[2:10], Ed25519PublicKey.from_public_bytes(raw[10:]))

    def line(self) -> str:
        raw = b"Ed" + self.key_id + self.key.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return base64.b64encode(raw).decode()

    @property
    def key_id_hex(self) -> str:
        return self.key_id[::-1].hex().upper()  # minisign prints it little-endian


@dataclass(frozen=True)
class SecretKey:
    key_id: bytes
    key: Ed25519PrivateKey

    @classmethod
    def generate(cls) -> "SecretKey":
        return cls(os.urandom(8), Ed25519PrivateKey.generate())

    @classmethod
    def load(cls, path: Path) -> "SecretKey":
        lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
        b64 = next((ln for ln in lines if not ln.startswith("untrusted comment:")), "")
        raw = base64.b64decode(b64, validate=True)
        if len(raw) != 42 or raw[:2] != b"Eq":
            raise SignatureError(f"{path} is not a Flotilla secret key")
        return cls(raw[2:10], Ed25519PrivateKey.from_private_bytes(raw[10:]))

    def save(self, path: Path) -> None:
        seed = self.key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        )
        body = (
            "untrusted comment: flotilla feed-editor secret key (UNENCRYPTED: keep offline, back it up)\n"
            + base64.b64encode(b"Eq" + self.key_id + seed).decode()
            + "\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(body)

    @property
    def public(self) -> PublicKey:
        return PublicKey(self.key_id, self.key.public_key())

    def sign(self, message: bytes, trusted_comment: str | None = None, untrusted_comment: str | None = None) -> str:
        if trusted_comment is None:
            trusted_comment = f"timestamp:{int(time.time())}"
        if "\n" in trusted_comment or "\r" in trusted_comment:
            raise SignatureError("trusted comment must be one line")
        digest = hashlib.blake2b(message, digest_size=64).digest()
        sig = self.key.sign(digest)
        global_sig = self.key.sign(sig + trusted_comment.encode())
        untrusted_comment = untrusted_comment or f"signature from flotilla key {self.public.key_id_hex}"
        return (
            f"untrusted comment: {untrusted_comment}\n"
            f"{base64.b64encode(b'ED' + self.key_id + sig).decode()}\n"
            f"trusted comment: {trusted_comment}\n"
            f"{base64.b64encode(global_sig).decode()}\n"
        )


def verify(message: bytes, signature_text: str, keys: list[PublicKey]) -> tuple[PublicKey, str]:
    """Verify a minisign signature against any of `keys`.

    Returns (key_used, trusted_comment). Raises SignatureError on any failure.
    """
    lines = signature_text.replace("\r\n", "\n").split("\n")
    lines = [ln for ln in lines if ln.strip()]
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith("trusted comment: "):
        raise SignatureError("malformed signature file")
    try:
        sig_blob = base64.b64decode(lines[1].strip(), validate=True)
        global_sig = base64.b64decode(lines[3].strip(), validate=True)
    except Exception as e:
        raise SignatureError(f"signature is not base64: {e}") from e
    if len(sig_blob) != 74 or len(global_sig) != 64:
        raise SignatureError("signature has the wrong length")
    alg, key_id, sig = sig_blob[:2], sig_blob[2:10], sig_blob[10:]
    if alg == b"ED":
        signed = hashlib.blake2b(message, digest_size=64).digest()
    elif alg == b"Ed":
        signed = message
    else:
        raise SignatureError(f"unknown signature algorithm {alg!r}")
    trusted_comment = lines[2][len("trusted comment: "):]

    candidates = [k for k in keys if k.key_id == key_id]
    if not candidates:
        raise SignatureError("signed by a key that is not trusted")
    for k in candidates:
        try:
            k.key.verify(sig, signed)
            k.key.verify(global_sig, sig + trusted_comment.encode())
            return k, trusted_comment
        except InvalidSignature:
            continue
    raise SignatureError("signature does not verify")
