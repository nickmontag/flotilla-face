"""The port a VPN forwards to this node, found by itself, so peers can start connections to it.

A node pinned to a VPN connects out to peers, but a peer can only connect *in* if the VPN forwards a
port to it, and Flotilla listens on that port. Most VPNs that forward one hand out a different port
per connection, so a fixed `listen_port` goes stale. This finds the current one, in order:

1. gluetun's control server (http://127.0.0.1:8000, inside its network): `GET /v1/portforward`, the
   port gluetun got from its provider (PIA, ProtonVPN, Perfect Privacy, PrivateVPN) and opened in its
   firewall. Only asked from inside a container, and only once it answers like gluetun.
2. NAT-PMP (RFC 6886) to the VPN's own gateway, inside the tunnel: ProtonVPN (10.2.0.1) and other VPNs
   that hand out ports this way. Sent from the pinned address to a private address, so it never
   leaves the tunnel, and renewed every 45 s, as the mapping lasts 60.
3. PIA's desktop app: `piactl get portforward`.
4. A file holding the port number (`vpn_port_file`), for anything else that can write one: gluetun's
   VPN_PORT_FORWARDING_STATUS_FILE on a shared folder, a script after wg-quick.

VPNs that forward no port at all (NordVPN, Mullvad, Surfshark, ExpressVPN, most others) leave nothing
to find; `Forward.note` says so and what would.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

GLUETUN = os.environ.get("FLOTILLA_GLUETUN_URL", "http://127.0.0.1:8000").rstrip("/")
# gluetun's control server wants a key (since v3.40). Either the key itself or the role JSON from
# gluetun's HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE ({"auth":"apikey","apikey":...} or basic).
GLUETUN_AUTH_ENV = "FLOTILLA_GLUETUN_AUTH"
# The VPN provider gluetun uses (its VPN_SERVICE_PROVIDER), which the host's setup and updates pass on:
# from inside, gluetun won't say without a key.
PROVIDER_ENV = "FLOTILLA_VPN_PROVIDER"
# gluetun providers and the names to show for them. Those in FORWARDS can forward a port through gluetun.
GLUETUN_FORWARDS = {"private internet access": "PIA", "protonvpn": "ProtonVPN", "perfect privacy": "Perfect Privacy",
                    "privatevpn": "PrivateVPN"}
_PROVIDER_NAMES = {"nordvpn": "NordVPN", "mullvad": "Mullvad", "surfshark": "Surfshark", "expressvpn": "ExpressVPN",
                   "ivpn": "IVPN", "windscribe": "Windscribe", "airvpn": "AirVPN", "cyberghost": "CyberGhost",
                   "ipvanish": "IPVanish", "purevpn": "PureVPN", "torguard": "TorGuard", "vyprvpn": "VyprVPN",
                   "hidemyass": "HideMyAss", "privado": "Privado", "fastestvpn": "FastestVPN", "giganews": "Giganews",
                   "slickvpn": "SlickVPN", "vpn secure": "VPN Secure", "vpn unlimited": "VPN Unlimited",
                   "custom": "a custom VPN", **GLUETUN_FORWARDS}


def provider_name(provider: str) -> str:
    p = provider.strip().lower()
    return _PROVIDER_NAMES.get(p) or provider.strip()
NATPMP_PORT = 5351
LIFETIME = 60          # NAT-PMP mapping lifetime asked for, seconds
RENEW = 45.0           # ask again this often while a port is forwarded
RETRY = 300.0          # and this often while none is
PIACTL = ("/Applications/Private Internet Access.app/Contents/MacOS/piactl", "/opt/piavpn/bin/piactl")

# VPNs that never forward a port, by the interface names their apps give the tunnel.
_NO_FORWARD = [(re.compile(r"^nord(lynx|tun)", re.I), "NordVPN"), (re.compile(r"mullvad", re.I), "Mullvad"),
               (re.compile(r"surfshark", re.I), "Surfshark"), (re.compile(r"expressvpn", re.I), "ExpressVPN"),
               (re.compile(r"ivpn", re.I), "IVPN")]
FORWARDING_VPNS = "ProtonVPN (Plus), PIA, AirVPN, Windscribe, Perfect Privacy and PrivateVPN"


@dataclass
class Forward:
    port: int = 0          # the public port peers connect to; 0: no forwarded port found
    source: str = ""       # where it came from: "gluetun", "NAT-PMP 10.2.0.1", "PIA app", "file"
    note: str = ""         # with no port: why, and what would give one
    renew: float = RETRY   # seconds until it's worth asking again
    listen: int = 0        # the port to listen on here, when the VPN forwards to another one

    def __post_init__(self):
        self.listen = self.listen or self.port

    def view(self) -> dict:
        return {"port": self.port, "listen": self.listen, "source": self.source, "note": self.note}


# ---------------------------------------------------------------- gluetun


class GluetunAbsent(Exception):
    """Nothing that answers like gluetun's control server."""


