"""A Hugging Face account for gated models: a read-only access token the user chose to connect.

Some models are gated: Hugging Face serves them only to accounts their author let in. To fetch
the pieces no peer has of those, Flotilla can use a token connected with `flotilla hf connect`:

* It's kept in its own file, `hf-token` next to config.toml (mode 0600), or the file
  FLOTILLA_HF_TOKEN_FILE names (a Docker secret, say). Flotilla doesn't read HF_TOKEN or a
  huggingface-cli login: connecting an account is a choice made for Flotilla alone.
* Only read-only tokens are used. A token that can write or spend (inference) is refused, so
  whoever reads that file can't change anything on the account.
* Requests go without it first. It's sent only for models Hugging Face won't serve without
  one, and only to huggingface.co, never to the download CDN a request is redirected to.
* The node checks it with Hugging Face (whoami) the first time a gated model needs it, not at
  startup: until then Hugging Face doesn't learn that this account runs Flotilla.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import netsafe
from .netsafe import FetchError, UnsafeURL

HOST = "huggingface.co"
WHOAMI_URL = f"https://{HOST}/api/whoami-v2"
TOKENS_PAGE = f"https://{HOST}/settings/tokens"
TOKEN_FILE = "hf-token"
TOKEN_RE = re.compile(r"hf_[A-Za-z0-9]{20,100}")
RECHECK_AFTER_ERROR = 900.0     # seconds before asking Hugging Face again after it couldn't be reached


class TokenError(ValueError):
    """This token can't be used; the message says why and what to do."""


@dataclass
class Account:
    name: str                   # the Hugging Face username
    token_name: str             # the token's name on huggingface.co/settings/tokens
    role: str                   # "read", or "fineGrained" with read permissions only
    scoped: list[str] = field(default_factory=list)   # a fine-grained token limited to these repos/users/orgs


def token_path(config_path: Path | None) -> Path | None:
    """Where the token is kept: FLOTILLA_HF_TOKEN_FILE, else hf-token next to the config file."""
    env = os.environ.get("FLOTILLA_HF_TOKEN_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    return config_path.parent / TOKEN_FILE if config_path else None


def clean_token(raw: str) -> str:
    t = raw.strip()
    if not TOKEN_RE.fullmatch(t):
        raise TokenError("that isn't a Hugging Face access token (they start with hf_)")
    return t


def read_token(path: Path | None) -> str | None:
    """The token in `path`, or None if there's no file. TokenError if the file holds something else."""
    if path is None:
        return None
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    return clean_token(raw)


def save_token(path: Path, token: str) -> None:
    """Write the token readable by this user only, replacing any earlier one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(clean_token(token) + "\n")
    os.chmod(tmp, 0o600)        # in case a leftover .tmp had another mode
    os.replace(tmp, path)


def remove_token(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def readable_by_others(path: Path) -> bool:
    try:
        return bool(path.stat().st_mode & 0o077)
    except OSError:
        return False


def read_only_problem(auth: dict) -> str | None:
    """Why the token whoami describes can do more than read, or None if it only reads."""
    tok = auth.get("accessToken") if isinstance(auth, dict) else None
    if not isinstance(tok, dict):
        return "Hugging Face didn't say what this token may do"
    role = tok.get("role")
    if role == "read":
        return None
    if role == "fineGrained":
        fg = tok.get("fineGrained") or {}
        perms = {str(p) for p in fg.get("global") or []}
        for s in fg.get("scoped") or []:
            perms.update(str(p) for p in (s or {}).get("permissions") or [])
        more = sorted(p for p in perms if not p.endswith(".read"))
        return f"it may also {', '.join(more)}" if more else None
    if role == "write":
        return "it's a Write token"
    return f"it's a {role or 'unknown'} token"


def check(token: str, timeout: float = 20.0) -> Account:
    """Ask Hugging Face whose token this is. TokenError if it's rejected or can do more than read;
    FetchError if Hugging Face can't be reached."""
    bearer = f"Bearer {clean_token(token)}"
    try:
        doc = netsafe.fetch_json(WHOAMI_URL, allowed=(HOST,), auth=(HOST, bearer), timeout=timeout, max_bytes=1 << 20)
    except UnsafeURL as e:
        raise FetchError(WHOAMI_URL, None, f"sent off the allowlist ({e})") from e
    except FetchError as e:
        if e.status == 401:
            raise TokenError("Hugging Face doesn't accept this token (revoked, expired or mistyped)") from e
        raise
    except ValueError as e:   # not JSON
        raise FetchError(WHOAMI_URL, None, f"unexpected reply ({e})") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("auth"), dict):
        raise FetchError(WHOAMI_URL, None, "unexpected reply")
    if doc.get("type") not in (None, "user"):
        raise TokenError(f"it belongs to {doc.get('type')} {doc.get('name')!r}, not a personal account; gated "
                         f"models let in people, so use one of your own tokens")
    problem = read_only_problem(doc["auth"])
    if problem:
        raise TokenError(f"{problem}. Flotilla only uses read-only tokens, so whoever can read its token file "
                         f"can't change anything on your account: make a Read token at {TOKENS_PAGE}")
    tok = doc["auth"]["accessToken"]
    scoped = [str((s.get("entity") or {}).get("name")) for s in (tok.get("fineGrained") or {}).get("scoped") or []
              if isinstance(s, dict) and (s.get("entity") or {}).get("name")]
    return Account(name=str(doc.get("name") or "?"), token_name=str(tok.get("displayName") or "?"),
                   role=str(tok.get("role")), scoped=scoped)


