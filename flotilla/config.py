"""User config (TOML) and local state (JSON)."""

from __future__ import annotations

import json
import math
import os
import secrets
import shutil
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

# How often a node may swap what it holds for what the swarm needs most (Config.reorient), in seconds.
# "never" isn't here: the node then only swaps when its user asks.
REORIENT_EVERY: dict[str, float] = {"daily": 86400.0, "weekly": 7 * 86400.0, "monthly": 30 * 86400.0}
REORIENT_CHOICES: tuple[str, ...] = (*REORIENT_EVERY, "never")
# What a node favours (planner.FOCUS_WEIGHTS): the copies most likely to be lost, the swarms with the
# most people downloading, or both.
FOCUS_CHOICES: tuple[str, ...] = ("at-risk", "balanced", "in-demand")

# Public keys of feed editors (minisign format). Ship your project's key here.
BUILTIN_EDITOR_KEYS: list[str] = [
    "RWQOOQ0x3qaNRuBCX933UEwMo/bKYR8GE/Ae/qQrswgEHlQHCBu4RixD",  # Flotilla project list, key ID 468DA6DE310D390E
]


@dataclass
class Config:
    folder: Path = Path.home() / "flotilla-seeds"
    budget_gb: float = 100.0
    reserve_gb: float = 20.0

    # Reorienting: how often this node swaps the lowest-priority pieces it holds for the pieces the
    # swarm needs most ("daily", "weekly", "monthly", or "never": only when you ask), and the most
    # it swaps each time. Between reorientations what it holds stays; free space is still filled, and
    # your own changes (a model you add, skip or set space aside for) still apply at once. Pieces of
    # models you added or set space aside for are never swapped out.
    reorient: str = "daily"
    focus: str = "at-risk"
    churn_gb: float = 20.0

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
    # Most models (torrents) this node carries at once. Space, memory_gb and the upload and connection
    # limits already bound what a node does, and a model nobody is downloading costs only a tracker
    # announce now and then, so the default is high; lower it to keep fewer swarms open.
    max_models: int = 1000
    listen_port: int = 6881
    port_forwarding: bool = False
    # On a node bound to a VPN: find the port the VPN forwards (gluetun, NAT-PMP, PIA's app; see
    # vpnport.py) and listen on it, so peers can connect in. vpn_port_file: a file holding the port
    # number, for a VPN client that writes one.
    vpn_port_forwarding: bool = True
    vpn_port_file: str = ""
    # In gluetun's network: when the port it forwarded has been gone 10 minutes, have gluetun reconnect
    # its VPN so it asks for a port again (after a failed request it never does by itself). Each try
    # cuts every container sharing gluetun's network for a few seconds; tries back off from an hour to
    # a day. Needs PUT /v1/vpn/status in the role Flotilla's gluetun key has. Off unless turned on.
    vpn_port_recover: bool = False
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

    # Web dashboard to watch and steer Flotilla, on http://127.0.0.1:7421.
    dashboard: bool = True
    dashboard_port: int = 7421
    # Who can reach it: "local" (this machine), "lan" (your home network), "tailnet" (your Tailscale
    # devices, through `tailscale serve`) or "public" (anyone on the internet). Anything but "local"
    # needs the access key, and anything but "public" refuses visitors from public internet addresses.
    dashboard_access: str = "local"

    # Once a day, see whether a newer signed release is out. The dashboard and `flotilla status` say
    # so; nothing installs by itself (`flotilla update` does, when you run it).
    check_updates: bool = True
    # Pirate Face's per-file swarms: each verified shard also seeds in its own swarm (see perfile.py).
    per_file_swarms: bool = True
    # Keep and seed Pirate Face's magnet index, and list models from it while the site is down (magnetindex.py).
    keep_magnet_index: bool = True
    # Your Pirate Face handle (the name in pirateface.co/<name>), if you want the dashboard to link to
    # its page, where Pirate Face shows the real points. Never sent anywhere.
    pf_handle: str = ""

    # Each lets anyone copy and redistribute the weights. glm-5.3 and qwen-community-1.0 are MIT-style
    # with extra conditions only for very large commercial AI services.
    licenses: list[str] = field(default_factory=lambda: ["mit", "apache-2.0", "wtfpl", "openmdw-1.1",
                                                         "glm-5.3", "qwen-community-1.0"])
    include_unlisted: bool = True
    include_rescue: bool = True
    seed_code_files: bool = False
    allow_pickle: bool = False
    whole_model_under_gb: float = 25.0
    max_candidates: int = 20      # unlisted models (rescued, new torrents) looked at per refresh
    # Once everything on the list has its copies, look at the least-seeded models of Pirate Face's whole
    # catalog (sweep.py), at most sweep_candidates at a time.
    catalog_sweep: bool = True
    sweep_candidates: int = 20

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
        """The planner's cap on models carried at once (Config.max_models). It used to be about one per
        Mbit/s of upload, 4 to 64, which left most of a big disk empty once the list was full: Pirate
        Face's catalog is mostly models under 1 GB."""
        return self.max_models

    @property
    def connections_limit(self) -> int:
        return int(max(50, min(500, self.upload_mbps * self.upload_share * 8)))


