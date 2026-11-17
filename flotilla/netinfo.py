"""Small, read-only facts about this machine's network: is traffic going through a VPN?"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

# Interface names that are almost always VPN tunnels: the generic kinds (tun0, wg0, utun4), the names
# VPN apps give theirs (nordlynx, proton0, wg0-mullvad, wg-windscribe) and wg-quick configs named after
# a server (us-nyc-wg-001, home-vpn).
_VPN_IFACE = re.compile(r"^(utun\d+|ipsec\d+|ppp\d+|tun\d+|tap\d+|wg[\w.-]*|nordlynx|nordtun|proton[\w.-]*|"
                        r"pvpn[\w.-]*|mullvad[\w.-]*|pia[\w.-]*|surfshark[\w.-]*|ivpn[\w.-]*|vpn[\w.-]*|"
                        r"[\w.-]*vpn|[\w.-]+[-_](wg|vpn|tun)([-_.][\w.-]*)?)$", re.I)
# Mesh overlays (Tailscale, ZeroTier, Nebula) are tunnels too, but they reach your own devices, not the
# internet: peers would never see them, and pinning to one would cut Flotilla off.
_MESH_IFACE = re.compile(r"^(tailscale\d*|zt\w+|nebula\w*|netbird\w*|wt\d+)$", re.I)
_CGNAT = re.compile(r"^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.")    # 100.64.0.0/10: Tailscale's addresses


@dataclass
class Route:
    iface: str
    address: str | None

    @property
    def looks_like_vpn(self) -> bool:
        if _MESH_IFACE.match(self.iface):
            return False
        if sys.platform == "darwin" and self.address and _CGNAT.match(self.address):
            return False      # Tailscale on macOS is a utun too
        return bool(_VPN_IFACE.match(self.iface))

    @property
    def bind_value(self) -> str:
        """What to put in `bind_interface`.

        macOS renumbers utun interfaces on every reconnect, so pin the
        address there. Linux interface names (wg0, tun0, nordlynx) are
        stable while addresses may not be, so pin the name."""
        if sys.platform == "darwin" and self.address:
            return self.address
        return self.iface


def _iface_ipv4(iface: str) -> str | None:
    """IPv4 address of an interface without external tools (Linux SIOCGIFADDR)."""
    try:
        import fcntl
        import socket
        import struct

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            req = struct.pack("256s", iface.encode()[:15])
            return socket.inet_ntoa(fcntl.ioctl(sk.fileno(), 0x8915, req)[20:24])
    except (OSError, ImportError):
        return None


def _proc_route(probe: str) -> Route | None:
    """Longest-prefix match in /proc/net/route: for containers without `ip` (gluetun & co)."""
    try:
        import socket
        import struct

        target = struct.unpack("<I", socket.inet_aton(probe))[0]
        best: tuple[int, int, str] | None = None
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                cols = line.split()
                iface, dest, mask, metric = cols[0], int(cols[1], 16), int(cols[7], 16), int(cols[6])
                if target & mask == dest & mask:
                    key = (bin(mask).count("1"), -metric)
                    if best is None or key > best[:2]:
                        best = (key[0], key[1], iface)
    except (OSError, ValueError, StopIteration, IndexError):
        return None
    if best is None:
        return None
    return Route(best[2], _iface_ipv4(best[2]))


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def internet_route(probe: str = "1.1.1.1") -> Route | None:
    """The interface (and its IPv4 address) this machine uses to reach the internet."""
    if sys.platform == "darwin":
        out = _run(["route", "-n", "get", probe])
        m = re.search(r"interface:\s*(\S+)", out)
        if not m:
            return None
        iface = m.group(1)
        a = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", _run(["ifconfig", iface]))
        return Route(iface, a.group(1) if a else None)
    out = _run(["ip", "-4", "route", "get", probe])
    m = re.search(r"\bdev (\S+)", out)
    if not m:
        return _proc_route(probe)
    src = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", out)
    return Route(m.group(1), src.group(1) if src else None)


def interface_address(value: str) -> str | None:
    """The IPv4 address to bind for a bind_interface value: the value itself if it's an address,
    else the named interface's current address (None while it's down)."""
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value):
        return value
    if sys.platform == "darwin":
        a = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", _run(["ifconfig", value]))
        return a.group(1) if a else None
    return _iface_ipv4(value)


def address_present(value: str) -> bool:
    """Is a bind_interface value (an IPv4 address or an interface name) up right now?"""
    if not value:
        return False
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value):
        if value.startswith("127."):
            return True
        out = _run(["ifconfig"]) if sys.platform == "darwin" else _run(["ip", "-4", "addr"])
        if not out and sys.platform != "darwin":  # no `ip`: try to bind the address instead
            import socket

            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
                    sk.bind((value, 0))
                return True
            except OSError:
                return False
        return re.search(rf"inet {re.escape(value)}[/ ]", out) is not None
    if sys.platform == "darwin":
        return "inet " in _run(["ifconfig", value])
    out = _run(["ip", "-4", "addr", "show", "dev", value])
    return "inet " in out if out else _iface_ipv4(value) is not None


def _interfaces() -> list[Route]:
    """Every interface with an IPv4 address."""
    if sys.platform == "darwin":
        found, iface = [], None
        for line in _run(["ifconfig"]).splitlines():
            m = re.match(r"^(\S+?):\s", line)
            if m:
                iface = m.group(1)
                continue
            a = re.match(r"\s+inet (\d+\.\d+\.\d+\.\d+)", line)
            if a and iface:
                found.append(Route(iface, a.group(1)))
        return found
    import os

    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        return []
    return [Route(n, a) for n in names if (a := _iface_ipv4(n))]


def carries_internet(route: Route, probe: str = "1.1.1.1") -> bool:
    """Would traffic leaving from this interface's address reach the internet through it? A VPN that's
    connected but isn't the default route (split tunnel, policy routing) often does; one that only
    reaches a work network doesn't, and pinning to it would leave Flotilla with no internet at all."""
    if not route.address:
        return False
    if sys.platform == "darwin":
        out = _run(["route", "-n", "get", "-ifscope", route.iface, probe])
    else:
        out = _run(["ip", "-4", "route", "get", probe, "from", route.address])
    m = re.search(r"interface:\s*(\S+)", out) or re.search(r"\bdev (\S+)", out)
    return bool(m) and m.group(1) == route.iface


def other_vpns(current: Route | None) -> list[Route]:
    """VPN tunnels that are up and can reach the internet, but that this machine's internet traffic
    doesn't use by default (a VPN app in split-tunnel mode, a WireGuard tunnel without a default route)."""
    skip = current.iface if current else None
    return [r for r in _interfaces()
            if r.iface != skip and r.looks_like_vpn and carries_internet(r)]
