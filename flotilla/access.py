"""Who can reach the dashboard: this machine, your home network, your Tailscale devices, or anyone.

The dashboard steers the node, and even its "needs its access key" page tells a visitor this address
runs Flotilla. So unless you chose `public` (setup makes you type it), the dashboard drops connections
from public internet addresses before answering, however its port got exposed: a VPS, a hand-written
`docker run -p`, a router port forwarded by mistake. The access key is the second line of defense.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
from pathlib import Path

ACCESS_CHOICES: tuple[str, ...] = ("local", "lan", "tailnet", "public")

# Tailscale (and carrier-grade NAT) addresses; Python counts them neither private nor public.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def allows(client: str, access: str) -> bool:
    """May a connection from this address reach the dashboard? Everything but public internet
    addresses, unless access is "public"."""
    if access == "public":
        return True
    try:
        a = ipaddress.ip_address(client.split("%")[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.is_loopback or a.is_private or a.is_link_local:
        return True
    return a in (_CGNAT if a.version == 4 else _TAILSCALE_V6)


def listen_for(access: str) -> tuple[str, bool]:
    """Where a dashboard on this machine listens, and whether it needs the access key.
    tailnet listens on 127.0.0.1 too: `tailscale serve` passes your tailnet's requests to it."""
    if access == "local":
        return "127.0.0.1", False
    if access == "tailnet":
        return "127.0.0.1", True
    return "0.0.0.0", True


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def tailscale_name() -> str | None:
    """This machine's Tailscale name (media-server.tail1234.ts.net) while Tailscale is up."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
        doc = json.loads(out.stdout) if out.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    name = ((doc.get("Self") or {}).get("DNSName") or "").rstrip(".")
    return name if doc.get("BackendState") == "Running" and name else None


def tailscale_serving(port: int) -> bool | None:
    """Is `tailscale serve` passing https://<this machine>:<port> on to the dashboard? None if we can't tell."""
    try:
        out = subprocess.run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=5)
        doc = json.loads(out.stdout or "{}") if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if doc is None:
        return None
    for where, web in (doc.get("Web") or {}).items():
        if where.endswith(f":{port}") and any(f":{port}" in (h.get("Proxy") or "")
                                               for h in (web.get("Handlers") or {}).values()):
            return True
    return False


def serve_command(port: int) -> str:
    """Puts the dashboard on your tailnet, with HTTPS: https://<machine>.<tailnet>.ts.net:<port>."""
    return f"tailscale serve --bg --https={port} http://127.0.0.1:{port}"


# Shown whenever a public dashboard starts, however it was made public (setup, the config file, a flag).
PUBLIC_WARNING = ("the dashboard is PUBLIC: anyone on the internet can reach it, and only its access key keeps "
                  "them out. Internet scanners will find it. To stop, set access under [dashboard] in the config "
                  "to local, lan or tailnet (or run `flotilla init` again).")

PLACEHOLDER = "<this computer's address>"
PUBLIC_PLACEHOLDER = "<this server's public address>"
# Set on a dashboard container by the host's setup: who it lets in, and its address as the host sees
# it (a container can't see the host's home-network address or Tailscale name).
ACCESS_ENV, URL_ENV = "FLOTILLA_DASHBOARD_ACCESS", "FLOTILLA_DASHBOARD_URL"


def address(access: str, port: int, key: str | None, lan: str | None = None, tailnet: str | None = None,
            public: str | None = None) -> str:
    """The address to open for a dashboard with this access."""
    q = f"/?key={key}" if key else ""
    if access == "tailnet" and tailnet:
        return f"https://{tailnet}:{port}{q}"
    if access in ("local", "tailnet"):
        return f"http://127.0.0.1:{port}{q}"
    if access == "lan" and lan:
        return f"http://{lan}:{port}{q}"
    if access == "public":
        return f"http://{public or PUBLIC_PLACEHOLDER}:{port}{q}"
    return f"http://{PLACEHOLDER}:{port}{q}"


def public_address() -> str | None:
    """This machine's own public IPv4 address, when it has one (a VPS). Behind a home router it
    doesn't, and finding the router's would mean asking an outside service, which Flotilla doesn't."""
    from .netinfo import internet_route

    route = internet_route()
    if not route or not route.address or route.looks_like_vpn:
        return None
    try:
        return route.address if ipaddress.ip_address(route.address).is_global else None
    except ValueError:
        return None


def base_address(access: str, port: int) -> str:
    """The dashboard's address without its key, as seen from where it's meant to be opened."""
    given = os.environ.get(URL_ENV)
    if given:
        return given.rstrip("/")
    if in_container():      # the container's own addresses mean nothing to your browser
        return address("public" if access == "public" else "lan", port, None)
    from .netinfo import lan_address

    return address(access, port, None, lan=lan_address() if access == "lan" else None,
                   tailnet=tailscale_name() if access == "tailnet" else None,
                   public=public_address() if access == "public" else None)


def full_address(access: str, port: int, key: str | None) -> str:
    return base_address(access, port) + (f"/?key={key}" if key else "")


def describe(access: str) -> str:
    return {"local": "this machine only", "lan": "your home network", "tailnet": "your Tailscale devices",
            "public": "anyone on the internet, with the access key"}[access]


_KEY = re.compile(r"[?&]key=([\w-]+)")


def key_in(url: str) -> str | None:
    m = _KEY.search(url)
    return m.group(1) if m else None