class GluetunAuth(Exception):
    """gluetun is there but wants credentials we don't have (or the ones we have are wrong)."""


def _gluetun_headers(auth: str) -> dict:
    auth = (auth or "").strip()
    if not auth:
        return {}
    try:
        role = json.loads(auth)
    except ValueError:
        return {"X-API-Key": auth}        # just the key
    if not isinstance(role, dict):
        return {}
    if role.get("auth") == "apikey" and role.get("apikey"):
        return {"X-API-Key": str(role["apikey"])}
    if role.get("auth") == "basic" and role.get("username"):
        import base64

        pair = f"{role['username']}:{role.get('password', '')}".encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode()}
    return {}


_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # loopback only: never a proxy


def gluetun_port(auth: str = "", base: str = GLUETUN, timeout: float = 3.0) -> int:
    """The port gluetun forwards (0 while it forwards none). GluetunAbsent / GluetunAuth otherwise."""
    host = urllib.parse.urlsplit(base).hostname or ""
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise GluetunAbsent(f"{base} isn't on this machine")
    except ValueError:
        if host != "localhost":
            raise GluetunAbsent(f"{base} isn't on this machine") from None
    headers = _gluetun_headers(auth)
    refused = 0
    # The new route, then the old one. A gluetun without the new route may answer 401 for it rather
    # than 404 (routes no role names are private), so a refusal there is no answer yet either.
    for path in ("/v1/portforward", "/v1/openvpn/portforwarded"):
        req = urllib.request.Request(base + path, headers=headers)
        try:
            with _LOCAL.open(req, timeout=timeout) as r:
                doc = json.loads(r.read(4096))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                refused = e.code
                continue
            if e.code == 404:
                continue
            raise GluetunAbsent(f"HTTP {e.code}") from None
        except (OSError, ValueError) as e:
            raise GluetunAbsent(str(e)) from None
        if not isinstance(doc, dict):
            raise GluetunAbsent("not gluetun's answer")
        ports = doc.get("ports") if "ports" in doc else [doc.get("port")]
        if not isinstance(ports, list) or not all(isinstance(p, int) and 0 <= p < 65536 for p in ports):
            raise GluetunAbsent("not gluetun's answer")
        return next((p for p in ports if p), 0)
    if refused:
        raise GluetunAuth(f"gluetun's control server said {refused}")
    raise GluetunAbsent("no port forwarding route")


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


# ---------------------------------------------------------------- NAT-PMP

TCP, UDP = 2, 1   # NAT-PMP opcodes


class NatPmpError(Exception):
    pass


def _ask(sk: socket.socket, gateway: str, msg: bytes, size: int, op: int) -> bytes:
    """Send and wait for the answer, retrying as RFC 6886 says (250 ms, doubling), three times."""
    wait = 0.25
    for _ in range(3):
        sk.sendto(msg, (gateway, NATPMP_PORT))
        sk.settimeout(wait)
        try:
            while True:
                data, (host, port) = sk.recvfrom(64)
                if host == gateway and port == NATPMP_PORT and len(data) >= size and data[1] == 128 + op:
                    return data
        except socket.timeout:
            wait *= 2
    raise NatPmpError(f"no NAT-PMP answer from {gateway}")


