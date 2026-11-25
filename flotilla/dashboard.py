"""Web dashboard: http://127.0.0.1:7421

Watch the node and steer it (space, speeds, pause, replan, skip a model).

Security, since anything on localhost is reachable from a browser tab:
* By default it listens on 127.0.0.1 only.
* The Host header must be 127.0.0.1:<port> or localhost:<port>. That defeats
  DNS rebinding (a hostile site re-pointing its own name at 127.0.0.1).
* Every POST needs the per-process token, which is embedded only in the page
  itself. Other origins can't read the page (same-origin policy), so they can't
  get the token. POSTs must also be JSON and, if an Origin header is present,
  it must be ours.
* No CORS headers, ever.

Reachable from other computers (dashboard.access = "lan", "tailnet" or "public"; or `flotilla
dashboard --serve --listen 0.0.0.0`, for a node in a VPN container): then every request also needs
the dashboard's access key. Opening the address with ?key=... once sets a cookie (HttpOnly,
SameSite=Lax) and drops the key from the URL. The cookie holds a value derived from the key, not
the key, under a name with this dashboard's port in it: browsers send a host's cookies to every
port on it. Any Host is accepted then; the key is what keeps others out, and a rebinding site
never gets the cookie.

Anyone who can connect can hold a connection open before any key is checked, so at most
MAX_CONNECTIONS are open at once (more are closed at once), and each request must arrive in full,
request line, headers and body, within REQUEST_TIMEOUT of when the server started waiting for it.

Unless access is "public", connections from public internet addresses are dropped before a byte is
read or sent (see access.py): not even the "needs its access key" page, which would tell a scanner
this address runs Flotilla.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .access import allows
from .feed import REPO_RE
from .history import RANGES

MAX_BODY = 16 * 1024
REQUEST_TIMEOUT = 30.0       # for a whole request (line, headers, body), and for each response
MAX_CONNECTIONS = 32         # a few browser tabs on a few devices; a browser opens up to 6 per site
COOKIE = "flotilla_key"      # before: the key itself, sent to every port on the host. Accepted once, then replaced
COOKIE_PERIOD = 30 * 86400   # the cookie's value changes this often; the previous period's still counts
LOCAL_ONLY = "Only reachable from this computer."
NETWORK = "Reachable from your network with this dashboard's access key."
TAILNET = "Reachable from your Tailscale devices with this dashboard's access key."
PUBLIC = ("<strong style='color:#d29922'>Public: anyone on the internet can reach this page. Only its access "
          "key keeps them out.</strong>")
NEED_KEY = (b"<!doctype html><meta charset=utf-8><title>Flotilla Face</title><body style='font:15px system-ui;"
            b"margin:3em'><h1 style='font-size:18px'>This dashboard needs its access key</h1><p>On the "
            b"computer running it, <code>flotilla dashboard --url</code> prints the address to open.</p>")

# Set on the node container inside a VPN container's network (containers.node_command). Its own dashboard
# would listen on the loopback it shares with every container on that network (other torrent clients,
# *arr apps) with no key, since loopback is "local". The keyed dashboard container serves it instead,
# through the state folder (remote.py), which the node serves whether or not it has a dashboard.
NO_DASHBOARD_ENV = "FLOTILLA_NO_DASHBOARD"


class DashboardOff(OSError):
    """This process mustn't serve a dashboard (NO_DASHBOARD_ENV). An OSError, so `flotilla run` logs it
    as a dashboard that didn't start and seeds on."""


def _same(given: str, want: str) -> bool:
    """Constant-time comparison that can't raise (compare_digest refuses non-ASCII str)."""
    return secrets.compare_digest(given.encode("utf-8", "replace"), want.encode("utf-8", "replace"))


class _Server(ThreadingHTTPServer):
    """At most MAX_CONNECTIONS handler threads (and sockets) at once; more are closed unanswered, so
    nobody can use up this process's threads or file descriptors (in `flotilla run`, the node's)."""
    daemon_threads = True
    block_on_close = False       # stopping never waits for a slow client

    def __init__(self, address, handler, max_connections: int = MAX_CONNECTIONS):
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:    # the thread didn't start
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        if isinstance(sys.exception(), (ConnectionError, TimeoutError)):
            return               # the visitor went away, or was too slow: nothing worth a traceback
        super().handle_error(request, client_address)