class Connection:
    """The running node's view of the connected token. The file is re-read when it changes (so
    `flotilla hf connect` and `disconnect` take effect without a restart), and the token is checked
    with Hugging Face the first time a gated model needs it. Thread-safe."""

    def __init__(self, path: Path | None, log=print):
        self.path = path
        self.log = log
        self._lock = threading.Lock()
        self._sig: object = None
        self.token: str | None = None
        self.account: Account | None = None
        self.state = "none"     # none / unchecked / ok / refused / error
        self.why: str | None = None
        self._retry_at = 0.0
        self.generation = 0     # goes up whenever the token in use changes

    def refresh(self) -> bool:
        """Re-read the token file if it changed. True if the token or its state changed."""
        if self.path is None:
            return False
        try:
            st = self.path.stat()
            sig: object = (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            sig = None
        except OSError as e:
            sig = ("error", str(e))
        if sig == self._sig:
            return False
        self._sig = sig
        token, state, why = None, "none", None
        if sig is not None:
            try:
                token = read_token(self.path)
                state = "unchecked" if token else "none"
            except (TokenError, OSError) as e:
                state, why = "refused", f"the token file {self.path} can't be used: {e}"
        with self._lock:
            changed = token != self.token or state != self.state
            self.token, self.state, self.why, self.account, self._retry_at = token, state, why, None, 0.0
            self.generation += 1
        if changed:
            if state == "unchecked":
                self.log("hugging face: an account token is connected; it's used only for models Hugging Face "
                         "won't serve without one")
            elif state == "refused":
                self.log(f"hugging face: {why}")
            else:
                self.log("hugging face: no account token is connected; gated models wait for peers")
        return changed

    def auth(self) -> tuple[str, str] | None:
        """(host, Authorization value) for huggingface.co, or None if there's no usable token."""
        with self._lock:
            if self.state == "ok":
                return HOST, f"Bearer {self.token}"
            if not (self.state == "unchecked" or (self.state == "error" and time.time() >= self._retry_at)):
                return None
            token = self.token
        try:
            acct = check(token)
        except TokenError as e:
            self._set(token, "refused", f"the connected token isn't used: {e}")
            return None
        except FetchError as e:
            self._set(token, "error", f"couldn't check the token with Hugging Face ({e})",
                      retry_at=time.time() + RECHECK_AFTER_ERROR)
            return None
        if not self._set(token, "ok", None, acct):
            return None
        self.log(f"hugging face: using {acct.name}'s read-only token {acct.token_name!r} for gated models")
        return HOST, f"Bearer {token}"

    def rejected(self) -> None:
        """Hugging Face turned the token down on a download (revoked or expired since it was checked)."""
        with self._lock:
            token = self.token
        self._set(token, "refused", "Hugging Face no longer accepts the connected token (revoked or expired?); "
                                    "connect a new one with `flotilla hf connect`")

    def _set(self, token, state, why, account=None, retry_at=0.0) -> bool:
        with self._lock:
            if token != self.token:     # disconnected or replaced meanwhile
                return False
            said = (self.state, self.why)
            self.state, self.why, self.account, self._retry_at = state, why, account, retry_at
        if why and (state, why) != said:
            self.log(f"hugging face: {why}")
        return True

    def disconnect(self) -> bool:
        """Remove the token file. True if there was one."""
        gone = remove_token(self.path)
        self.refresh()
        return gone

    def info(self) -> dict:
        with self._lock:
            a = self.account
            return {"state": self.state, "why": self.why, "name": a.name if a else None,
                    "token_name": a.token_name if a else None, "role": a.role if a else None,
                    "scoped": list(a.scoped) if a else [], "path": str(self.path) if self.path else None,
                    "from_env": bool(os.environ.get("FLOTILLA_HF_TOKEN_FILE", "").strip())}