_SECTIONS = {
    "storage": ["folder", "budget_gb", "reserve_gb"],
    "reorient": ["every", "churn_gb"],
    "network": ["upload_mbps", "download_mbps", "upload_share", "download_share", "monthly_upload_cap_gb",
                "memory_gb", "max_models", "listen_port", "port_forwarding", "vpn_port_forwarding", "vpn_port_file",
                "vpn_port_recover", "bind_interface", "hf_fallback", "hf_gb_per_day"],
    "policy": ["focus", "licenses", "include_unlisted", "include_rescue", "seed_code_files", "allow_pickle",
               "whole_model_under_gb", "max_candidates", "catalog_sweep", "sweep_candidates"],
    "feed": ["urls", "trusted_keys", "allowed_hosts", "local"],
    "net": ["allowed_hosts"],
    "tuning": ["refresh_hours", "probe_seconds", "spread", "hold_bonus", "min_hold_days"],
    "dashboard": ["enabled", "port", "access"],
    "updates": ["check"],
    "pirateface": ["per_file_swarms", "keep_magnet_index", "handle"],
}


PAST_DEFAULT_LICENSES = [["mit", "apache-2.0", "wtfpl", "openmdw-1.1"]]

# The numeric settings, one table for every way a number gets in: the config file (load_config),
# `flotilla init` (flags), and the dashboard's Settings (engine.Engine.SETTINGS, which must agree with the
# first nine rows: a test checks it). attr: (name as the config file spells it, lowest, highest, whole number?).
# Without limits a NaN or a zero slips through: budget_gb = nan means "0 GB" and drops everything held,
# refresh_hours = 0 refreshes in a loop, upload_mbps = nan stops the daemon at start.
NUMBER_LIMITS: dict[str, tuple[str, float, float, bool]] = {
    "budget_gb": ("storage.budget_gb", 1, 1e6, False),
    "reserve_gb": ("storage.reserve_gb", 0, 1e6, False),
    "upload_mbps": ("network.upload_mbps", 0.1, 1e5, False),
    "download_mbps": ("network.download_mbps", 0.1, 1e5, False),
    "upload_share": ("network.upload_share", 0.05, 1, False),
    "download_share": ("network.download_share", 0.05, 1, False),
    "monthly_upload_cap_gb": ("network.monthly_upload_cap_gb", 0, 1e7, False),
    "hf_gb_per_day": ("network.hf_gb_per_day", 0, 1e6, False),
    "churn_gb": ("reorient.churn_gb", 0, 1e6, False),
    "memory_gb": ("network.memory_gb", 0, 1e6, False),
    "max_models": ("network.max_models", 1, 100_000, True),
    "listen_port": ("network.listen_port", 0, 65535, True),
    "whole_model_under_gb": ("policy.whole_model_under_gb", 0, 1e6, False),
    "max_candidates": ("policy.max_candidates", 0, 10_000, True),
    "sweep_candidates": ("policy.sweep_candidates", 0, 200, True),
    "dashboard_port": ("dashboard.port", 1, 65535, True),
    "refresh_hours": ("tuning.refresh_hours", 0.1, 8760, False),
    "probe_seconds": ("tuning.probe_seconds", 0, 3600, True),
    "spread": ("tuning.spread", 0, 20, False),
    "hold_bonus": ("tuning.hold_bonus", 0, 100, False),
    "min_hold_days": ("tuning.min_hold_days", 0, 3650, False),
}


