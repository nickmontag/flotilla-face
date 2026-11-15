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

Reachable from other computers (`flotilla dashboard --serve --listen 0.0.0.0`, for a node in a
VPN container): then every request also needs the dashboard's access key. Opening the address
with ?key=... once sets it as a cookie (HttpOnly, SameSite=Strict) and drops it from the URL.
Any Host is accepted then; the key is what keeps others out, and a rebinding site never gets
the cookie.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .feed import REPO_RE
from .history import RANGES

MAX_BODY = 16 * 1024
COOKIE = "flotilla_key"
LOCAL_ONLY = "Only reachable from this computer."
NETWORK = "Reachable from your network with this dashboard's access key."
NEED_KEY = (b"<!doctype html><meta charset=utf-8><title>Flotilla Face</title><body style='font:15px system-ui;"
            b"margin:3em'><h1 style='font-size:18px'>This dashboard needs its access key</h1><p>On the "
            b"computer running it, <code>flotilla dashboard --url</code> prints the address to open.</p>")


class Dashboard:
    def __init__(self, engine, port: int, host: str = "127.0.0.1", key: str | None = None):
        self.engine = engine
        self.host = host
        self.key = key
        self.token = secrets.token_urlsafe(24)
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self._page_file = resources.files("flotilla").joinpath("static/dashboard.html")
        self._page = self._render(self._page_file.read_text())
        # FLOTILLA_DASHBOARD_DEV=1 re-reads the page on every load, for UI work on a live node.
        self.dev = os.environ.get("FLOTILLA_DASHBOARD_DEV") == "1"
        handler = self._make_handler()
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="dashboard", daemon=True)

    def _render(self, html: str) -> bytes:
        html = html.replace("__FLOTILLA_TOKEN__", self.token)
        if self.key:
            html = html.replace(LOCAL_ONLY, NETWORK)
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

            def log_message(self, *args):  # keep the terminal quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json", headers=()) -> None:
                self.send_response(code)
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
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj).encode())

            def _host_ok(self) -> bool:
                if dash.key:   # on the network: the access key keeps others out, whatever the name
                    return bool(self.headers.get("Host"))
                return self.headers.get("Host", "") in dash.allowed_hosts

            def _key_ok(self) -> bool:
                if not dash.key:
                    return True
                given = self.headers.get("X-Flotilla-Key", "")
                if not given:
                    try:
                        jar = SimpleCookie(self.headers.get("Cookie", ""))
                        given = jar[COOKIE].value if COOKIE in jar else ""
                    except Exception:
                        given = ""
                return bool(given) and secrets.compare_digest(given, dash.key)

            def do_GET(self):
                if not self._host_ok():
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
                path = self.path.split("?", 1)[0]
                if dash.key:
                    q = urllib.parse.parse_qs(self.path.partition("?")[2])
                    if path == "/" and "key" in q:
                        if secrets.compare_digest(q["key"][0], dash.key):
                            cookie = f"{COOKIE}={dash.key}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
                            return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain",
                                              [("Location", "/"), ("Set-Cookie", cookie)])
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
                ours = {f"http://{self.headers.get('Host')}"} if dash.key else {f"http://{h}" for h in dash.allowed_hosts}
                if origin is not None and origin not in ours:
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "bad origin"})
                if not self._key_ok():
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "needs the access key"})
                if not secrets.compare_digest(self.headers.get("X-Flotilla-Token", ""), dash.token):
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON only"})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    n = -1
                if not 0 <= n <= MAX_BODY:
                    return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad JSON"})
                action = self.path.split("?", 1)[0].removeprefix("/api/")
                try:
                    msg = dash.engine.submit(action, body)
                except ValueError as e:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                return self._json(HTTPStatus.OK, {"ok": True, "message": msg})

        return Handler
