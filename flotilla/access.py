"""Who can reach the dashboard: this machine, your home network, your Tailscale devices, or anyone.

The dashboard steers the node, and even its "needs its access key" page tells a visitor this address
runs Flotilla. So unless you chose `public` (setup makes you type it), the dashboard drops connections
from public internet addresses before answering, however its port got exposed: a VPS, a hand-written
`docker run -p`, a router port forwarded by mistake. The access key is the second line of defense.
"""

from __future__ import annotations

import ipaddress
import json
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


def serve_command(port: int) -> str:
    """Puts the dashboard on your tailnet, with HTTPS: https://<machine>.<tailnet>.ts.net:<port>."""
    return f"tailscale serve --bg --https={port} http://127.0.0.1:{port}"


# Shown whenever a public dashboard starts, however it was made public (setup, the config file, a flag).
PUBLIC_WARNING = ("the dashboard is PUBLIC: anyone on the internet can reach it, and only its access key keeps "
                  "them out. Internet scanners will find it. To stop, set access under [dashboard] in the config "
                  "to local, lan or tailnet (or run `flotilla init` again).")

PLACEHOLDER = "<this computer's address>"


def address(access: str, port: int, key: str | None, lan: str | None = None, tailnet: str | None = None) -> str:
    """The address to open for a dashboard with this access."""
    q = f"/?key={key}" if key else ""
    if access == "tailnet" and tailnet:
        return f"https://{tailnet}:{port}{q}"
    if access in ("local", "tailnet"):
        return f"http://127.0.0.1:{port}{q}"
    if access == "lan" and lan:
        return f"http://{lan}:{port}{q}"
    host = "<this server's public address>" if access == "public" else PLACEHOLDER
    return f"http://{host}:{port}{q}"


def describe(access: str) -> str:
    return {"local": "this machine only", "lan": "your home network", "tailnet": "your Tailscale devices",
            "public": "anyone on the internet, with the access key"}[access]


_KEY = re.compile(r"[?&]key=([\w-]+)")


def key_in(url: str) -> str | None:
    m = _KEY.search(url)
    return m.group(1) if m else None