def check_number(attr: str, v, name: str | None = None):
    """Return v if it is a real number within the limits for Config.attr, else raise ValueError saying
    what's allowed. NaN, infinity, booleans and text are never numbers here. `name` is how the caller
    spells the setting (a command-line flag, say); by default the config file's name for it."""
    where, lo, hi, whole = NUMBER_LIMITS[attr]
    ok = (not isinstance(v, bool) and isinstance(v, int if whole else (int, float))
          and math.isfinite(v) and lo <= v <= hi)
    if not ok:
        raise ValueError(f"{name or where} must be {'a whole number' if whole else 'a number'} "
                         f"from {lo:,.12g} to {hi:,.12g}")
    return v


# What versions before NUMBER_LIMITS already refused. A number they accepted must not stop a node
# that upgrades; one they refused still does.
_REFUSED_BEFORE = {
    "budget_gb": lambda v: v <= 0,
    "upload_share": lambda v: not 0 < v <= 1,
    "download_share": lambda v: not 0 < v <= 1,
    "hf_gb_per_day": lambda v: v < 0,
    "churn_gb": lambda v: v < 0,
    "max_models": lambda v: not 1 <= v <= 100_000,
    "sweep_candidates": lambda v: not 0 <= v <= 200,
    "listen_port": lambda v: True,       # a port can't be moved to the nearest limit
    "dashboard_port": lambda v: True,
}


