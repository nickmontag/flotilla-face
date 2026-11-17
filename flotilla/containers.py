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
# Set on the setup container, so `flotilla init` inside knows the host already chose where files go.
STORAGE_ENV, VPN_ENV = "FLOTILLA_DOCKER_STORAGE", "FLOTILLA_DOCKER_VPN"

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
    id: str = ""

    @property
    def network(self) -> str:
        return f"container:{self.name}"

    def is_target(self, network_mode: str) -> bool:
        """Is a container with this NetworkMode ("container:<id or name>") on our network?"""
        if not network_mode.startswith("container:"):
            return False
        t = network_mode.split(":", 1)[1]
        return t == self.name or bool(self.id and len(t) >= 12 and self.id.startswith(t))


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
    found = [VpnContainer(name, image, health, sorted(users.get(cid, [])), cid)
             for cid, name, image, _, health in rows
             if is_vpn_client(image, name, bool(users.get(cid)))]
    found.sort(key=lambda c: (c.health == "unhealthy", -len(c.shared_by), c.name))
    return found


@dataclass
class Installed:
    """A container already using one of Flotilla's container names."""
    name: str
    ours: bool                 # Flotilla's own node or dashboard (a previous setup)
    running: bool
    network_mode: str          # the node's: "container:<the VPN container>"
    storage: str               # what's mounted at /root: a volume name or a host folder
    access: str = ""           # the dashboard's: who it lets in (set by setup since dashboard access)


def installed(names: tuple[str, ...] = (NODE, DASHBOARD)) -> dict[str, Installed]:
    """The containers already using Flotilla's names, from an earlier setup or not."""
    out = _docker("inspect", *names)          # exits 1 when some don't exist, still lists the others
    try:
        docs = json.loads(out.stdout) if out and out.stdout.strip() else []
    except ValueError:
        return {}
    found = {}
    for d in docs:
        name = d.get("Name", "").lstrip("/")
        cmd = " ".join((d.get("Config") or {}).get("Cmd") or [])
        root = next((m for m in d.get("Mounts") or [] if m.get("Destination") == "/root"), {})
        storage = root.get("Name") if root.get("Type") == "volume" else root.get("Source", "")
        access = re.search(r"--access (\w+)", cmd)
        found[name] = Installed(name, re.search(r"\bflotilla (run|dashboard)\b", cmd) is not None,
                                bool((d.get("State") or {}).get("Running")),
                                (d.get("HostConfig") or {}).get("NetworkMode", ""), storage or "",
                                access.group(1) if access else "")
    return found


def remove(names: list[str]) -> bool:
    """Remove containers (a previous setup's, to recreate them). Their files stay in the volume or folder."""
    out = _docker("rm", "-f", *names)
    return bool(out) and out.returncode == 0


def dashboard_address(timeout: float = 180.0, every: float = 2.0, lan_address: str | None = None) -> str | None:
    """The dashboard container's address with its access key, once it has started (it installs
    Flotilla first, which takes a minute or so). None if it hasn't said within the timeout."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        out = _docker("logs", DASHBOARD)
        text = (out.stdout + out.stderr) if out else ""
        m = re.search(r"Open (https?://[^\n]*?\?key=\S+)", text)
        if m:
            url = m.group(1)
            return url.replace("<this computer's address>", lan_address) if lan_address else url
        if time.monotonic() >= deadline:
            return None
        time.sleep(every)


def _pip_then(cmd: str) -> str:
    # No pip cache: in a host folder root doesn't own pip can't use one and warns, and each
    # container starts fresh anyway. No version check either: the pip in the image is fine.
    return f"pip install -q --no-cache-dir --disable-pip-version-check --root-user-action=ignore {SOURCE} && {cmd}"


def setup_command(vpn: VpnContainer, volume: str = VOLUME) -> list[str]:
    """`flotilla init` inside the VPN container's network: it sees the tunnel as the default route."""
    return ["docker", "run", "-it", "--rm", "--network", vpn.network, "-v", f"{volume}:/root",
            "-e", f"{STORAGE_ENV}={volume}", "-e", f"{VPN_ENV}={vpn.name}", IMAGE,
            "sh", "-c", _pip_then("flotilla init")]


_LOGS = ["--log-opt", "max-size=10m", "--log-opt", "max-file=3"]


def node_command(vpn: VpnContainer, volume: str = VOLUME, memory: str = "3g") -> list[str]:
    """The seeding node, every connection through the VPN container."""
    return ["docker", "run", "-d", "--name", NODE, "--network", vpn.network, "--restart", "unless-stopped",
            "--memory", memory, "--memory-swap", memory, *_LOGS, "-v", f"{volume}:/root", IMAGE,
            "sh", "-c", _pip_then("exec flotilla run")]


def publish(access: str, port: int, lan: str | None) -> str:
    """Docker's -p for the dashboard. Never every network unless it's public: Docker hands IPv6
    visitors to the container from its own private address, which would get them past the
    dashboard's check for public addresses."""
    if access == "public":
        return f"{port}:{port}"
    if access == "lan" and lan:
        return f"{lan}:{port}:{port}"
    return f"127.0.0.1:{port}:{port}"       # local, and tailnet (`tailscale serve` on this machine)


def dashboard_command(port: int = 7421, volume: str = VOLUME, access: str = "lan",
                      lan: str | None = None, url: str | None = None) -> list[str]:
    """The dashboard, outside the VPN so your own network can reach it; it shares only the state folder.
    url is its address as this machine sees it, for its log and `flotilla dashboard --url`."""
    from .access import ACCESS_ENV, URL_ENV

    env = ["-e", f"{ACCESS_ENV}={access}"] + (["-e", f"{URL_ENV}={url}"] if url else [])
    return ["docker", "run", "-d", "--name", DASHBOARD, "--restart", "unless-stopped",
            "-p", publish(access, port, lan), *env, *_LOGS, "-v", f"{volume}:/root", IMAGE,
            "sh", "-c", _pip_then(f"exec flotilla dashboard --serve --listen 0.0.0.0 --port {port} --access {access}")]


def shell(cmd: list[str]) -> str:
    return shlex.join(cmd)


def run(cmd: list[str], interactive: bool = False) -> int:
    """Run a docker command, its output going straight to the terminal."""
    try:
        return subprocess.run(cmd, timeout=None if interactive else 600).returncode
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  docker failed: {e}")
        return 1
