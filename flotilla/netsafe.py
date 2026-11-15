"""The only door to the network for metadata.

Every HTTP request Flotilla makes goes through `fetch`. It enforces:

* HTTPS only, certificate verification on (certifi bundle, so it behaves the
  same on every OS).
* A host allowlist (pirateface.co and huggingface.co plus their subdomains by
  default). Redirects are followed by hand and every hop is re-checked, so an
  allowed host cannot bounce us somewhere else.
* No credentials in URLs, no non-default ports.
* A hard byte cap on every response, so a hostile server cannot fill memory.

Torrent *payload* traffic (peers, web seeds) is handled by libtorrent and is
protected differently: by infohash pinning plus SHA-256 checks against
Hugging Face. See DESIGN.md, "Trust model".
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import certifi

from . import USER_AGENT

DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("pirateface.co", "huggingface.co")
MAX_REDIRECTS = 5


class UnsafeURL(ValueError):
    """The URL failed the allowlist or scheme rules."""


class FetchError(RuntimeError):
    def __init__(self, url: str, status: int | None, msg: str):
        super().__init__(f"{url}: {msg}")
        self.url = url
        self.status = status


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        """Parse the body as JSON. A page that isn't JSON (an API that changed shape, an
        error page) becomes a FetchError, which every caller already handles."""
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            ctype = self.headers.get("content-type", "?")
            raise FetchError(self.url, self.status, f"expected JSON, got {ctype} (did the API change?)") from e


def host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in allowed)


def check_url(url: str, allowed: tuple[str, ...], allow_file: bool = False) -> urllib.parse.SplitResult:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "file" and allow_file:
        return parts
    if parts.scheme != "https":
        raise UnsafeURL(f"only https:// is allowed: {url}")
    if parts.username or parts.password:
        raise UnsafeURL(f"credentials in URL are not allowed: {url}")
    if parts.port not in (None, 443):
        raise UnsafeURL(f"non-standard port not allowed: {url}")
    if not parts.hostname or not host_allowed(parts.hostname, allowed):
        raise UnsafeURL(f"host not on allowlist {allowed}: {url}")
    return parts


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_SSL = ssl.create_default_context(cafile=certifi.where())
_OPENER = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=_SSL))


def fetch(
    url: str,
    *,
    max_bytes: int,
    allowed: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS,
    allow_file: bool = False,
    timeout: float = 30.0,
    accept: str | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    """GET `url` under the rules above. Raises FetchError on HTTP errors."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = check_url(url, allowed, allow_file)
        if parts.scheme == "file":
            path = Path(urllib.parse.unquote(parts.path))
            try:
                data = path.read_bytes()
            except OSError as e:
                raise FetchError(url, None, str(e)) from e
            if len(data) > max_bytes:
                raise FetchError(url, None, f"larger than {max_bytes} bytes")
            return Response(url, 200, {}, data)

        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", USER_AGENT)
        if accept:
            req.add_header("Accept", accept)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            resp = _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc:
                    raise FetchError(url, e.code, "redirect without Location") from e
                url = urllib.parse.urljoin(url, loc)
                continue
            raise FetchError(url, e.code, f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FetchError(url, None, str(e)) from e

        with resp:
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FetchError(url, resp.status, f"Content-Length {declared} exceeds cap {max_bytes}")
            chunks, total = [], 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(url, resp.status, f"response exceeds cap {max_bytes}")
                chunks.append(chunk)
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return Response(url, resp.status, hdrs, b"".join(chunks))
    raise FetchError(url, None, "too many redirects")


def fetch_json(url: str, *, max_bytes: int = 8 << 20, **kw):
    return fetch(url, max_bytes=max_bytes, accept="application/json", **kw).json()


def parse_link_next(link_header: str | None) -> str | None:
    """Return the rel="next" URL from an RFC 8288 Link header, if any."""
    if not link_header:
        return None
    for part in link_header.split(","):
        seg = part.strip().split(";")
        if len(seg) < 2:
            continue
        target = seg[0].strip()
        if not (target.startswith("<") and target.endswith(">")):
            continue
        if any(p.strip().replace(" ", "") in ('rel="next"', "rel=next") for p in seg[1:]):
            return target[1:-1]
    return None