def natpmp_map(gateway: str, source: str, internal: int, external: int, proto: int,
               lifetime: int = LIFETIME) -> int:
    """Ask `gateway` to forward `external` (a wish; it may give another) to `internal`. Returns the
    port it gave; lifetime 0 removes the mapping. Sent from `source`, the pinned VPN address."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
        sk.bind((source, 0))            # fails while the VPN is down: nothing leaves another way
        data = _ask(sk, gateway, struct.pack("!BBHHHI", 0, proto, 0, internal, external, lifetime), 16, proto)
    _, _, result, _, _, mapped, _ = struct.unpack("!BBHIHHI", data[:16])
    if result:
        raise NatPmpError(f"{gateway} refused the mapping (NAT-PMP result {result})")
    return mapped


_TUNNEL_NETS = [ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]


def _private(addr: str) -> bool:
    """A VPN gateway inside the tunnel has an RFC 1918 or carrier-grade NAT address; nothing else is asked."""
    try:
        ip = ipaddress.IPv4Address(addr)
    except ValueError:
        return False
    return any(ip in n for n in _TUNNEL_NETS)


def _route_gateways(iface: str) -> list[str]:
    """Gateways the routing table names for this interface."""
    found = []
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["netstat", "-rn", "-f", "inet"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 4 and cols[3] == iface and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", cols[1]):
                found.append(cols[1])
        return found
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                cols = line.split()
                if cols[0] == iface and int(cols[2], 16):
                    found.append(socket.inet_ntoa(struct.pack("<I", int(cols[2], 16))))
    except (OSError, ValueError, StopIteration, IndexError):
        pass
    return found


def gateways(bind: str, address: str) -> list[str]:
    """Where the VPN's NAT-PMP server may be: its gateway in the routing table, the .1 of its
    subnet (WireGuard tunnels have no gateway), and ProtonVPN's fixed 10.2.0.1. Private only."""
    from .netinfo import _interfaces

    iface = bind
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", bind):
        iface = next((r.iface for r in _interfaces() if r.address == bind), "")
    found = _route_gateways(iface) if iface else []
    a, b, c, _ = address.split(".")
    found.append(f"{a}.{b}.{c}.1")
    if address.startswith("10.2."):
        found.append("10.2.0.1")
    out = []
    for g in found:
        if g != address and g not in out and _private(g):
            out.append(g)
    return out


# ---------------------------------------------------------------- PIA's app


