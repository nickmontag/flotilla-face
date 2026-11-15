"""Magnet parsing and validation.

A magnet is only accepted when every tracker and web seed in it is on the
allowlist. Peer hints (x.pe) and other parameters that could steer a client
somewhere unexpected are rejected outright rather than stripped, so a tampered
magnet fails loudly instead of being half-used.
"""

from __future__ import annotations

import base64
import binascii
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


def _btih_hex(value: str) -> str:
    if len(value) == 40:
        try:
            bytes.fromhex(value)
        except ValueError as e:
            raise MagnetError("btih is not hex") from e
        return value.lower()
    if len(value) == 32:
        try:
            return base64.b32decode(value.upper()).hex()
        except binascii.Error as e:
            raise MagnetError("btih is not base32") from e
    raise MagnetError("btih has the wrong length")


def parse_magnet(uri: str) -> Magnet:
    if not uri.startswith("magnet:?"):
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
    for tr in m.trackers:
        p = urllib.parse.urlsplit(tr)
        if p.scheme not in ("udp", "http", "https") or not p.hostname or not host_allowed(p.hostname, tracker_hosts):
            raise MagnetError(f"tracker not on allowlist: {tr}")
    for ws in m.web_seeds:
        p = urllib.parse.urlsplit(ws)
        if p.scheme != "https" or not p.hostname or not host_allowed(p.hostname, webseed_hosts):
            raise MagnetError(f"web seed not on allowlist: {ws}")
        if p.username or p.password or p.port not in (None, 443):
            raise MagnetError(f"web seed URL not allowed: {ws}")
    return m
