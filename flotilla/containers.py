"""VPN containers (gluetun and the like) running on this machine, and how to run Flotilla inside one.

A media server often keeps its VPN in a Docker container: gluetun, with the torrent client sharing
its network. The machine's own traffic then goes out the normal way, so looking at its routes finds
no VPN. Docker can tell us about the container, and Flotilla can run inside its network the same
way the torrent client does (README, "Behind gluetun").
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field

SOURCE = "https://github.com/nickmontag/flotilla-face/archive/main.tar.gz"
IMAGE = "python:3.13-slim"
VOLUME = "flotilla"
NODE, DASHBOARD = "flotilla", "flotilla-dashboard"

# Images that are a VPN client for the containers sharing their network. Ones with "vpn" in the name
# usually are too (binhex/arch-qbittorrentvpn, haugene/transmission-openvpn), except VPN *servers*.
_CLIENT_IMAGE = re.compile(r"(^|/)(gluetun|private-internet-access|openvpn-client|wireguard-client|"
                           r"docker-wireguard-pia|[\w.-]*vpn)$")
_SERVER_IMAGE = re.compile(r"(wg-easy|kylemanna/openvpn|openvpn-as|pivpn|firezone|headscale|netbird|"
                           r"tailscale|zerotier|softether|ipsec-vpn-server)")
# Ambiguous images (client or server): only a VPN client when other containers route through them.
_MAYBE_IMAGE = re.compile(r"(^|/)(wireguard|openvpn|[\w.-]*wireguard[\w.-]*)$")


@dataclass
class VpnContainer:
    name: str
    image: str
    health: str = ""                       # "healthy", "unhealthy", "starting", or "" (no healthcheck)
    shared_by: list[str] = field(default_factory=list)

    @property
    def network(self) -> str:
        return f"container:{self.name}"


def _repo(image: str) -> str:
    """'ghcr.io/qdm12/gluetun:v3' -> 'qdm12/gluetun'; 'qmcgaw/gluetun@sha256:..' -> 'qmcgaw/gluetun'."""
    image = image.lower().split("@")[0]
    parts = image.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    parts[-1] = parts[-1].split(":")[0]
    return "/".join(parts)


def is_vpn_client(image: str, name: str, shared: bool) -> bool:
    repo = _repo(image)
    if _SERVER_IMAGE.search(repo):
        return False
    if _CLIENT_IMAGE.search(repo) or "gluetun" in name.lower():
        return True
    return shared and bool(_MAYBE_IMAGE.search(repo) or re.search(r"vpn|wireguard", name, re.I))


def _docker(*args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None


class DockerUnavailable(Exception):
    """Docker is installed but we can't talk to it (no permission, daemon down)."""


def vpn_containers() -> list[VpnContainer]:
    """Running VPN-client containers, best first (healthy, then the most containers routed through
    them). [] when Docker isn't installed; DockerUnavailable when it is but won't answer."""
    if not shutil.which("docker"):
        return []
    ps = _docker("ps", "-q", "--no-trunc")
    if ps is None or ps.returncode != 0:
        why = (ps.stderr.strip().splitlines() or ["no answer"])[-1] if ps else "no answer"
        raise DockerUnavailable(why)
    ids = ps.stdout.split()
    if not ids:
        return []
    out = _docker("inspect", *ids)
    if out is None or out.returncode != 0:
        return []
    try:
        docs = json.loads(out.stdout)
    except ValueError:
        return []
    rows = []
    for d in docs:
        state = d.get("State") or {}
        rows.append((d.get("Id", ""), d.get("Name", "").lstrip("/"), (d.get("Config") or {}).get("Image", ""),
                     (d.get("HostConfig") or {}).get("NetworkMode", ""), (state.get("Health") or {}).get("Status", "")))
    # A container sharing another's network has NetworkMode "container:<id or name>".
    users: dict[str, list[str]] = {}
    by_key = {k: cid for cid, name, *_ in rows for k in (cid, name)}
    for _, name, _, mode, _ in rows:
        if mode.startswith("container:"):
            target = mode.split(":", 1)[1]
            cid = by_key.get(target) or next((c for c in by_key.values() if c.startswith(target)), None)
            if cid:
                users.setdefault(cid, []).append(name)
    found = [VpnContainer(name, image, health, sorted(users.get(cid, [])))
             for cid, name, image, _, health in rows
             if is_vpn_client(image, name, bool(users.get(cid)))]
    found.sort(key=lambda c: (c.health == "unhealthy", -len(c.shared_by), c.name))
    return found


def existing(names: tuple[str, ...] = (NODE, DASHBOARD)) -> list[str]:
    """Which of these container names are already taken (running or not)."""
    out = _docker("ps", "-a", "--format", "{{.Names}}")
    taken = set(out.stdout.split()) if out and out.returncode == 0 else set()
    return [n for n in names if n in taken]


def _pip_then(cmd: str) -> str:
    return f"pip install -q --root-user-action=ignore {SOURCE} && {cmd}"


def setup_command(vpn: VpnContainer, volume: str = VOLUME) -> list[str]:
    """`flotilla init` inside the VPN container's network: it sees the tunnel as the default route."""
    return ["docker", "run", "-it", "--rm", "--network", vpn.network, "-v", f"{volume}:/root", IMAGE,
            "sh", "-c", _pip_then("flotilla init")]


_LOGS = ["--log-opt", "max-size=10m", "--log-opt", "max-file=3"]


def node_command(vpn: VpnContainer, volume: str = VOLUME, memory: str = "3g") -> list[str]:
    """The seeding node, every connection through the VPN container."""
    return ["docker", "run", "-d", "--name", NODE, "--network", vpn.network, "--restart", "unless-stopped",
            "--memory", memory, "--memory-swap", memory, *_LOGS, "-v", f"{volume}:/root", IMAGE,
            "sh", "-c", _pip_then("exec flotilla run")]


def dashboard_command(port: int = 7421, volume: str = VOLUME) -> list[str]:
    """The dashboard, outside the VPN so your own network can reach it; it shares only the state folder."""
    return ["docker", "run", "-d", "--name", DASHBOARD, "--restart", "unless-stopped", "-p", f"{port}:{port}",
            *_LOGS, "-v", f"{volume}:/root", IMAGE,
            "sh", "-c", _pip_then(f"exec flotilla dashboard --serve --listen 0.0.0.0 --port {port}")]


def shell(cmd: list[str]) -> str:
    return shlex.join(cmd)


def run(cmd: list[str], interactive: bool = False) -> int:
    """Run a docker command, its output going straight to the terminal."""
    try:
        return subprocess.run(cmd, timeout=None if interactive else 600).returncode
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  docker failed: {e}")
        return 1
