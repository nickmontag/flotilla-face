"""Small, read-only facts about this machine's network: is traffic going through a VPN?"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

# Interface names that are almost always full-tunnel VPNs.
_VPN_IFACE = re.compile(r"^(utun\d+|ipsec\d+|ppp\d+|tun\d+|tap\d+|wg\w*|nordlynx|proton\w*|mullvad\w*|pia\w*|"
                        r"vpn\w*)$")


@dataclass
class Route:
    iface: str
    address: str | None

    @property
    def looks_like_vpn(self) -> bool:
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
