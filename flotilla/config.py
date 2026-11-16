"""User config (TOML) and local state (JSON)."""

from __future__ import annotations

import json
import os
import secrets
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .netsafe import DEFAULT_ALLOWED_HOSTS, pin


def _default_config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "flotilla"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "flotilla"


def _default_state_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "flotilla"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "flotilla"


CONFIG_PATH = Path(os.environ.get("FLOTILLA_CONFIG", _default_config_dir() / "config.toml"))
STATE_DIR = Path(os.environ.get("FLOTILLA_STATE", _default_state_dir()))

# Editorial feed mirrors. The signature, not the host, is what makes a feed
# trustworthy, so any mirror is fine as long as it is on feed.allowed_hosts.
DEFAULT_FEED_URLS: list[str] = [
    # The published list, fetched for updates between releases. Its signature is checked like
    # any other copy; if this fetch fails, the copy shipped in the package is used.
    "https://raw.githubusercontent.com/nickmontag/flotilla-face/main/flotilla/data/priorities.json",
]
FEED_HOSTS: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS + ("raw.githubusercontent.com",)

# Public keys of feed editors (minisign format). Ship your project's key here.
BUILTIN_EDITOR_KEYS: list[str] = [
    "RWQOOQ0x3qaNRuBCX933UEwMo/bKYR8GE/Ae/qQrswgEHlQHCBu4RixD",  # Flotilla project list, key ID 468DA6DE310D390E
]


@dataclass
class Config:
    folder: Path = Path.home() / "flotilla-seeds"
    budget_gb: float = 100.0
    reserve_gb: float = 20.0

    upload_mbps: float = 10.0
    download_mbps: float = 100.0
    upload_share: float = 0.8
    download_share: float = 0.9
    monthly_upload_cap_gb: float = 0.0
    # Most memory Flotilla should use. libtorrent needs 4-8 MB per GB of each file it is
    # downloading; above this Flotilla starts no new files until the ones under way finish
    # (seeding goes on). 0 = automatic: a quarter of this computer's memory, or 70% of a
    # container's limit.
    memory_gb: float = 0.0
    listen_port: int = 6881
    port_forwarding: bool = False
    # Pin all torrent traffic to one interface or address, e.g. a VPN's
    # (e.g. "10.8.0.2" or "wg0"). If the VPN drops, the address
    # vanishes and the seeder goes offline instead of using your real IP.
    bind_interface: str = ""
    # When no peer can supply a file's pieces (nothing from peers for 10 minutes), fetch the missing
    # pieces from Hugging Face, at the model's pinned commit, while it still hosts the model. Each
    # piece is checked against the torrent's own hash before it's kept. At most hf_gb_per_day a day
    # (UTC); 0 = no daily limit. Off until the user turns it on (`flotilla init`, or the dashboard).
    hf_fallback: bool = False
    hf_gb_per_day: float = 20.0

    # Local web dashboard (http://127.0.0.1:7421). Only reachable from this machine.
    dashboard: bool = True
    dashboard_port: int = 7421

    # Each lets anyone copy and redistribute the weights. glm-5.3 and qwen-community-1.0 are MIT-style
    # with extra conditions only for very large commercial AI services.
    licenses: list[str] = field(default_factory=lambda: ["mit", "apache-2.0", "wtfpl", "openmdw-1.1",
                                                         "glm-5.3", "qwen-community-1.0"])
    include_unlisted: bool = True
    include_rescue: bool = True
    seed_code_files: bool = False
    allow_pickle: bool = False
    whole_model_under_gb: float = 25.0
    max_candidates: int = 40

    feed_urls: list[str] = field(default_factory=lambda: list(DEFAULT_FEED_URLS))
    # Your own changes to the priority list (unsigned; it's your file). See README.
    local_feed: Path = field(default_factory=lambda: _default_config_dir() / "my-priorities.json")
    trusted_keys: list[str] = field(default_factory=list)
    feed_allowed_hosts: list[str] = field(default_factory=lambda: list(FEED_HOSTS))
    allowed_hosts: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_HOSTS))

    refresh_hours: float = 6.0
    probe_seconds: int = 90
    spread: float = 3.0
    hold_bonus: float = 1.5
    min_hold_days: float = 7.0

    path: Path | None = None

    # ---- derived numbers the engine uses
    @property
    def upload_bps(self) -> float:
        return self.upload_mbps * 125_000 * self.upload_share

    @property
    def download_bps(self) -> float:
        return self.download_mbps * 125_000 * self.download_share

    @property
    def max_torrents(self) -> int:
        # ~1 Mbit/s of useful upload per active swarm, bounded to keep the
        # session healthy on small links and sane on big ones.
        return int(max(4, min(64, self.upload_mbps * self.upload_share)))

    @property
    def connections_limit(self) -> int:
        return int(max(50, min(500, self.upload_mbps * self.upload_share * 8)))