def _loaded_number(attr: str, v):
    """A number from the config file. Text, booleans, NaN and infinity stop the node (they were never
    valid and misbehave silently), and so does anything older versions refused. A real number older
    versions accepted but that's outside the limits is moved to the nearest one, with a warning, so a
    node that upgrades keeps starting."""
    where, lo, hi, whole = NUMBER_LIMITS[attr]
    if whole and isinstance(v, float) and math.isfinite(v) and v.is_integer():
        v = int(v)
    if (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            and not (lo <= v <= hi) and not _REFUSED_BEFORE.get(attr, lambda _: False)(v)):
        near = (int if whole else float)(lo if v < lo else hi)
        print(f"warning: {where} = {v} is outside {lo:,.12g} to {hi:,.12g}; using {near:,.12g}",
              file=sys.stderr, flush=True)
        return near
    return check_number(attr, v)


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
                    ("feed", "local"): "local_feed", ("reorient", "every"): "reorient",
                    ("dashboard", "enabled"): "dashboard", ("dashboard", "port"): "dashboard_port",
                    ("dashboard", "access"): "dashboard_access",
                    ("updates", "check"): "check_updates", ("pirateface", "handle"): "pf_handle"}.get((section, k), k)
            v = sec[k]
            if attr in ("folder", "local_feed"):
                v = Path(os.path.expanduser(v))
            setattr(cfg, attr, v)
    # A config written by an older version holds that version's default licenses word for word.
    # Nobody chose that list; give it the current defaults so new reviewed licenses reach them.
    if sorted(cfg.licenses) in [sorted(old) for old in PAST_DEFAULT_LICENSES]:
        cfg.licenses = Config().licenses
    for attr in NUMBER_LIMITS:
        setattr(cfg, attr, _loaded_number(attr, getattr(cfg, attr)))
    from .access import ACCESS_CHOICES

    if cfg.dashboard_access not in ACCESS_CHOICES:
        raise ValueError(f"dashboard.access must be one of: {', '.join(ACCESS_CHOICES)}")
    if cfg.focus not in FOCUS_CHOICES:
        raise ValueError(f"policy.focus must be one of: {', '.join(FOCUS_CHOICES)}")
    if cfg.reorient not in REORIENT_CHOICES:
        raise ValueError(f"reorient.every must be one of: {', '.join(REORIENT_CHOICES)}")
    if not isinstance(cfg.vpn_port_forwarding, bool):
        raise ValueError("network.vpn_port_forwarding must be true or false")
    if not isinstance(cfg.vpn_port_recover, bool):
        raise ValueError("network.vpn_port_recover must be true or false")
    if not isinstance(cfg.vpn_port_file, str):
        raise ValueError("network.vpn_port_file must be a path, or \"\"")
    if not isinstance(cfg.catalog_sweep, bool):
        raise ValueError("policy.catalog_sweep must be true or false")
    if isinstance(cfg.sweep_candidates, bool) or not isinstance(cfg.sweep_candidates, int) or not 0 <= cfg.sweep_candidates <= 200:
        raise ValueError("policy.sweep_candidates must be a whole number from 0 to 200")
    from .pfaccount import PFKeyError, clean_handle

    try:
        cfg.pf_handle = clean_handle(cfg.pf_handle) if isinstance(cfg.pf_handle, str) else None
    except PFKeyError:
        cfg.pf_handle = None
    if cfg.pf_handle is None:
        raise ValueError("pirateface.handle must be the name in pirateface.co/name, or \"\"")
    for attr, where in (("licenses", "policy.licenses"), ("feed_urls", "feed.urls"),
                        ("trusted_keys", "feed.trusted_keys"), ("feed_allowed_hosts", "feed.allowed_hosts"),
                        ("allowed_hosts", "net.allowed_hosts")):
        v = getattr(cfg, attr)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"{where} must be a list of text")
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
    # [net] is written only when it was changed: a config that never set it keeps following the program's
    # defaults, and one that did (hosts Pirate Face is reached on, say) must survive every rewrite, from
    # `flotilla init` and from the dashboard's Settings.
    net = ""
    if list(cfg.allowed_hosts) != list(DEFAULT_ALLOWED_HOSTS):
        net = f"""[net]
allowed_hosts = {_toml_str(list(cfg.allowed_hosts))}   # hosts Flotilla's own requests may reach (Pirate Face, Hugging Face)

"""
    return f"""# Flotilla config. Edit freely; `flotilla plan` shows the effect.

[storage]
folder = {_toml_str(folder)}      # where seeded files live (not a temp dir)
budget_gb = {_toml_str(cfg.budget_gb)}            # most disk Flotilla may use
reserve_gb = {_toml_str(cfg.reserve_gb)}           # always leave this much free on the drive

[reorient]
every = {_toml_str(cfg.reorient)}         # swap low-priority pieces for what the swarm needs most: "daily", "weekly", "monthly" or "never"
churn_gb = {_toml_str(cfg.churn_gb)}           # most to swap each time; models you added or set space aside for are never swapped out

[network]
upload_mbps = {_toml_str(cfg.upload_mbps)}          # your line's upload speed, megabits/s
download_mbps = {_toml_str(cfg.download_mbps)}
upload_share = {_toml_str(cfg.upload_share)}         # fraction of upload given to the swarm
download_share = {_toml_str(cfg.download_share)}
monthly_upload_cap_gb = {_toml_str(cfg.monthly_upload_cap_gb)}  # 0 = no cap; pauses seeding when reached
memory_gb = {_toml_str(cfg.memory_gb)}             # most memory to use; no new downloads start above it. 0 = automatic
max_models = {_toml_str(cfg.max_models)}          # most models to carry at once; lower it to keep fewer swarms open
listen_port = {_toml_str(cfg.listen_port)}
port_forwarding = {_toml_str(cfg.port_forwarding)}   # UPnP/NAT-PMP on your router
vpn_port_forwarding = {_toml_str(cfg.vpn_port_forwarding)}   # bound to a VPN: find the port it forwards and listen on it
vpn_port_file = {_toml_str(cfg.vpn_port_file)}      # or read that port from this file; "" = don't
vpn_port_recover = {_toml_str(cfg.vpn_port_recover)}   # gluetun: reconnect its VPN when the forwarded port is lost
bind_interface = {_toml_str(cfg.bind_interface)}      # e.g. your VPN's address; "" = any interface
hf_fallback = {_toml_str(cfg.hf_fallback)}       # fetch pieces no peer has from Hugging Face (checked against the torrent)
hf_gb_per_day = {_toml_str(cfg.hf_gb_per_day)}        # most to fetch from Hugging Face a day; 0 = no limit

[policy]
focus = {_toml_str(cfg.focus)}      # what to seed first: "at-risk" (copies most likely to be lost), "in-demand" (most people downloading) or "balanced"
licenses = {_toml_str(cfg.licenses)}
include_unlisted = {_toml_str(cfg.include_unlisted)}   # also help models the list doesn't name (Pirate Face's rescued models and new torrents)
include_rescue = {_toml_str(cfg.include_rescue)}     # consider Pirate Face's rescued models
seed_code_files = {_toml_str(cfg.seed_code_files)}   # .py etc. in model repos (never executed either way)
allow_pickle = {_toml_str(cfg.allow_pickle)}      # formats that can run code when loaded: pickle (.bin/.pt/.ckpt), Keras .h5/.keras, NumPy .npz/.npy
whole_model_under_gb = {_toml_str(cfg.whole_model_under_gb)}  # seed small models whole instead of by shard
max_candidates = {_toml_str(cfg.max_candidates)}   # most unlisted models to consider (on top of the list)
catalog_sweep = {_toml_str(cfg.catalog_sweep)}    # once the whole list has its copies, seed the least-seeded models in Pirate Face's catalog
sweep_candidates = {_toml_str(cfg.sweep_candidates)}  # most catalog models the sweep looks at at a time

[feed]
urls = {_toml_str(cfg.feed_urls)}
trusted_keys = {_toml_str(cfg.trusted_keys)}       # extra editor public keys (minisign format)
allowed_hosts = {_toml_str(cfg.feed_allowed_hosts)}
local = {_toml_str(cfg.local_feed)}   # your own changes to the list (`flotilla feed --init-local`)

{net}[dashboard]
enabled = {_toml_str(cfg.dashboard)}         # local web page to watch and steer Flotilla
port = {_toml_str(cfg.dashboard_port)}
access = {_toml_str(cfg.dashboard_access)}     # local, lan (home network), tailnet, or public (anyone on the internet)

[updates]
check = {_toml_str(cfg.check_updates)}        # daily: is a newer signed release out? Only says so; `flotilla update` installs it

[pirateface]
per_file_swarms = {_toml_str(cfg.per_file_swarms)}   # also seed each shard in its own swarm, where Pirate Face counts you as a full seeder
keep_magnet_index = {_toml_str(cfg.keep_magnet_index)}   # keep and seed Pirate Face's magnet index (about 10 MB); used while the site is down
# Seeding credit for Pirate Face points: `flotilla points connect` (your key goes in its own file, not here)
handle = {_toml_str(cfg.pf_handle)}                # your Pirate Face handle, to link the dashboard to its page (the real points are there)

[tuning]
refresh_hours = {_toml_str(cfg.refresh_hours)}
probe_seconds = {_toml_str(cfg.probe_seconds)}       # how long to watch each swarm before deciding
spread = {_toml_str(cfg.spread)}              # higher = greedier, lower = more diverse across seeders
hold_bonus = {_toml_str(cfg.hold_bonus)}          # stickiness for files you already hold
min_hold_days = {_toml_str(cfg.min_hold_days)}
"""


