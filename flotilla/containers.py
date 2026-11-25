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

from .vpnport import GLUETUN_FORWARDS, provider_name

VOLUME = "flotilla"
# For trying an unreleased build (`docker build` of your own): use this image instead of the release's.
IMAGE_ENV = "FLOTILLA_IMAGE"
NODE, DASHBOARD = "flotilla", "flotilla-dashboard"
# Set on the setup container, so `flotilla init` inside knows the host already chose where files go.
STORAGE_ENV, VPN_ENV = "FLOTILLA_DOCKER_STORAGE", "FLOTILLA_DOCKER_VPN"
# The VPN container's name, on the node. Docker keeps `--network container:<name>` as the id, which
# is gone once that container is recreated (a compose change, a new image): the name finds the new one.
VPN_LABEL = "flotilla.vpn"

# Images that are a VPN client for the containers sharing their network. Ones with "vpn" in the name
# usually are too (binhex/arch-qbittorrentvpn, haugene/transmission-openvpn), except VPN *servers*.
_CLIENT_IMAGE = re.compile(r"(^|/)(gluetun|private-internet-access|openvpn-client|wireguard-client|"
                           r"docker-wireguard-pia|[\w.-]*vpn)$")
_SERVER_IMAGE = re.compile(r"(wg-easy|kylemanna/openvpn|openvpn-as|pivpn|firezone|headscale|netbird|"
                           r"tailscale|zerotier|softether|ipsec-vpn-server)")
# The settings Flotilla reads from a gluetun container.
GLUETUN_ENV = ("VPN_SERVICE_PROVIDER", "VPN_PORT_FORWARDING", "VPN_PORT_FORWARDING_PROVIDER",
               "HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE", "HTTP_CONTROL_SERVER_ADDRESS")
# Ambiguous images (client or server): only a VPN client when other containers route through them.
_MAYBE_IMAGE = re.compile(r"(^|/)(wireguard|openvpn|[\w.-]*wireguard[\w.-]*)$")


@dataclass
class VpnContainer:
    name: str
    image: str
    health: str = ""                       # "healthy", "unhealthy", "starting", or "" (no healthcheck)
    shared_by: list[str] = field(default_factory=list)
    id: str = ""
    env: dict = field(default_factory=dict)  # its gluetun settings (GLUETUN_ENV), when it's gluetun

    @property
    def is_gluetun(self) -> bool:
        return "gluetun" in _repo(self.image) or any(k.startswith("VPN_SERVICE") for k in self.env)

    def forwarding(self) -> tuple[bool | None, str]:
        """Will this VPN container forward a port to Flotilla? (True/False/None for can't tell, why)"""
        if not self.is_gluetun:
            return None, ""
        provider = (self.env.get("VPN_PORT_FORWARDING_PROVIDER") or self.env.get("VPN_SERVICE_PROVIDER")
                    or "private internet access").strip().lower()
        label = provider_name(provider)
        on = self.env.get("VPN_PORT_FORWARDING", "off").strip().lower() in ("on", "yes", "true", "1")
        if provider not in GLUETUN_FORWARDS:
            return False, (f"{self.name} uses {label}, which forwards no ports, so peers can't start "
                           f"connections to Flotilla (it still uploads over the ones it opens). gluetun "
                           f"forwards ports for {', '.join(GLUETUN_FORWARDS.values())}.")
        if not on:
            return False, (f"{label} can forward a port, but {self.name} doesn't ask for one: add "
                           f"VPN_PORT_FORWARDING=on to its settings and recreate it, then Flotilla listens on it.")
        if not self.env.get("HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE") and provider != "protonvpn":
            # (ProtonVPN's own NAT-PMP gives Flotilla the same port gluetun opened, no key needed.)
            return None, (f"{self.name} forwards a port, but its control server needs a key before it tells "
                          f"Flotilla which: add HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE="
                          f"'{{\"auth\":\"apikey\",\"apikey\":\"<a long random key>\"}}' to it, recreate it, "
                          f"and run setup again.")
        return True, f"{self.name} forwards a port from {label}: Flotilla listens on it, so peers can connect in."

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


