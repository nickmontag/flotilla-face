"""Magnet parsing and validation.

A magnet is only accepted when every tracker and web seed in it is on the
allowlist. Peer hints (x.pe) and other parameters that could steer a client
somewhere unexpected are rejected outright rather than stripped, so a tampered
magnet fails loudly instead of being half-used.
"""

from __future__ import annotations

import base64
import binascii
import re
import urllib.parse
from dataclasses import dataclass, field

from .netsafe import host_allowed

DEFAULT_TRACKER_HOSTS = ("tracker.pirateface.co",)
DEFAULT_WEBSEED_HOSTS = ("pirateface.co", "huggingface.co")
ALLOWED_PARAMS = {"xt", "dn", "tr", "ws", "xl"}


class MagnetError(ValueError):
    pass


@dataclass
class Magnet:
    btih: str                      # lowercase hex v1 infohash
    btmh: str | None = None        # v2 multihash (hex) if present
    name: str | None = None
    trackers: list[str] = field(default_factory=list)
    web_seeds: list[str] = field(default_factory=list)
    raw: str = ""


_HEX40 = re.compile(r"[0-9A-Fa-f]{40}")
_B32 = re.compile(r"[A-Za-z2-7]{32}")


def _btih_hex(value: str) -> str:
    # Matched exactly: bytes.fromhex skips whitespace, so "+abab…+" (spaces, once decoded) passed.
    if _HEX40.fullmatch(value):
        return value.lower()
    if _B32.fullmatch(value):
        try:
            return base64.b32decode(value.upper()).hex()
        except binascii.Error as e:
            raise MagnetError("btih is not base32") from e
    if len(value) in (32, 40):
        raise MagnetError("btih is not hex or base32")
    raise MagnetError("btih has the wrong length")


def parse_magnet(uri: str) -> Magnet:
    if not isinstance(uri, str) or not uri.startswith("magnet:?"):
        raise MagnetError("not a magnet URI")
    params = urllib.parse.parse_qsl(uri[len("magnet:?"):], keep_blank_values=True, strict_parsing=False)
    m = Magnet(btih="", raw=uri)
    for k, v in params:
        if k not in ALLOWED_PARAMS:
            raise MagnetError(f"magnet parameter {k!r} is not allowed")
        if k == "xt":
            if v.startswith("urn:btih:"):
                if m.btih:
                    raise MagnetError("more than one btih")
                m.btih = _btih_hex(v[len("urn:btih:"):])
            elif v.startswith("urn:btmh:"):
                m.btmh = v[len("urn:btmh:"):].lower()
            else:
                raise MagnetError(f"unsupported xt {v!r}")
        elif k == "dn":
            m.name = v
        elif k == "tr":
            m.trackers.append(v)
        elif k == "ws":
            m.web_seeds.append(v)
    if not m.btih:
        raise MagnetError("magnet has no v1 infohash")
    return m


def validate_magnet(
    m: Magnet,
    tracker_hosts: tuple[str, ...] = DEFAULT_TRACKER_HOSTS,
    webseed_hosts: tuple[str, ...] = DEFAULT_WEBSEED_HOSTS,
) -> Magnet:
    # urlsplit and .port raise ValueError on a malformed URL (a bad IPv6 literal or port): that is
    # a rejected magnet too, not an error for the caller.
    for tr in m.trackers:
        try:
            p = urllib.parse.urlsplit(tr)
            ok = p.scheme in ("udp", "http", "https") and p.hostname and host_allowed(p.hostname, tracker_hosts) \
                and (p.port is None or p.port > 0)
        except ValueError:
            ok = False
        if not ok:
            raise MagnetError(f"tracker not on allowlist: {tr}")
    for ws in m.web_seeds:
        try:
            p = urllib.parse.urlsplit(ws)
            ok = p.scheme == "https" and p.hostname and host_allowed(p.hostname, webseed_hosts)
            port = p.port
        except ValueError:
            raise MagnetError(f"web seed URL not allowed: {ws}") from None
        if not ok:
            raise MagnetError(f"web seed not on allowlist: {ws}")
        if p.username or p.password or port not in (None, 443):
            raise MagnetError(f"web seed URL not allowed: {ws}")
    return m