def _keep_copy(path: Path, backup: Path) -> None:
    """Make `backup` another name for what `path` holds now, without ever leaving it missing or half
    written (a hard link, since the file is about to be replaced rather than changed in place; a copy
    where links aren't possible). A failure here only costs the backup, never the save."""
    tmp = backup.with_name(f"{backup.name}.{os.getpid()}.tmp")
    try:
        try:
            os.link(path, tmp)
        except OSError:
            shutil.copy2(path, tmp)
        os.replace(tmp, backup)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def write_atomic(path: Path, text: str, keep_backup: bool = False) -> None:
    """Replace `path` with `text` so a reader (or a power cut) sees the old file or the new one, never a
    half-written one: write a temp file of its own beside it, flush it to disk, rename it over `path`,
    then flush the folder so the rename itself survives. keep_backup: the file being replaced is kept as
    `<name>.bak`. The new file keeps the old one's permissions (a config a container reads as another
    user stays readable), else the usual ones for a new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "x") as f:          # "x": never write into a file someone else made at that name
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except OSError:
            pass                           # no old file to copy from
        if keep_backup and path.exists():
            _keep_copy(path, path.with_name(path.name + ".bak"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:        # not everywhere can a folder be opened and flushed (Windows can't); the file itself is safe
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def write_config(cfg: Config, path: Path | None = None) -> None:
    """Save the config (atomically: a crash mid-save never leaves a half-written config.toml)."""
    path = path or cfg.path
    if path is None:
        raise ValueError("no config path to write to")
    write_atomic(Path(path), render_config(cfg))


class StateError(ValueError):
    """state.json is unreadable and so is its backup."""


class State:
    """Small JSON state file: node id, feed version, holdings, upload counters.

    It records which seed folders belong to Flotilla (holdings), so losing it orphans them: save() keeps
    the previous good copy as state.json.bak, and loading falls back to that copy, loudly, if state.json
    can't be read. If neither can, it stops with a message rather than starting empty."""

    def __init__(self, state_dir: Path = STATE_DIR):
        self.dir = state_dir
        self.path = state_dir / "state.json"
        self.backup = state_dir / "state.json.bak"
        self._from_backup = False
        self.data: dict = self._load()
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

    @staticmethod
    def _read(path: Path) -> dict:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return data

    def _load(self) -> dict:
        if not self.path.exists() and not self.backup.exists():
            return {}                      # a first run
        problem = ""
        if self.path.exists():
            try:
                return self._read(self.path)
            except (OSError, ValueError) as e:     # JSONDecodeError and UnicodeDecodeError are ValueErrors
                problem = f"{self.path} can't be read ({e})"
        else:
            problem = f"{self.path} is missing"
        try:
            data = self._read(self.backup)
        except (OSError, ValueError) as e:
            raise StateError(
                f"{problem}, and its backup {self.backup} can't be read either ({e}). Flotilla won't start "
                f"empty: that would forget which folders hold its seeded files. Restore state.json from a "
                f"copy if you have one. Otherwise move both files out of {self.dir}: Flotilla then starts "
                f"fresh and will treat the files already in your seed folder as not its own.") from e
        print(f"state: {problem}; using the previous copy, {self.backup}. Changes since it was saved are "
              f"lost: Flotilla picks them up again from the files on disk.", file=sys.stderr, flush=True)
        self._from_backup = True
        return data

    def save(self) -> None:
        # Having just fallen back to the backup, the unreadable state.json must not become the backup:
        # the first save only replaces it.
        write_atomic(self.path, json.dumps(self.data, sort_keys=True, separators=(",", ":")),
                     keep_backup=not self._from_backup)
        self._from_backup = False


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
    """This process's private memory in bytes (Linux: RssAnon; macOS: the phys_footprint Activity
    Monitor shows as Memory). None where it can't be read."""
    if sys.platform == "darwin":
        return _darwin_footprint()
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("RssAnon:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


_TASK_VM_INFO = 22            # task_info flavor
_PHYS_FOOTPRINT = 18          # task_vm_info.phys_footprint, counted in 64-bit fields
_mach = None


def _darwin_footprint() -> int | None:
    global _mach
    try:
        if _mach is None:
            import ctypes
            import ctypes.util

            libc = ctypes.CDLL(ctypes.util.find_library("c"))
            _mach = (ctypes, libc, ctypes.c_uint.in_dll(libc, "mach_task_self_"))
        ctypes, libc, task = _mach
        buf = (ctypes.c_uint64 * 64)()
        count = ctypes.c_uint(128)            # room in 32-bit words; the kernel fills what it has
        if libc.task_info(task, _TASK_VM_INFO, ctypes.byref(buf), ctypes.byref(count)) != 0:
            return None
        if count.value < 2 * (_PHYS_FOOTPRINT + 1):   # an old kernel without phys_footprint
            return None
        return int(buf[_PHYS_FOOTPRINT]) or None
    except (OSError, AttributeError, ValueError):
        return None