_SECTIONS = {
    "storage": ["folder", "budget_gb", "reserve_gb"],
    "network": ["upload_mbps", "download_mbps", "upload_share", "download_share", "monthly_upload_cap_gb",
                "memory_gb", "listen_port", "port_forwarding", "bind_interface", "hf_fallback", "hf_gb_per_day"],
    "policy": ["licenses", "include_unlisted", "include_rescue", "seed_code_files", "allow_pickle",
               "whole_model_under_gb", "max_candidates"],
    "feed": ["urls", "trusted_keys", "allowed_hosts", "local"],
    "net": ["allowed_hosts"],
    "tuning": ["refresh_hours", "probe_seconds", "spread", "hold_bonus", "min_hold_days"],
    "dashboard": ["enabled", "port"],
}


PAST_DEFAULT_LICENSES = [["mit", "apache-2.0", "wtfpl", "openmdw-1.1"]]


def load_config(path: Path = CONFIG_PATH) -> Config:
    cfg = Config(path=path)
    if not path.exists():
        pin("")
        return cfg
    doc = tomllib.loads(path.read_text())
    for section, keys in _SECTIONS.items():
        sec = doc.get(section, {})
        for k in keys:
            if k not in sec:
                continue
            attr = {("feed", "urls"): "feed_urls", ("feed", "allowed_hosts"): "feed_allowed_hosts",
                    ("feed", "local"): "local_feed",
                    ("dashboard", "enabled"): "dashboard", ("dashboard", "port"): "dashboard_port"}.get((section, k), k)
            v = sec[k]
            if attr in ("folder", "local_feed"):
                v = Path(os.path.expanduser(v))
            setattr(cfg, attr, v)
    # A config written by an older version holds that version's default licenses word for word.
    # Nobody chose that list; give it the current defaults so new reviewed licenses reach them.
    if sorted(cfg.licenses) in [sorted(old) for old in PAST_DEFAULT_LICENSES]:
        cfg.licenses = Config().licenses
    if not 0 < cfg.upload_share <= 1 or not 0 < cfg.download_share <= 1:
        raise ValueError("upload_share and download_share must be in (0, 1]")
    if cfg.budget_gb <= 0:
        raise ValueError("storage.budget_gb must be positive")
    if cfg.hf_gb_per_day < 0:
        raise ValueError("network.hf_gb_per_day can't be negative")
    # Flotilla's own requests (listings, pieces from Hugging Face) leave by the same pin as torrent traffic.
    pin(cfg.bind_interface)
    return cfg


def _toml_str(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, Path):
        v = str(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_str(x) for x in v) + "]"
    raise TypeError(type(v))