def piactl_port() -> tuple[int, str]:
    """PIA's desktop app: (port, "") or (0, why). (0, "") when it isn't installed."""
    exe = shutil.which("piactl") or next((p for p in PIACTL if os.access(p, os.X_OK)), None)
    if not exe:
        return 0, ""
    try:
        out = subprocess.run([exe, "get", "portforward"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 0, ""
    if out.isdigit() and 0 < int(out) < 65536:
        return int(out), ""
    if out == "Inactive":
        return 0, "PIA's app isn't asking for a port: turn on Settings > Network > Request Port Forwarding"
    if out == "Unavailable":
        return 0, "this PIA server has no port forwarding (US servers don't): pick another region"
    return 0, f"PIA's app says port forwarding is {out.lower() or 'not ready'}" if out else ""


# ---------------------------------------------------------------- putting it together


def no_forward_vpn(bind: str) -> str:
    """The VPN's name, when its interface name says it's one that never forwards a port."""
    return next((name for pat, name in _NO_FORWARD if pat.search(bind)), "")


class PortFinder:
    """Asked every `Forward.renew` seconds with the port the node listens on now; returns the port
    the VPN forwards. Remembers which way worked, to go straight there next time."""

    def __init__(self, bind: str, *, port_file: str = "", gluetun_auth: str | None = None,
                 address=None, log=print):
        from .netinfo import interface_address

        self.bind, self.port_file, self.log = bind, port_file, log
        self.gluetun_auth = os.environ.get(GLUETUN_AUTH_ENV, "") if gluetun_auth is None else gluetun_auth
        self.provider = os.environ.get(PROVIDER_ENV, "")
        self.address = address or (lambda: interface_address(bind))
        self.natpmp_gw: str | None = None     # the gateway that answered
        self._translated: tuple[str, int, int] | None = None   # (gateway, here, public)
        self.gluetun = in_container()         # worth asking until it's clearly not there

    def _gluetun_note(self, wants_key: bool) -> str:
        """Why gluetun forwards no port (or won't say which), going by its provider when the host passed it on."""
        provider = self.provider.strip().lower()
        if provider and provider not in GLUETUN_FORWARDS:
            if provider in ("custom", "windscribe", "airvpn"):
                return (f"gluetun's {provider_name(provider)} has no port forwarding Flotilla can ask for. A fixed "
                        f"forwarded port goes in listen_port, and in gluetun's FIREWALL_VPN_INPUT_PORTS")
            return (f"{provider_name(provider)} forwards no ports, so peers can't start connections to you: you "
                    f"upload over the ones you open. VPNs that forward one: {FORWARDING_VPNS}")
        if wants_key:
            vpn = provider_name(provider) if provider else "your VPN"
            maybe = "" if provider else " If its VPN forwards ports (PIA, ProtonVPN, Perfect Privacy, PrivateVPN),"
            return (f"couldn't ask gluetun which port {vpn} forwards: its control server wants a key.{maybe} give "
                    f"gluetun HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE and run `flotilla update` or `flotilla init` "
                    f"on the Docker host, which passes it to this container as {GLUETUN_AUTH_ENV}")
        vpn = provider_name(provider) if provider else "Its provider needs port forwarding (PIA, ProtonVPN, " \
                                                       "Perfect Privacy or PrivateVPN), and gluetun"
        return f"gluetun forwards no port. {vpn} needs VPN_PORT_FORWARDING=on"

    def find(self, current: int) -> Forward:
        notes = []
        if self.gluetun:
            try:
                port = gluetun_port(self.gluetun_auth)
                if port:
                    return Forward(port, "gluetun", renew=60.0)
                return Forward(note=self._gluetun_note(False))
            except GluetunAuth:
                # Not NAT-PMP instead: a port mapped past gluetun's firewall can't come in.
                return Forward(note=self._gluetun_note(True))
            except GluetunAbsent:
                self.gluetun = False
        fw = self._natpmp(current)
        if fw:
            return fw
        port, why = piactl_port()
        if port:
            return Forward(port, "PIA app", renew=60.0)
        if why:
            notes.append(why)
        if self.port_file:
            try:
                raw = Path(self.port_file).expanduser().read_text().split()[0]
                if raw.isdigit() and 0 < int(raw) < 65536:
                    return Forward(int(raw), "file", renew=60.0)
                notes.append(f"{self.port_file} doesn't hold a port number")
            except (OSError, IndexError):
                notes.append(f"couldn't read {self.port_file}")
        if not notes:
            name = no_forward_vpn(self.bind)
            vpn = f"{name} doesn't forward ports" if name else "this VPN doesn't seem to forward a port"
            notes.append(f"{vpn}, so peers can't start connections to you: you upload over the ones you "
                         f"open. VPNs that forward one: {FORWARDING_VPNS} (a fixed one goes in listen_port)")
        return Forward(note="; ".join(notes))

    def _natpmp(self, current: int) -> Forward | None:
        src = self.address()
        if not src:
            return None                   # the VPN is down
        tried = [self.natpmp_gw] if self.natpmp_gw else gateways(self.bind, src)
        for gw in tried:
            try:
                port = natpmp_map(gw, src, current, current, TCP)
                listen = current
                if port != current:
                    if self._translated == (gw, current, port):
                        pass              # known: it forwards `port` there to `current` here
                    elif natpmp_map(gw, src, port, port, TCP) == port:
                        # Most VPNs (ProtonVPN among them) forward the port they give to the same
                        # port here: listen on it. The mapping for `current` lapses by itself.
                        listen = port
                    else:
                        self._translated = (gw, current, port)
                udp = natpmp_map(gw, src, listen, port, UDP)
            except (OSError, NatPmpError):
                continue
            if udp != port:
                self.log(f"vpn port: {gw} forwards TCP {port} but UDP {udp}; uTP peers may not reach you")
            self.natpmp_gw = gw
            return Forward(port, f"NAT-PMP {gw}", renew=RENEW, listen=listen)
        self.natpmp_gw = None
        return None
