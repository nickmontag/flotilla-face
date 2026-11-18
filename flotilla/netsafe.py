"""The only door to the network for Flotilla's own HTTP requests.

Every HTTP request Flotilla makes goes through `fetch` (listings, metadata) or
`fetch_range` (pieces from Hugging Face that no peer can supply). They enforce:

* HTTPS only, certificate verification on (certifi bundle, so it behaves the
  same on every OS).
* A host allowlist (pirateface.co and huggingface.co plus their subdomains by
  default). Redirects are followed by hand and every hop is re-checked, so an
  allowed host cannot bounce us somewhere else.
* No credentials in URLs, no non-default ports.
* A hard byte cap on every response, so a hostile server cannot fill memory.
* `post_json` (a rescue request) follows the same rules and never follows a redirect.
* The same network pin as torrent traffic (`pin`): with `bind_interface` set,
  every connection leaves from that interface or address, and none is made
  while it is down. Name lookups still use the system resolver.

Torrent *payload* traffic from peers is handled by libtorrent and is protected
differently: by infohash pinning plus SHA-256 checks against Hugging Face.
Pieces from Hugging Face are checked against the torrent's own piece hashes
before libtorrent sees them. See DESIGN.md, "Trust model".
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import sys
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
    def __init__(self, url: str, status: int | None, msg: str, retry_after: float | None = None,
                 headers=None):
        super().__init__(f"{url}: {msg}")
        self.url = url
        self.status = status
        self.retry_after = retry_after   # seconds, from a 429/503's Retry-After header
        # The error reply's headers, lower-cased (Hugging Face says why in X-Error-Code/-Message).
        self.headers = {k.lower(): v for k, v in headers.items()} if headers else {}


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

# ---------------------------------------------------------------- network pin

_PIN = ""   # Config.bind_interface: an interface name (wg0, tun0) or an IPv4 address


def pin(value: str) -> None:
    """Send every request from `value` (an interface name or IPv4 address), as libtorrent's
    outgoing_interfaces does for torrent traffic. "" = any interface. While the interface is
    down, requests fail instead of leaving through another one."""
    global _PIN
    _PIN = (value or "").strip()


def pinned_to() -> str:
    return _PIN


def _pin_address(value: str) -> str | None:
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value):
        return value
    from .netinfo import interface_address

    return interface_address(value)


def pinned_connection(address: tuple[str, int], timeout=None, source_address=None) -> socket.socket:
    """socket.create_connection, leaving from the pinned interface. Binding the source address
    fails while the address is gone (VPN down), so nothing ever leaves another way. On Linux an
    interface name is also bound with SO_BINDTODEVICE where allowed (as libtorrent does)."""
    value = _PIN
    src = _pin_address(value)
    if not src:
        raise OSError(f"the pinned network interface {value!r} is down or has no IPv4 address")
    host, port = address
    err: OSError | None = None
    for family, kind, proto, _, sa in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        s = socket.socket(family, kind, proto)
        try:
            if src != value and sys.platform.startswith("linux") and hasattr(socket, "SO_BINDTODEVICE"):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, value.encode())
                except OSError:
                    pass   # needs CAP_NET_RAW; the source address below still pins the route
            s.bind((src, 0))
            if timeout is not None and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                s.settimeout(timeout)
            s.connect(sa)
            return s
        except OSError as e:
            err = e
            s.close()
    raise err or OSError(f"no IPv4 address for {host}")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._create_connection = pinned_connection


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


_PINNED_OPENER = urllib.request.build_opener(_NoRedirect(), _PinnedHTTPSHandler(context=_SSL))


def _opener():
    return _PINNED_OPENER if _PIN else _OPENER


def _retry_after(headers) -> float | None:
    v = headers.get("Retry-After") if headers is not None else None
    return float(v) if v and str(v).strip().isdigit() else None


def fetch(
    url: str,
    *,
    max_bytes: int,
    allowed: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS,
    allow_file: bool = False,
    timeout: float = 30.0,
    accept: str | None = None,
    headers: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
) -> Response:
    """GET `url` under the rules above. Raises FetchError on HTTP errors. `auth` = (host,
    Authorization value) is sent only to that exact host, never to a host a redirect leads to."""
    for hop in range(MAX_REDIRECTS + 1):
        # A local file is allowed only as the URL asked for, never as where a server redirects us.
        parts = check_url(url, allowed, allow_file and hop == 0)
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
        if auth and (parts.hostname or "").lower() == auth[0]:
            req.add_header("Authorization", auth[1])
        try:
            resp = _opener().open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc:
                    raise FetchError(url, e.code, "redirect without Location") from e
                url = urllib.parse.urljoin(url, loc)
                continue
            raise FetchError(url, e.code, f"HTTP {e.code}", _retry_after(e.headers), e.headers) from e
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


def post_json(url: str, body: dict, *, max_bytes: int = 64 << 10,
              allowed: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS, timeout: float = 30.0) -> Response:
    """POST a JSON body under the same rules as fetch. Redirects aren't followed: a request that
    sends something is never re-sent somewhere else. Raises FetchError on HTTP errors."""
    check_url(url, allowed)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        resp = _opener().open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise FetchError(url, e.code, f"HTTP {e.code}", _retry_after(e.headers), e.headers) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(url, None, str(e)) from e
    with resp:
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise FetchError(url, resp.status, f"response exceeds cap {max_bytes}")
        return Response(url, resp.status, {k.lower(): v for k, v in resp.headers.items()}, data)


def fetch_json(url: str, *, max_bytes: int = 8 << 20, **kw):
    return fetch(url, max_bytes=max_bytes, accept="application/json", **kw).json()


_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


def fetch_range(
    url: str,
    start: int,
    length: int,
    *,
    allowed: tuple[str, ...],
    timeout: float = 60.0,
    auth: tuple[str, str] | None = None,
    on_chunk=None,
) -> tuple[bytes, str]:
    """GET bytes [start, start + length) of `url` under the rules above.

    The reply must be exactly that range (206 with a matching Content-Range, or a 200 of
    exactly `length` bytes when the range is the whole file); anything else is a FetchError
    and nothing is returned. `auth` = (host, Authorization value) is sent only to that exact
    host, never to a host a redirect leads to. `on_chunk(n)` is called as bytes arrive (a
    rate limit can sleep there). Returns the bytes and the URL that served them."""
    if length <= 0:
        return b"", url
    for _ in range(MAX_REDIRECTS + 1):
        parts = check_url(url, allowed)
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Range", f"bytes={start}-{start + length - 1}")
        req.add_header("Accept-Encoding", "identity")
        if auth and (parts.hostname or "").lower() == auth[0]:
            req.add_header("Authorization", auth[1])
        try:
            resp = _opener().open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc:
                    raise FetchError(url, e.code, "redirect without Location") from e
                url = urllib.parse.urljoin(url, loc)
                continue
            raise FetchError(url, e.code, f"HTTP {e.code}", _retry_after(e.headers), e.headers) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FetchError(url, None, str(e)) from e

        with resp:
            if resp.status == 206:
                m = _CONTENT_RANGE.fullmatch((resp.headers.get("Content-Range") or "").strip())
                if not m or int(m.group(1)) != start or int(m.group(2)) != start + length - 1:
                    raise FetchError(url, resp.status, f"wrong range in reply: {resp.headers.get('Content-Range')!r}")
            elif resp.status == 200:
                declared = resp.headers.get("Content-Length")
                if start != 0 or not (declared and declared.isdigit() and int(declared) == length):
                    raise FetchError(url, resp.status, "the server ignored the byte range")
            else:
                raise FetchError(url, resp.status, f"HTTP {resp.status}")
            buf = bytearray()
            try:
                while len(buf) < length:
                    chunk = resp.read(min(256 << 10, length - len(buf)))
                    if not chunk:
                        break
                    buf += chunk
                    if on_chunk:
                        on_chunk(len(chunk))
                extra = resp.read(1)
            except (TimeoutError, OSError, http.client.HTTPException) as e:
                raise FetchError(url, resp.status, f"read failed after {len(buf)} bytes: {e}") from e
            if len(buf) != length or extra:
                raise FetchError(url, resp.status, f"expected {length} bytes, got {len(buf)}{'+' if extra else ''}")
            return bytes(buf), url
    raise FetchError(url, None, "too many redirects")


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