def render_config(cfg: Config) -> str:
    home = str(Path.home())
    folder = str(cfg.folder)
    if folder.startswith(home):
        folder = "~" + folder[len(home):]
    return f"""# Flotilla config. Edit freely; `flotilla plan` shows the effect.

[storage]
folder = {_toml_str(folder)}      # where seeded files live (not a temp dir)
budget_gb = {_toml_str(cfg.budget_gb)}            # most disk Flotilla may use
reserve_gb = {_toml_str(cfg.reserve_gb)}           # always leave this much free on the drive

[network]
upload_mbps = {_toml_str(cfg.upload_mbps)}          # your line's upload speed, megabits/s
download_mbps = {_toml_str(cfg.download_mbps)}
upload_share = {_toml_str(cfg.upload_share)}         # fraction of upload given to the swarm
download_share = {_toml_str(cfg.download_share)}
monthly_upload_cap_gb = {_toml_str(cfg.monthly_upload_cap_gb)}  # 0 = no cap; pauses seeding when reached
memory_gb = {_toml_str(cfg.memory_gb)}             # most memory to use; no new downloads start above it. 0 = automatic
listen_port = {_toml_str(cfg.listen_port)}
port_forwarding = {_toml_str(cfg.port_forwarding)}   # UPnP/NAT-PMP on your router
bind_interface = {_toml_str(cfg.bind_interface)}      # e.g. your VPN's address; "" = any interface
hf_fallback = {_toml_str(cfg.hf_fallback)}       # fetch pieces no peer has from Hugging Face (checked against the torrent)
hf_gb_per_day = {_toml_str(cfg.hf_gb_per_day)}        # most to fetch from Hugging Face a day; 0 = no limit

[policy]
licenses = {_toml_str(cfg.licenses)}
include_unlisted = {_toml_str(cfg.include_unlisted)}   # also help models the editorial feed doesn't list
include_rescue = {_toml_str(cfg.include_rescue)}     # consider Pirate Face's rescued models
seed_code_files = {_toml_str(cfg.seed_code_files)}   # .py etc. in model repos (never executed either way)
allow_pickle = {_toml_str(cfg.allow_pickle)}      # formats that can run code when loaded: pickle (.bin/.pt/.ckpt), Keras .h5/.keras, NumPy .npz/.npy
whole_model_under_gb = {_toml_str(cfg.whole_model_under_gb)}  # seed small models whole instead of by shard
max_candidates = {_toml_str(cfg.max_candidates)}

[feed]
urls = {_toml_str(cfg.feed_urls)}
trusted_keys = {_toml_str(cfg.trusted_keys)}       # extra editor public keys (minisign format)
allowed_hosts = {_toml_str(cfg.feed_allowed_hosts)}
local = {_toml_str(cfg.local_feed)}   # your own changes to the list (`flotilla feed --init-local`)

[dashboard]
enabled = {_toml_str(cfg.dashboard)}         # local web page to watch and steer Flotilla
port = {_toml_str(cfg.dashboard_port)}           # http://127.0.0.1:<port>, this machine only

[tuning]
refresh_hours = {_toml_str(cfg.refresh_hours)}
probe_seconds = {_toml_str(cfg.probe_seconds)}       # how long to watch each swarm before deciding
spread = {_toml_str(cfg.spread)}              # higher = greedier, lower = more diverse across seeders
hold_bonus = {_toml_str(cfg.hold_bonus)}          # stickiness for files you already hold
min_hold_days = {_toml_str(cfg.min_hold_days)}
"""


class State:
    """Small JSON state file: node id, feed version, holdings, upload counters."""

    def __init__(self, state_dir: Path = STATE_DIR):
        self.dir = state_dir
        self.path = state_dir / "state.json"
        self.data: dict = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        if "node_id" not in self.data:
            self.data["node_id"] = secrets.token_hex(16)
        self.data.setdefault("feed_version", 0)
        self.data.setdefault("holdings", {})
        self.data.setdefault("upload", {"month": "", "bytes": 0})

    @property
    def node_id(self) -> str:
        return self.data["node_id"]

    @property
    def holdings(self) -> dict:
        return self.data["holdings"]

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        os.replace(tmp, self.path)


def memory_budget(cfg: Config) -> int:
    """Bytes of memory Flotilla aims to stay under (see Config.memory_gb)."""
    if cfg.memory_gb and cfg.memory_gb > 0:
        return int(cfg.memory_gb * 1024 ** 3)
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        total = 8 * 1024 ** 3
    budget = total // 4
    limit = container_memory_limit()
    if limit and limit < total:
        budget = min(budget, int(limit * 0.7))
    return max(budget, 512 * 1024 ** 2)


def container_memory_limit() -> int | None:
    """The memory limit of the cgroup we run in (a Docker --memory cap), if any."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 60:
            return int(raw)
    return None


def own_memory() -> int | None:
    """This process's private memory in bytes (Linux: RssAnon). None where it can't be read."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("RssAnon:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None