class _DeadlineReader(io.RawIOBase):
    """The socket's reads, none waiting past the handler's deadline. A timeout per read alone would let
    a client send a byte every few seconds and keep its thread forever."""

    def __init__(self, sock, handler):
        self._sock, self._handler = sock, handler

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        left = self._handler.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the request took too long")
        self._sock.settimeout(left)
        try:
            return self._sock.recv_into(b)
        finally:
            self._sock.settimeout(REQUEST_TIMEOUT)     # what writing the answer gets


class Dashboard:
    def __init__(self, engine, port: int, host: str = "127.0.0.1", key: str | None = None,
                 access: str = "local"):
        from .remote import RemoteEngine

        if os.environ.get(NO_DASHBOARD_ENV) == "1" and not isinstance(engine, RemoteEngine):
            raise DashboardOff(f"{NO_DASHBOARD_ENV}=1: the dashboard container serves this node's dashboard")
        self.engine = engine
        self.host = host
        self.key = key
        self.access = access
        self.token = secrets.token_urlsafe(24)
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self._page_file = resources.files("flotilla").joinpath("static/dashboard.html")
        self._page = self._render(self._page_file.read_text())
        # FLOTILLA_DASHBOARD_DEV=1 re-reads the page on every load, for UI work on a live node.
        self.dev = os.environ.get("FLOTILLA_DASHBOARD_DEV") == "1"
        handler = self._make_handler()
        self.httpd = _Server((host, port), handler)
        # Dropped without a reply: a visitor from the internet learns nothing, not even that this is Flotilla.
        self.httpd.verify_request = lambda request, client: allows(client[0], self.access)
        self.port = self.httpd.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.cookie_name = f"{COOKIE}_{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="dashboard", daemon=True)

    def _render(self, html: str) -> bytes:
        html = html.replace("__FLOTILLA_TOKEN__", self.token)
        if self.key:
            html = html.replace(LOCAL_ONLY, {"public": PUBLIC, "tailnet": TAILNET}.get(self.access, NETWORK))
        return html.encode()

    @property
    def page(self) -> bytes:
        if self.dev:
            return self._render(self._page_file.read_text())
        return self._page

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("127.0.0.1", "0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}" + (f"/?key={self.key}" if self.key else "")

    def cookie_value(self, period: int | None = None) -> str:
        """What the cookie holds. Derived from the key, so the key itself never goes into a cookie (which
        the browser also sends to every other port on this host), and changing every COOKIE_PERIOD, so a
        copied cookie stops working within two periods unless it's used meanwhile. Not a random session
        per process: the dashboard container restarts with each image and would log everyone out."""
        if period is None:
            period = int(time.time() // COOKIE_PERIOD)
        return hmac.new(self.key.encode(), f"flotilla dashboard cookie {period}".encode(), hashlib.sha256).hexdigest()

    def set_cookie(self) -> list[tuple[str, str]]:
        # SameSite=Lax, not Strict: a key link opened from another site (a chat, a notes app) is a
        # cross-site navigation, and with Strict the browser withholds the cookie on the redirect that
        # follows, landing on "needs its access key". Lax sends it only on top-level GETs, which only
        # read; every POST still needs the page's token and a same-origin Origin.
        return [("Set-Cookie", f"{self.cookie_name}={self.cookie_value()}; Path=/; HttpOnly; SameSite=Lax; "
                               f"Max-Age={2 * COOKIE_PERIOD}"),
                ("Set-Cookie", f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")]   # the old raw-key one

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _make_handler(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "flotilla"
            sys_version = ""
            # A connection that sends nothing is closed after this long, so idle or half-sent
            # requests (from anyone on the network, before the key is checked) can't pile up threads.
            timeout = REQUEST_TIMEOUT

            def log_message(self, *args):  # keep the terminal quiet
                pass

            def setup(self):
                super().setup()
                plain, self.rfile = self.rfile, io.BufferedReader(_DeadlineReader(self.connection, self))
                plain.close()    # else the socket's fd stays open until garbage collection
                self.deadline = time.monotonic() + REQUEST_TIMEOUT

            def handle_one_request(self):
                # Each request, from waiting for its first line to the end of its body, gets
                # REQUEST_TIMEOUT in all; an idle keep-alive connection is closed after that too.
                self.deadline = time.monotonic() + REQUEST_TIMEOUT
                self.refresh_cookie = False
                try:
                    super().handle_one_request()
                except (ConnectionError, TimeoutError):
                    self.close_connection = True

            def _send(self, code: int, body: bytes, ctype: str = "application/json", headers=()) -> None:
                self.send_response(code)
                if getattr(self, "refresh_cookie", False):
                    headers = [*headers, *dash.set_cookie()]
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                 "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                 "connect-src 'self'; frame-ancestors 'none'")
                try:
                    self.end_headers()
                    self.wfile.write(body)
                except (ConnectionError, TimeoutError):   # the visitor left, or stopped reading
                    self.close_connection = True

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj).encode())

            def _host_ok(self) -> bool:
                if dash.key:   # on the network: the access key keeps others out, whatever the name
                    return bool(self.headers.get("Host"))
                return self.headers.get("Host", "") in dash.allowed_hosts

            def _key_ok(self) -> bool:
                """The key in X-Flotilla-Key, or this dashboard's cookie (this period's or the last one's),
                or the old cookie holding the key itself. The last two are replaced in the answer."""
                if not dash.key:
                    return True
                given = self.headers.get("X-Flotilla-Key", "")
                if given:
                    return _same(given, dash.key)
                try:
                    jar = SimpleCookie(self.headers.get("Cookie", ""))
                except Exception:
                    return False
                if dash.cookie_name in jar:
                    value, now = jar[dash.cookie_name].value, int(time.time() // COOKIE_PERIOD)
                    if _same(value, dash.cookie_value(now)):
                        return True
                    if _same(value, dash.cookie_value(now - 1)):
                        self.refresh_cookie = True
                        return True
                if COOKIE in jar and _same(jar[COOKIE].value, dash.key):
                    self.refresh_cookie = True
                    return True
                return False

            def do_GET(self):
                if not self._host_ok():
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
                path = self.path.split("?", 1)[0]
                if dash.key:
                    q = urllib.parse.parse_qs(self.path.partition("?")[2])
                    if path == "/" and "key" in q:
                        if _same(q["key"][0], dash.key):
                            return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain",
                                              [("Location", "/"), *dash.set_cookie()])
                        return self._send(HTTPStatus.UNAUTHORIZED, NEED_KEY, "text/html; charset=utf-8")
                    if not self._key_ok():
                        if path.startswith("/api/"):
                            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "needs the access key"})
                        return self._send(HTTPStatus.UNAUTHORIZED, NEED_KEY, "text/html; charset=utf-8")
                if path in ("/", "/index.html"):
                    return self._send(HTTPStatus.OK, dash.page, "text/html; charset=utf-8")
                if path == "/api/state":
                    try:
                        return self._json(HTTPStatus.OK, dash.engine.snapshot())
                    except Exception as e:  # never kill the server over one bad snapshot
                        return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
                if path == "/api/history":
                    q = urllib.parse.parse_qs(self.path.partition("?")[2])
                    range_ = q.get("range", ["24h"])[0]
                    repo = q.get("repo", [""])[0] or None
                    try:
                        tz = int(q.get("tz", ["0"])[0])
                    except ValueError:
                        tz = 0
                    if range_ not in RANGES or (repo and not REPO_RE.match(repo)) or abs(tz) > 14 * 60:
                        return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad range, repo or tz"})
                    try:
                        return self._json(HTTPStatus.OK, dash.engine.history.query(range_, repo, tz * 60))
                    except Exception as e:
                        return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self):
                if not self._host_ok():
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
                origin = self.headers.get("Origin")
                # With a key, https too: `tailscale serve` (which setup suggests for tailnet access) and
                # other TLS proxies pass on the browser's https:// Origin with the Host it asked for.
                host = self.headers.get("Host")
                ours = ({f"http://{host}", f"https://{host}"} if dash.key
                        else {f"http://{h}" for h in dash.allowed_hosts})
                if origin is not None and origin not in ours:
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "bad origin"})
                if not self._key_ok():
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "needs the access key"})
                if not _same(self.headers.get("X-Flotilla-Token", ""), dash.token):
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON only"})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    n = -1
                if not 0 <= n <= MAX_BODY:
                    return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
                data = self.rfile.read(n)
                if len(data) < n:            # the client hung up mid-body
                    self.close_connection = True
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "body cut short"})
                try:
                    body = json.loads(data or b"{}")
                except (ValueError, RecursionError):     # bad JSON, bad UTF-8, or nested too deep
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad JSON"})
                if not isinstance(body, dict):
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "the body must be a JSON object"})
                action = self.path.split("?", 1)[0].removeprefix("/api/")
                try:
                    msg = dash.engine.submit(action, body)
                except ValueError as e:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                except Exception as e:   # a disk error saving the settings, say: answer, don't drop the connection
                    return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"failed: {e}"[:300]})
                return self._json(HTTPStatus.OK, {"ok": True, "message": msg})

        return Handler