def _gluetun_settings(doc: dict) -> dict:
    """The GLUETUN_ENV settings from a container's `docker inspect`."""
    env = dict(e.split("=", 1) for e in (doc.get("Config") or {}).get("Env") or [] if "=" in e)
    return {k: v for k, v in env.items() if k in GLUETUN_ENV}


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
        env = _gluetun_settings(d)
        rows.append((d.get("Id", ""), d.get("Name", "").lstrip("/"), (d.get("Config") or {}).get("Image", ""),
                     (d.get("HostConfig") or {}).get("NetworkMode", ""), (state.get("Health") or {}).get("Status", ""),
                     env))
    # A container sharing another's network has NetworkMode "container:<id or name>".
    users: dict[str, list[str]] = {}
    by_key = {k: cid for cid, name, *_ in rows for k in (cid, name)}
    for _, name, _, mode, _, _ in rows:
        if mode.startswith("container:"):
            target = mode.split(":", 1)[1]
            cid = by_key.get(target) or next((c for c in by_key.values() if c.startswith(target)), None)
            if cid:
                users.setdefault(cid, []).append(name)
    found = [VpnContainer(name, image, health, sorted(users.get(cid, [])), cid, env)
             for cid, name, image, _, health, env in rows
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
    image: str = ""
    args: list[str] = field(default_factory=list)   # what follows `flotilla` in its command
    doc: dict = field(default_factory=dict)         # docker inspect, to recreate it with a new image


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
        found[name] = Installed(name, re.search(r"\bflotilla (run|dashboard)\b", cmd) is not None
                                or flotilla_args(d)[:1] in (["run"], ["dashboard"]),
                                bool((d.get("State") or {}).get("Running")),
                                (d.get("HostConfig") or {}).get("NetworkMode", ""), storage or "",
                                access.group(1) if access else "", (d.get("Config") or {}).get("Image", ""),
                                flotilla_args(d), d)
    return found


def remove(names: list[str]) -> bool:
    """Remove containers (a previous setup's, to recreate them). Their files stay in the volume or folder."""
    out = _docker("rm", "-f", *names)
    return bool(out) and out.returncode == 0


def remove_volume(name: str) -> bool:
    """Remove a Docker volume a setup made, once no container uses it."""
    out = _docker("volume", "rm", name)
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


def setup_command(vpn: VpnContainer, volume: str, image: str) -> list[str]:
    """`flotilla init` inside the VPN container's network: it sees the tunnel as the default route."""
    return ["docker", "run", "-it", "--rm", "--network", vpn.network, "-v", f"{volume}:/root",
            "-e", f"{STORAGE_ENV}={volume}", "-e", f"{VPN_ENV}={vpn.name}", image, "init"]


_LOGS = ["--log-opt", "max-size=10m", "--log-opt", "max-file=3"]


class Command(list):
    """A `docker run` whose secret environment (gluetun's control-server key, a container's own
    settings carried over by an update) stays off its argv, where any local user could read it with
    `ps`, and out of anything printed: `run` hands it to docker in a 0600 --env-file, and `shell` shows
    a placeholder in its place."""

    def __init__(self, args=(), env: dict | None = None, hints: dict | None = None):
        super().__init__(args)
        self.env = dict(env or {})        # NAME -> value
        self.hints = dict(hints or {})    # NAME -> what to put there, in a command printed for you


def _env_line_ok(value: str) -> bool:
    """Can an --env-file carry this value? One line each, so no line breaks."""
    return "\n" not in value and "\r" not in value


def node_command(vpn: VpnContainer, volume: str, image: str, memory: str = "3g") -> Command:
    """The seeding node, every connection through the VPN container. It serves no dashboard of its own:
    that would listen, keyless, on the loopback every container on the VPN's network shares. The
    dashboard container serves it, through the state folder."""
    from .dashboard import NO_DASHBOARD_ENV

    args, env, hints = gluetun_env(vpn)
    return Command(["docker", "run", "-d", "--name", NODE, "--network", vpn.network, "--label",
                    f"{VPN_LABEL}={vpn.name}", "--restart", "unless-stopped",
                    "--memory", memory, "--memory-swap", memory, *_LOGS, *args, "-e", f"{NO_DASHBOARD_ENV}=1",
                    "-v", f"{volume}:/root", image, "run"], env, hints)


def gluetun_env(vpn: VpnContainer) -> tuple[list[str], dict, dict]:
    """What the node needs to ask gluetun's control server for the forwarded port (vpnport.py): its
    key, and its address when it isn't the default. (docker run -e arguments, the secret environment
    for a Command, and placeholders for that in a printed command.)"""
    from .vpnport import GLUETUN_AUTH_ENV, PROVIDER_ENV

    out, env, hints = [], {}, {}
    provider = (vpn.env.get("VPN_PORT_FORWARDING_PROVIDER") or vpn.env.get("VPN_SERVICE_PROVIDER") or "").strip()
    if provider and vpn.is_gluetun:
        out += ["-e", f"{PROVIDER_ENV}={provider.lower()}"]
    role = vpn.env.get("HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE", "")
    if role:
        env[GLUETUN_AUTH_ENV] = role
        hints[GLUETUN_AUTH_ENV] = f"<HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE of {vpn.name}>"
    addr = vpn.env.get("HTTP_CONTROL_SERVER_ADDRESS", "").strip()
    m = re.fullmatch(r"(?:[\w.\[\]:]*):(\d+)", addr)
    if m and m.group(1) != "8000":
        out += ["-e", f"FLOTILLA_GLUETUN_URL=http://127.0.0.1:{m.group(1)}"]
    return out, env, hints


def publish(access: str, port: int, lan: str | None) -> str:
    """Docker's -p for the dashboard. Never every network unless it's public: Docker hands IPv6
    visitors to the container from its own private address, which would get them past the
    dashboard's check for public addresses."""
    if access == "public":
        return f"{port}:{port}"
    if access == "lan" and lan:
        return f"{lan}:{port}:{port}"
    return f"127.0.0.1:{port}:{port}"       # local, and tailnet (`tailscale serve` on this machine)


def dashboard_command(port: int, volume: str, access: str, lan: str | None, url: str | None,
                      image: str) -> list[str]:
    """The dashboard, outside the VPN so your own network can reach it; it shares only the state folder.
    url is its address as this machine sees it, for its log and `flotilla dashboard --url`."""
    from .access import ACCESS_ENV, URL_ENV

    env = ["-e", f"{ACCESS_ENV}={access}"] + (["-e", f"{URL_ENV}={url}"] if url else [])
    return ["docker", "run", "-d", "--name", DASHBOARD, "--restart", "unless-stopped",
            "-p", publish(access, port, lan), *env, *_LOGS, "-v", f"{volume}:/root", image,
            "dashboard", "--serve", "--listen", "0.0.0.0", "--port", str(port), "--access", access]


def release_image() -> tuple[str, str]:
    """The image to run and its version: the newest signed release's, by digest."""
    import os

    from . import release

    if os.environ.get(IMAGE_ENV):
        return os.environ[IMAGE_ENV], "your own build"
    rel = release.latest()
    return rel.image, rel.version


def flotilla_args(doc: dict) -> list[str]:
    """What a Flotilla container runs after `flotilla`: from the image's entrypoint, or from the
    `pip install ... && exec flotilla ...` command of containers set up before there were images."""
    import shlex

    cfg = doc.get("Config") or {}
    if (cfg.get("Entrypoint") or []) == ["flotilla"]:
        return list(cfg.get("Cmd") or [])
    m = re.search(r"\bexec flotilla (.+)$", " ".join(cfg.get("Cmd") or []))
    return shlex.split(m.group(1)) if m else []


def _gluetun_env_now(ref: str) -> tuple[list[str], dict, dict] | None:
    """gluetun_env for the VPN container `ref` as Docker has it now; None when Docker won't say (then
    the node keeps what it had)."""
    out = _docker("inspect", ref)
    try:
        doc = json.loads(out.stdout)[0] if out and out.returncode == 0 else None
    except (ValueError, IndexError, KeyError):
        doc = None
    if not isinstance(doc, dict):
        return None
    vpn = VpnContainer(ref, (doc.get("Config") or {}).get("Image", ""), env=_gluetun_settings(doc))
    return gluetun_env(vpn) if vpn.is_gluetun else ([], {}, {})


def _container_name(ref: str) -> str:
    out = _docker("inspect", "--format", "{{.Name}}", ref)
    return out.stdout.strip().lstrip("/") if out and out.returncode == 0 and out.stdout.strip() else ref


# The python image's own environment, for when the old image can't be inspected: it isn't the
# container's own setting, and the new image brings its own.
_IMAGE_ENV = re.compile(r"PATH|HOME|LANG|LC_ALL|GPG_KEY|PYTHON[A-Z0-9_]*")


def _image_env(doc: dict) -> set[str] | None:
    """The `NAME=value` lines the container's image set (not the container); None if Docker won't say."""
    ref = doc.get("Image") or (doc.get("Config") or {}).get("Image")
    out = _docker("image", "inspect", ref) if ref else None
    try:
        img = json.loads(out.stdout)[0] if out and out.returncode == 0 else None
    except (ValueError, IndexError, KeyError, TypeError):
        img = None
    if not isinstance(img, dict):
        return None
    return {e for e in (img.get("Config") or {}).get("Env") or [] if isinstance(e, str)}


def _mount_arg(fields: list[str]) -> str:
    """--mount's value is CSV, so a path with a comma or a quote gets quoted."""
    import csv
    import io

    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(fields)
    return buf.getvalue()


def _carried(d: dict, name: str) -> list[str]:
    """What else a container was started with (other folders, user, ports, devices...) as `docker run`
    arguments, so an update doesn't drop it. DockerError for anything it can't carry over safely."""
    hc, cfg = d.get("HostConfig") or {}, d.get("Config") or {}
    redo = ("which an update can't carry over. Recreate it yourself on the new image (`flotilla update "
            "--check` names it), or set it up again with `flotilla init`.")
    if any(k.startswith("com.docker.compose.") for k in cfg.get("Labels") or {}):
        raise DockerError(f"{name} is managed by Docker Compose: update it there (the new image in the compose "
                          f"file, then `docker compose up -d`)")
    if hc.get("Links") or hc.get("VolumesFrom"):
        raise DockerError(f"{name} was started with --link or --volumes-from, " + redo)
    out: list[str] = []
    for m in d.get("Mounts") or []:
        dst, kind = m.get("Destination") or "", m.get("Type") or ""
        if dst == "/root":
            continue               # the state folder: run_args mounts it
        if kind == "bind" and m.get("Source"):
            if {"z", "Z"} & set((m.get("Mode") or "").split(",")):
                raise DockerError(f"{name} mounts {m['Source']} with an SELinux label (:z), " + redo)
            fields = ["type=bind", f"source={m['Source']}", f"target={dst}"]
            if (m.get("Propagation") or "rprivate") != "rprivate":
                fields.append(f"bind-propagation={m['Propagation']}")
        elif kind == "volume" and m.get("Name"):
            fields = ["type=volume", f"source={m['Name']}", f"target={dst}"]
        elif kind == "tmpfs" and dst:
            fields = ["type=tmpfs", f"target={dst}"]
        else:
            raise DockerError(f"{name} has a {kind or 'kind of'} mount at {dst or '?'}, " + redo)
        if m.get("RW") is False:
            fields.append("readonly")
        out += ["--mount", _mount_arg(fields)]
    for dst, opts in (hc.get("Tmpfs") or {}).items():
        out += ["--tmpfs", f"{dst}:{opts}" if opts else dst]
    if cfg.get("User"):
        out += ["--user", str(cfg["User"])]
    for cport, binds in (hc.get("PortBindings") or {}).items():
        port, _, proto = cport.partition("/")
        suffix = f"/{proto}" if proto and proto != "tcp" else ""
        for b in binds or []:
            ip = b.get("HostIp") or ""
            if ":" in ip:          # IPv6, which -p wants in brackets
                ip = f"[{ip}]"
            out += ["-p", f"{ip + ':' if ip else ''}{b.get('HostPort') or port}:{port}{suffix}"]
    log = hc.get("LogConfig") or {}
    if (log.get("Type") or "json-file") != "json-file":
        out += ["--log-driver", log["Type"]]
    for k, v in (log.get("Config") or {}).items():
        out += ["--log-opt", f"{k}={v}"]
    for flag, key in (("--cap-add", "CapAdd"), ("--cap-drop", "CapDrop"), ("--add-host", "ExtraHosts"),
                      ("--dns", "Dns"), ("--dns-search", "DnsSearch"), ("--dns-option", "DnsOptions"),
                      ("--security-opt", "SecurityOpt"), ("--group-add", "GroupAdd")):
        for v in hc.get(key) or []:
            out += [flag, str(v)]
    for dev in hc.get("Devices") or []:
        out += ["--device", ":".join(str(x) for x in (dev.get("PathOnHost"), dev.get("PathInContainer"),
                                                      dev.get("CgroupPermissions")) if x)]
    for k, v in (hc.get("Sysctls") or {}).items():
        out += ["--sysctl", f"{k}={v}"]
    for u in hc.get("Ulimits") or []:
        out += ["--ulimit", f"{u.get('Name')}={u.get('Soft')}:{u.get('Hard')}"]
    if hc.get("Privileged"):
        out.append("--privileged")
    if hc.get("Init"):
        out.append("--init")
    if hc.get("NanoCpus"):
        out += ["--cpus", f"{hc['NanoCpus'] / 1e9:g}"]
    if hc.get("PidMode"):
        out += ["--pid", str(hc["PidMode"])]
    if (hc.get("IpcMode") or "private") not in ("private", "shareable"):
        out += ["--ipc", str(hc["IpcMode"])]
    if hc.get("ShmSize") and hc["ShmSize"] != 64 * 1024 * 1024:
        out += ["--shm-size", str(hc["ShmSize"])]
    if (hc.get("Runtime") or "runc") != "runc":
        out += ["--runtime", str(hc["Runtime"])]
    return out


def run_args(old: Installed, image: str) -> Command:
    """`docker run` for the same container (name, network, ports, memory, logs, folders, user, its own
    environment and Flotilla's arguments) on a new image. DockerError, before anything is changed,
    when it was started with something an update can't carry over."""
    from .dashboard import NO_DASHBOARD_ENV
    from .vpnport import GLUETUN_AUTH_ENV, PROVIDER_ENV

    d = old.doc
    hc, cfg = d.get("HostConfig") or {}, d.get("Config") or {}
    cmd = ["docker", "run", "-d", "--name", old.name]
    mode = hc.get("NetworkMode") or ""
    vpn_env: tuple[list[str], dict, dict] | None = ([], {}, {})
    if mode.startswith("container:"):      # by name: an id goes stale when the VPN container is recreated
        ref = mode.split(":", 1)[1]
        vpn_name = _container_name(ref)
        if vpn_name == ref:                # Docker doesn't know it (recreated): the name we kept
            vpn_name = (cfg.get("Labels") or {}).get(VPN_LABEL) or ref
        cmd += ["--network", "container:" + vpn_name, "--label", f"{VPN_LABEL}={vpn_name}"]
        vpn_env = _gluetun_env_now(vpn_name)
    elif mode and mode not in ("default", "bridge"):
        cmd += ["--network", mode]
    policy = (hc.get("RestartPolicy") or {}).get("Name")
    if policy and policy != "no":
        cmd += ["--restart", policy]
    if hc.get("Memory"):
        cmd += ["--memory", str(hc["Memory"])]
    if hc.get("MemorySwap"):
        cmd += ["--memory-swap", str(hc["MemorySwap"])]
    cmd += _carried(d, old.name)
    # What the node knows about its gluetun comes from gluetun as it is now (its provider, its
    # control-server key), so an update picks up a key or provider changed since setup.
    fresh = {"FLOTILLA_GLUETUN_URL", GLUETUN_AUTH_ENV, PROVIDER_ENV} if vpn_env is not None else set()
    env: dict[str, str] = {}
    image_env = _image_env(d)
    for e in cfg.get("Env") or []:
        if not isinstance(e, str) or "=" not in e:
            continue
        name, value = e.split("=", 1)
        if name.startswith("FLOTILLA_"):
            if name in fresh:
                continue
            if name != GLUETUN_AUTH_ENV:     # Flotilla's own settings, none of them secret
                cmd += ["-e", e]
                continue
        elif (e in image_env) if image_env is not None else _IMAGE_ENV.fullmatch(name):
            continue               # the old image's, not the container's: the new image sets its own
        if not _env_line_ok(value):
            raise DockerError(f"{old.name}'s {name} setting has a line break, which an update can't pass on; "
                              f"recreate it yourself on the new image, or set it up again with `flotilla init`")
        env[name] = value          # the container's own settings may be secrets: kept off the command line
    args, vpn_secrets, hints = vpn_env or ([], {}, {})
    cmd += args
    env.update(vpn_secrets)
    if mode.startswith("container:") and old.args[:1] == ["run"] and f"{NO_DASHBOARD_ENV}=1" not in cmd:
        cmd += ["-e", f"{NO_DASHBOARD_ENV}=1"]       # set up before the node stopped serving its own
    if old.storage:
        cmd += ["-v", f"{old.storage}:/root"]
    return Command(cmd + [image, *old.args], env, hints)


class DockerError(Exception):
    pass


def pull(image: str) -> None:
    try:
        r = subprocess.run(["docker", "pull", image], timeout=1800)
    except (OSError, subprocess.SubprocessError) as e:
        raise DockerError(f"docker pull failed: {e}") from e
    if r.returncode:
        raise DockerError(f"couldn't download {image}")


def replace(old: Installed, image: str) -> None:
    """Recreate a container on a new image, putting the old one back if the new one won't start."""
    if not old.args:
        raise DockerError(f"can't tell what {old.name} runs; set it up again with `flotilla init`")
    cmd = run_args(old, image)
    keep = f"{old.name}-before-update"
    _docker("rm", "-f", keep)
    r = _docker("rename", old.name, keep)
    if not r or r.returncode:
        raise DockerError(f"couldn't rename {old.name} to make way for the new one: {(r.stderr if r else '').strip()}")
    # Up to a minute for Flotilla to save its state and exit; Docker's own wait, then its kill, inside that.
    try:
        r = subprocess.run(["docker", "stop", "-t", "60", keep], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        r = subprocess.CompletedProcess([], 1, "", str(e))
    if r.returncode:
        _docker("rename", keep, old.name)
        _docker("start", old.name)
        raise DockerError(f"couldn't stop {old.name} ({(r.stderr or '').strip()}); it's running as before")
    if run(cmd) != 0:
        _docker("rm", "-f", old.name)
        _docker("rename", keep, old.name)
        _docker("start", old.name)
        raise DockerError(f"the new {old.name} container wouldn't start; the old one is running again")
    _docker("rm", "-f", keep)


def recover(names: tuple[str, ...] = (NODE, DASHBOARD)) -> list[str]:
    """Put back a container an interrupted update left renamed `<name>-before-update` (0.2.1 could),
    so it's found, updated and run again under its own name."""
    back = []
    for name in names:
        keep = f"{name}-before-update"
        have = installed((name, keep))
        if keep in have and name not in have:
            r = _docker("rename", keep, name)
            if r and r.returncode == 0:
                _docker("start", name)
                back.append(name)
    return back


def shell(cmd: list[str]) -> str:
    """The command to show: a Command's secret environment as placeholders, never its values."""
    env = getattr(cmd, "env", None) or {}
    if env:
        shown = [x for name in env for x in ("-e", f"{name}={cmd.hints.get(name, '<' + name + '>')}")]
        cmd = [*cmd[:2], *shown, *cmd[2:]]
    return shlex.join(cmd)


def _env_file(env: dict) -> tuple[str, dict]:
    """An --env-file for the values it can hold (made 0600, by this user only), and the rest, which go
    to docker through its own environment instead (`-e NAME`)."""
    import os
    import tempfile

    fd, path = tempfile.mkstemp(prefix="flotilla-env-")
    rest = {k: v for k, v in env.items() if not _env_line_ok(v)}
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(f"{k}={v}\n" for k, v in env.items() if k not in rest)
    return path, rest


def run(cmd: list[str], interactive: bool = False) -> int:
    """Run a docker command, its output going straight to the terminal. A Command's secret environment
    goes in a temporary --env-file, removed once docker has read it."""
    import os

    env = getattr(cmd, "env", None) or {}
    path, proc_env = None, None
    try:
        argv = list(cmd)
        if env:
            path, rest = _env_file(env)
            argv[2:2] = ["--env-file", path] + [x for k in rest for x in ("-e", k)]
            proc_env = {**os.environ, **rest} if rest else None
        return subprocess.run(argv, timeout=None if interactive else 600, env=proc_env).returncode
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  docker failed: {e}")
        return 1
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
