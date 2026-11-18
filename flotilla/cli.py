"""Command line interface."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, ui
from .config import (BUILTIN_EDITOR_KEYS, CONFIG_PATH, FOCUS_CHOICES, REORIENT_CHOICES, STATE_DIR, Config, State, load_config,
                     render_config)
from .feed import BUNDLED_FEED, LOCAL_EXAMPLE, FeedError, apply_local, load_feed, origin_view, parse_feed
from .ledger import LedgerError, load_ledger
from .planner import GB, Params, Plan, plan as make_plan
from .signing import PublicKey, SecretKey, SignatureError, verify


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)
    from .service import rotate_own_log

    rotate_own_log()      # the background service's log file doesn't grow for ever


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or unit == "PB":
            return f"{n:.1f} {unit}" if unit not in ("B",) else f"{int(n)} B"
        n /= 1000
    return f"{n:.1f} PB"


def duration(sec: float) -> str:
    if sec == float("inf"):
        return "∞"
    if sec < 3600:
        return f"{sec / 60:.0f} min"
    if sec < 86400 * 2:
        return f"{sec / 3600:.1f} h"
    return f"{sec / 86400:.1f} days"


# ------------------------------------------------------------------ init


_ask = ui.ask
_yes = ui.yes


def parse_gb(raw: str) -> float:
    """'50', '50G', '50 GB', '1.5TB' -> gigabytes."""
    t = raw.strip().lower().replace(" ", "").removesuffix("b")
    mult = 1000.0 if t.endswith("t") else 1.0
    t = t.rstrip("tg")
    v = float(t) * mult
    if v <= 0:
        raise ValueError("must be positive")
    return v


SETUP_STEPS = 8


def cmd_init(args) -> int:
    path = Path(args.config)
    cfg = load_config(path) if path.exists() else Config()
    docker = _in_setup_container()
    ui.title(f"Flotilla {__version__} setup", f"writes {_host_path(path, docker)}")
    ui.note("Seeding shares files with strangers: peers in each swarm can see your IP address, and seeding "
            "uses disk space and upload bandwidth. Press Enter to keep a default. You can change any of this "
            "later in the dashboard, or stop at any time.")

    from .netinfo import internet_route

    route = internet_route()
    vpn_containers: list = []
    if not (route and route.looks_like_vpn) and not cfg.bind_interface:
        vpn_containers = _vpn_containers()
        if vpn_containers:
            handled = _offer_vpn_container(vpn_containers, cfg)
            if handled is not None:
                _setup_result("docker")
                return handled

    ui.section(1, SETUP_STEPS, "Storage")
    if docker:     # the folder was chosen on the host; seeded files go in it
        folder = cfg.folder
        ui.info(f"seeded files go in {_host_path(folder, docker)}")
    else:
        folder = Path(_ask("Folder for seeded files", str(cfg.folder))).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(folder)
    ui.info(f"that drive has {human(du.free)} free of {human(du.total)}")
    suggested = max(10.0, round((du.free / GB - cfg.reserve_gb) * 0.5))
    budget = _ask("Space for Flotilla, in GB", suggested if not path.exists() else cfg.budget_gb, parse_gb)
    reserve = _ask("Always leave this much free on the drive, in GB", cfg.reserve_gb, parse_gb)
    if budget > du.free / GB - reserve:
        ui.caution(f"only {du.free / GB - reserve:.0f} GB is free after the reserve; Flotilla will stay under that")

    ui.section(2, SETUP_STEPS, "Bandwidth")
    ui.note("In megabits per second. A speed test site will tell you yours.")
    up = _ask("Upload speed", cfg.upload_mbps, float)
    down = _ask("Download speed", cfg.download_mbps, float)
    share = _ask("Share of upload to give the swarm, 0.1-1", cfg.upload_share, float)
    dshare = _ask("Share of download to use while filling, 0.1-1", cfg.download_share, float)
    cap = _ask("Monthly upload cap in GB, 0 for none", cfg.monthly_upload_cap_gb, float)

    ui.section(3, SETUP_STEPS, "What to seed")
    ui.note("at-risk: the copies most likely to be lost, and models Hugging Face has taken down. in-demand: "
            "the models the most people are downloading right now. balanced: both.")

    def one_of(choices):
        def check(raw: str) -> str:
            if raw.strip().lower() not in choices:
                raise ValueError(raw)
            return raw.strip().lower()
        return check

    focus = _ask(f"Seed first: {', '.join(FOCUS_CHOICES)}", cfg.focus, one_of(FOCUS_CHOICES))
    ui.note("What the swarm needs changes. On a schedule, Flotilla drops the lowest-priority pieces you hold "
            "and fetches the ones the swarm needs most. Models you add or set space aside for stay."
            + (" Demand moves faster than rarity, so in-demand works best daily." if focus == "in-demand" else ""))
    often = "daily" if focus == "in-demand" and cfg.reorient == Config().reorient else cfg.reorient
    reorient = _ask(f"How often: {', '.join(REORIENT_CHOICES)}", often, one_of(REORIENT_CHOICES))
    churn = cfg.churn_gb
    if reorient != "never":
        churn = max(0.0, _ask("Most to swap each time, in GB", churn, parse_gb))

    ui.section(4, SETUP_STEPS, "Network")
    bind, pf = cfg.bind_interface, cfg.port_forwarding
    if route and route.looks_like_vpn:
        where = f"{route.iface}, {route.address}" if route.address else route.iface
        ui.info(f"your internet traffic goes through a VPN ({where})")
        if _yes("Bind torrent traffic to the VPN, so Flotilla goes offline if the VPN drops instead of "
                "using your real connection", True):
            bind, pf = route.bind_value, False
    elif bind:
        if not _yes(f"Keep torrent traffic bound to {bind} (offline whenever it's down)", True):
            bind = ""
    else:
        for other in _split_vpns(route):
            ui.info(f"a VPN is connected ({other.iface}, {other.address}), but this machine's internet "
                    f"traffic doesn't go through it unless asked to")
            if _yes(f"Bind torrent traffic to {other.iface}, so peers see the VPN's address and Flotilla goes "
                    f"offline if the VPN drops", True):
                bind, pf = other.bind_value, False
                break
        if not bind and vpn_containers:
            ui.caution(f"{vpn_containers[0].name} is a VPN container, but Flotilla set up here doesn't use it: "
                       f"peers will see this connection's public IP address. Run `flotilla init` again to set "
                       f"Flotilla up inside it.")
        elif not bind:
            ui.caution("no VPN detected: peers will see this connection's public IP address")
    if bind:
        from .vpnport import FORWARDING_VPNS, no_forward_vpn

        if name := no_forward_vpn(bind):
            ui.note(f"{name} forwards no ports, so peers can't start connections to you: Flotilla uploads over "
                    f"the ones it opens. VPNs that forward one: {FORWARDING_VPNS}.")
        else:
            ui.note("If your VPN forwards a port (gluetun, ProtonVPN, PIA's app), Flotilla finds it and listens on "
                    "it, so peers can connect in.")
    if not bind:
        ui.note("Opening a port on your router (UPnP/NAT-PMP) lets peers reach you, so you seed more. Skip it "
                "if you've turned UPnP off on your router.")
        pf = _yes("Open a port on your router", pf)

    ui.section(5, SETUP_STEPS, "Pieces no peer has")
    ui.note("Some models have no full seeder, and the peers holding a file's last pieces aren't always online. "
            "Flotilla can fetch those pieces from Hugging Face while it still has the model, checking each "
            "against the torrent. Hugging Face sees the address you fetch from"
            + (f" ({bind}, your VPN)." if bind else " (this connection's public IP)."))
    hf, hf_gb = cfg.hf_fallback, cfg.hf_gb_per_day
    hf = _yes("Fetch pieces no peer has from Hugging Face", hf)
    if hf:
        hf_gb = max(0.0, _ask("Most to fetch a day, in GB, 0 for no limit", hf_gb, float))
        _setup_hf_account(path, bind)

    ui.section(6, SETUP_STEPS, "Dashboard")
    if docker:    # its container is outside the VPN; the host's setup asks about it
        access = cfg.dashboard_access
        ui.info("the dashboard runs in a container of its own; setup on the host asks who can reach it")
    else:
        access = _ask_access(cfg.dashboard_access, cfg.dashboard_port)

    ui.section(7, SETUP_STEPS, "Pirate Face points")
    credit = _setup_points(path, bind, tuple(cfg.allowed_hosts))

    ui.section(8, SETUP_STEPS, "Licenses")
    licenses = list(cfg.licenses)
    try:
        from .netsafe import FetchError
        from .sources import PirateFace

        lic = PirateFace(tuple(cfg.allowed_hosts)).licenses()
        auto = [x["id"] for x in lic if not x.get("acceptFirst")]
        # Licenses Flotilla's maintainers read and allow by default (see README, Safety).
        reviewed = [x for x in Config().licenses if x not in auto]
        gated = [x["label"] for x in lic if x.get("acceptFirst") and x["id"] not in reviewed]
        ui.note(f"Pirate Face lists these without extra terms: {', '.join(auto)}.")
        if reviewed:
            ui.note(f"Flotilla also allows {', '.join(reviewed)}: they let anyone redistribute, and their extra "
                    f"terms only apply to very large commercial AI services.")
        if gated:
            ui.note(f"Not picked automatically, because you must accept their terms first: {', '.join(gated)}.")
        if _yes("Seed models under all of the allowed licenses", True):
            licenses = auto + reviewed
    except (FetchError, OSError, ValueError) as e:
        ui.caution(f"couldn't read Pirate Face's license list ({e}); keeping {', '.join(licenses)}")

    cfg.folder, cfg.budget_gb, cfg.reserve_gb = folder, budget, reserve
    cfg.upload_mbps, cfg.download_mbps = up, down
    cfg.upload_share, cfg.download_share = min(1.0, max(0.1, share)), min(1.0, max(0.1, dshare))
    cfg.monthly_upload_cap_gb, cfg.port_forwarding, cfg.licenses = cap, pf, licenses
    cfg.bind_interface = bind
    cfg.hf_fallback, cfg.hf_gb_per_day = hf, hf_gb
    cfg.reorient, cfg.churn_gb, cfg.focus = reorient, churn, focus
    cfg.dashboard_access = access

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(cfg))
    _setup_result("here")
    print()
    ui.done(f"Saved {_host_path(path, docker)}")
    fill = duration(budget * GB / cfg.download_bps)
    ui.table([
        ("Space", f"{budget:g} GB in {_host_path(folder, docker)}, keeping {reserve:g} GB free"),
        ("Upload", f"{up:g} Mbit/s × {cfg.upload_share:.0%}, up to {cfg.max_torrents} swarms at once"
                   + (f", {cap:g} GB a month" if cap else "")),
        ("Filling", f"about {fill} at {down:g} Mbit/s × {cfg.download_share:.0%} (rare models take longer)"),
        ("Seeding", {"at-risk": "at-risk first: the copies most likely to be lost",
                     "balanced": "balanced: at-risk copies and busy swarms",
                     "in-demand": "in-demand first: the swarms with the most people downloading"}[focus]),
        ("Reorient", reorient if reorient == "never" else f"{reorient}, up to {churn:g} GB"),
        ("Network", f"pinned to {bind}: if the VPN drops, Flotilla goes offline" if bind
                    else "router port open (UPnP)" if pf else "your connection, no router port"),
        ("Hugging Face", (f"fetches pieces no peer has, up to {f'{hf_gb:g} GB a day' if hf_gb else 'any amount'}")
                         if hf else "off: pieces no peer has are waited for"),
        ("Dashboard", _describe_access(access)),
        ("Points", "your seeding counts toward your Pirate Face account" if credit
                   else "not earning: no Pirate Face account connected (`flotilla points connect`)"),
    ])
    if access == "public":
        from .access import PUBLIC_WARNING

        ui.caution(PUBLIC_WARNING[0].upper() + PUBLIC_WARNING[1:])
    if docker:     # the host starts the seeding and dashboard containers next
        return 0
    print(f"\n{ui.bold('Next')}")
    ui.table([("flotilla plan", "preview what this node would seed"),
              ("flotilla service install", "run it in the background"),
              ("flotilla dashboard", "open the dashboard, once it runs"
               + ("" if access == "local" else "; `flotilla dashboard --url` prints its address with the key"))],
             key_style=str, value_style=ui.dim)
    return 0


def _describe_access(access: str) -> str:
    from .access import describe

    return describe(access) + ("" if access in ("local", "public") else ", with its access key")


def _ask_access(default: str, port: int) -> str:
    """Who can reach the dashboard. Public has to be asked for, and typed out."""
    import socket

    from .access import describe, serve_command, tailscale_name
    from .netinfo import lan_address

    lan, ts = lan_address(), tailscale_name()
    options = [("local", "This machine only (from elsewhere, through an SSH tunnel)")]
    if lan:
        options.append(("lan", f"Devices on your home network, at {lan}"))
    if ts:
        options.append(("tailnet", f"Your Tailscale devices, from anywhere, at {ts}"))
    options.append(("public", "Anyone on the internet (not recommended)"))
    keys = [k for k, _ in options]
    ui.note("The dashboard steers Flotilla. Anything beyond this machine needs its access key, and unless you "
            "make it public, visitors from the internet are turned away even if they find its port.")
    for i, (_, label) in enumerate(options, 1):
        ui.info(f"{i}. {label}")
    if default not in keys or default == "public":
        default = "local"

    def pick(raw: str) -> str:
        raw = raw.strip().lower()
        if raw in keys:
            return raw
        n = int(raw)
        if not 1 <= n <= len(keys):
            raise ValueError(raw)
        return keys[n - 1]

    choice = pick(str(_ask(f"Who can reach the dashboard, 1-{len(keys)}", keys.index(default) + 1, pick)))
    if choice == "public":
        ui.caution("Anyone who finds its address sees a Flotilla dashboard. Internet scanners will, and that ties "
                   "this address to seeding these models. The page is plain HTTP, so the access key crosses the "
                   "internet unencrypted the first time you open it.")
        ui.note("To reach it from away from home, Tailscale or an SSH tunnel is safer. If you do make it public, "
                "put a proxy with HTTPS and a login in front of it.")
        if str(_ask("To make it public anyway, type public", "no", str)).strip().lower() != "public":
            choice = default
            ui.info(f"kept to {describe(choice)}")
    if choice == "local":
        ui.info(f"from another computer: ssh -L {port}:localhost:{port} {socket.gethostname()}, "
                f"then open http://localhost:{port}")
    elif choice == "tailnet":
        ui.info(f"run this once to put it on your tailnet (it changes Tailscale's settings, and HTTPS must be on "
                f"for your tailnet): {serve_command(port)}")
    return choice


def _in_setup_container() -> dict | None:
    """When the host's setup runs us in a VPN container's network: where it keeps our files there
    (a Docker volume name or a folder on the host) and which VPN container it is."""
    import os
    from . import containers

    storage = os.environ.get(containers.STORAGE_ENV)
    return {"storage": storage, "vpn": os.environ.get(containers.VPN_ENV, "")} if storage else None


def _host_path(p: Path, docker: dict | None) -> str:
    """A path inside the setup container as the host sees it: /root is the host's chosen folder."""
    if not docker:
        return str(p)
    try:
        rel = Path(p).relative_to(Path.home())
    except ValueError:
        return str(p)
    storage = docker["storage"]
    if storage.startswith("/"):
        return str(Path(storage) / rel)
    return f"{rel} in the {storage} Docker volume"


def _setup_result(where: str) -> None:
    """Tell install.sh where setup put Flotilla: "here" (run it as a service) or "docker" (the
    containers run themselves). It passes a file to write that to."""
    import os

    target = os.environ.get("FLOTILLA_SETUP_RESULT")
    if target:
        try:
            Path(target).write_text(where)
        except OSError:
            pass


def _vpn_containers() -> list:
    """VPN containers (gluetun & co) on this machine, when we're not inside one ourselves."""
    from . import containers

    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return []
    try:
        return containers.vpn_containers()
    except containers.DockerUnavailable as e:
        ui.caution(f"couldn't ask Docker about VPN containers on this machine ({e})")
        return []


def _split_vpns(route) -> list:
    from .netinfo import other_vpns

    return other_vpns(route)


def _offer_vpn_container(found: list, cfg: Config) -> int | None:
    """Offer to set Flotilla up inside a VPN container's network instead of on this machine, or to set
    up again a node an earlier setup put there. Returns init's exit code when it did (or printed how
    to), None to go on with setup here."""
    from . import containers as dc

    have = dc.installed()
    foreign = [n for n, e in have.items() if not e.ours]
    ours = {n: e for n, e in have.items() if e.ours}
    node = ours.get(dc.NODE)
    previous = next((c for c in found if node and c.is_target(node.network_mode)), None)

    print(f"\n{ui.bold('Found a VPN container' if len(found) == 1 else f'Found {len(found)} VPN containers')}")
    for i, c in enumerate(found, 1):
        others = [n for n in c.shared_by if n not in ours]
        users = f"; {', '.join(others)} use{'s' if len(others) == 1 else ''} its network" if others else ""
        health = f", {c.health}" if c.health and c.health != "healthy" else ""
        ui.info(f"{f'{i}. ' if len(found) > 1 else ''}{c.name} ({c.image}{health}){users}")
    if foreign:
        ui.caution(f"Docker already has a container named {' and '.join(foreign)} that isn't Flotilla's. Rename "
                   f"it ({'; '.join(f'docker rename {n} {n}-old' for n in foreign)}) and run setup again.")
        return 1
    storage = node.storage if node and node.storage else ""
    if ours:
        state = "running" if node and node.running else "stopped" if node else "dashboard only"
        on = f" on {previous.name}'s network" if previous else ""
        ui.info(f"Flotilla already runs in Docker: {dc.NODE} ({state}){on}" + (f", files in {storage}" if storage else ""))
        ui.note("Setting it up again keeps everything it has seeded, with its current settings as the defaults. "
                "Its containers are then replaced with new ones. (Most settings can also be changed in the "
                "dashboard, without any of this.)")
        if not _yes("Set it up again", True):
            ui.info("nothing changed")
            return 0
    else:
        ui.note(f"This machine's own internet traffic doesn't go through {'it' if len(found) == 1 else 'them'}, "
                "so Flotilla set up here would show peers your real IP address. Inside the VPN container's "
                "network, every connection goes through the VPN, and the dashboard runs in a container of its "
                "own, reachable from your network.")
        if not _yes("Set Flotilla up inside the VPN container", True):
            return None
    vpn = previous or found[0]
    if len(found) > 1:
        def pick(raw: str):
            for c in found:
                if raw.strip() == c.name:
                    return c
            n = int(raw)
            if not 1 <= n <= len(found):
                raise ValueError(raw)
            return found[n - 1]

        vpn = _ask(f"Which one, 1-{len(found)} or its name", vpn.name, pick)
        if isinstance(vpn, str):
            vpn = next(c for c in found if c.name == vpn)
    if vpn.health == "unhealthy":
        ui.caution(f"{vpn.name} reports itself unhealthy: Flotilla will stay offline until its VPN is up")
    forwards, why = vpn.forwarding()
    if why:
        (ui.info if forwards else ui.note)(why)

    def where(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith(("/", "~", ".")):
            folder = Path(raw).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            return str(folder)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", raw):
            raise ValueError(raw)
        return raw

    if storage:
        volume = storage           # where the earlier setup's files are; moving them isn't setup's job
    else:
        ui.note("Flotilla keeps its settings and the pieces it seeds in one place: a folder on this machine, or "
                "a Docker volume, which Docker keeps in its own folder on the drive it's installed on. Seeded "
                "models are large, so a folder on a drive with room to spare is usually best.")
        volume = _ask("Folder for Flotilla's files (a /path), or Enter for a Docker volume named",
                      dc.VOLUME, where)
        if volume.startswith("/"):
            ui.info(f"that drive has {human(shutil.disk_usage(volume).free)} free")
    from .netinfo import lan_address

    print(f"\n{ui.bold('Dashboard')}")
    lan = lan_address()
    earlier = (ours.get(dc.DASHBOARD) or node).access if ours else ""
    access = _ask_access(earlier or ("lan" if lan else "local"), cfg.dashboard_port)
    from .access import address, public_address, tailscale_name

    ts = tailscale_name() if access == "tailnet" else None
    base = address(access, cfg.dashboard_port, None, lan, ts, public_address() if access == "public" else None)
    from .netsafe import FetchError
    from .release import ReleaseError

    try:
        image, version = dc.release_image()
    except (ReleaseError, FetchError) as e:
        ui.caution(f"couldn't get the newest signed Flotilla release ({e}); Docker setup needs it. Check this "
                   f"machine's internet connection and try again.")
        return 1
    ui.info(f"Flotilla {version}, image {image}")
    setup = dc.setup_command(vpn, volume, image)
    start = [("seeding", dc.node_command(vpn, volume, image)),
             ("dashboard", dc.dashboard_command(cfg.dashboard_port, volume, access, lan, base, image))]
    if not sys.stdin.isatty():
        ui.note("Setup asks questions, so it needs a terminal. Run these, in order:")
        for cmd in ([["docker", "rm", "-f", *ours]] if ours else []) + [setup] + [c for _, c in start]:
            print(f"    {dc.shell(cmd)}")
        return 0

    ui.info(f"installing Flotilla in a container on {vpn.name}'s network; the setup questions follow there")
    if dc.run(setup, interactive=True) != 0:
        ui.caution("setup inside the container didn't finish" + ("; the running node is unchanged" if ours else ""))
        return 1
    print()
    verb = "Restart seeding" if ours else "Start seeding"
    if not _yes(f"{verb} through {vpn.name} now, with its dashboard (containers {dc.NODE} and "
                f"{dc.DASHBOARD}, restarted with Docker)", True):
        if ours:
            ui.note(f"The running node keeps its old settings until you restart it: docker restart {dc.NODE}")
        else:
            ui.note("To start them later:")
            for _, cmd in start:
                print(f"    {dc.shell(cmd)}")
        return 0
    if ours and not dc.remove(list(ours)):
        ui.caution(f"couldn't remove the old containers ({', '.join(ours)}); remove them with "
                   f"docker rm -f {' '.join(ours)} and run setup again")
        return 1
    for what, cmd in start:
        if dc.run(cmd) != 0:
            ui.caution(f"couldn't start the {what} container. The command was:")
            print(f"    {dc.shell(cmd)}")
            return 1
    ui.done(f"Seeding through {vpn.name}" + (" with the new settings" if ours else ""))
    from .access import key_in, serve_command

    ui.info("waiting for the dashboard to start")
    url = dc.dashboard_address()
    key = key_in(url) if url else None
    print()
    if key:
        print(f"{ui.bold('Dashboard')}  {ui.accent(f'{base}/?key={key}')}")
        ui.note({"local": "Open it on this machine; your browser remembers the key.",
                 "lan": "Open it from any device on your network; your browser remembers the key. Anyone with "
                        "this address can steer the node, so keep it to yourself.",
                 "tailnet": f"Open it from any of your Tailscale devices once you've run "
                            f"`{serve_command(cfg.dashboard_port)}`; your browser remembers the key.",
                 "public": "Anyone with this address can steer the node."}[access])
        if access == "public":
            from .access import PUBLIC_WARNING

            ui.caution(PUBLIC_WARNING[0].upper() + PUBLIC_WARNING[1:])
    else:
        ui.caution(f"the dashboard hasn't started yet: docker logs {dc.DASHBOARD} shows its address once it has")
    print(f"\n{ui.bold('Later')}")
    ui.table([(f"docker logs {dc.DASHBOARD}", "the dashboard's address again"),
              (f"docker exec {dc.NODE} flotilla status", "what the node is doing"),
              (f"docker logs -f {dc.NODE}", "its log")],
             key_style=str, value_style=ui.dim)
    return 0


def _setup_hf_account(config_path: Path, bind: str) -> None:
    from . import netsafe
    from .hfauth import TokenError, read_token, remove_token, token_path

    tpath = token_path(config_path)
    try:
        have = read_token(tpath) is not None
    except (TokenError, OSError):
        have = True
    if have:
        if not _yes(f"A Hugging Face account token is connected ({tpath}). Keep using it for gated models", True):
            remove_token(tpath)
            ui.info("disconnected; if it's no longer needed, delete it at huggingface.co/settings/tokens too")
        return
    ui.note("Some models are gated: Hugging Face serves them only to accounts their author let in. Without an "
            "account, Flotilla waits for peers to send their missing pieces.")
    if _yes("Connect a Hugging Face account for those (a read-only token)", False):
        netsafe.pin(bind)      # check the token over the connection Flotilla will use
        hf_connect(tpath)


def _setup_points(config_path: Path, bind: str, allowed: tuple[str, ...]) -> bool:
    """Setup's question about seeding credit. True if an account is connected afterwards."""
    from . import netsafe
    from .pfaccount import PFKeyError, key_path, read_key

    kpath = key_path(config_path)
    try:
        have = read_key(kpath) is not None
    except (PFKeyError, OSError):
        have = True
    ui.note("Flotilla seeds each shard it holds in that file's own swarm too, where Pirate Face counts you as a "
            "full seeder. Pirate Face gives points for seeding to accounts that connect their key; the "
            "account then sees what this node seeds, from the address peers see"
            + (f" ({bind}, your VPN)." if bind else "."))
    if have:
        if _yes(f"A Pirate Face account is connected ({kpath}). Keep earning points with it", True):
            return True
        from .hfauth import remove_token

        remove_token(kpath)
        ui.info("disconnected: seeding no longer earns points")
        return False
    netsafe.pin(bind)          # check a key over the connection Flotilla will use
    return points_connect(kpath, allowed, skippable=True)


def points_connect(kpath: Path, allowed: tuple[str, ...], source: str | None = None,
                   skippable: bool = False) -> bool:
    """Ask for a Pirate Face seeding tracker address or community key (or read it from `source`, a
    file or - for stdin), check it, save it. With `skippable`, pressing Enter skips."""
    import getpass

    from .hfauth import save_secret
    from .netsafe import FetchError
    from .pfaccount import SETTINGS_PAGE, PFKeyError, check, clean_key, kind_of, tracker_host
    from .sources import PirateFace

    if source is None:
        print("\nPirate Face counts seeding through a tracker of your account's own. From "
              f"{SETTINGS_PAGE}, paste either:")
        print("  * the Seeding tracker address (http://tracker.pirateface.co:6970/.../announce), or")
        print("  * the Community torrent key (pfct_...): with it Flotilla follows the tracker if Pirate Face changes it.")
        print("Points go to your handle: an account without one earns none. Every torrent Flotilla seeds also")
        print("announces to that tracker, so Pirate Face sees what this node seeds, and from which address, under")
        print(f"your account. It's saved in {kpath}, readable only by you. Don't post it publicly.")
        try:
            raw = getpass.getpass("Paste it (it won't show" + ("; Enter to skip): " if skippable else "): "))
        except EOFError:        # no one to ask
            raw = ""
    elif source == "-":
        raw = sys.stdin.readline()
    else:
        raw = Path(source).expanduser().read_text()
    if not raw.strip():
        print("Skipped: seeding earns no points. Connect later with `flotilla points connect`, or in the "
              "dashboard's settings." if skippable and source is None else "Not connected: nothing to connect.")
        return False
    try:
        key = clean_key(raw)
        if kind_of(key) == "key":
            print("Checking it with Pirate Face...")
        url = check(key, PirateFace(allowed))
    except PFKeyError as e:
        print(f"Not connected: {e}")
        return False
    except FetchError as e:
        print(f"Not connected: couldn't reach Pirate Face to check the key ({e}). Try again in a while.")
        return False
    try:
        save_secret(kpath, key)
    except OSError as e:
        print(f"Not connected: couldn't save the key to {kpath} ({e})")
        return False
    print(f"Connected: your seeding tracker is on {tracker_host(url)}. Saved to {kpath}.")
    if kind_of(key) == "tracker":
        print("Pirate Face can't confirm a tracker address. If your points don't move within a day, copy it again.")
    print("A running node starts announcing to it within a minute.")
    return True


def cmd_points(args) -> int:
    from .hfauth import readable_by_others, remove_token
    from .netsafe import FetchError
    from .pfaccount import PFKeyError, check, key_path, kind_of, read_key, tracker_host
    from .sources import PirateFace

    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)              # sets the network pin, so Pirate Face sees the address Flotilla uses
    kpath = key_path(cfg_path)
    if args.action == "connect":
        return 0 if points_connect(kpath, tuple(cfg.allowed_hosts), args.from_file) else 1
    if args.action == "disconnect":
        try:
            gone = remove_token(kpath)
        except OSError as e:
            print(f"Couldn't remove {kpath}: {e}")
            return 1
        print(f"Disconnected: removed {kpath}. Seeding no longer earns points." if gone
              else "No Pirate Face account was connected.")
        return 0
    try:
        key = read_key(kpath)
    except (PFKeyError, OSError) as e:
        print(f"The key file {kpath} can't be used: {e}")
        return 1
    if not key:
        print("No Pirate Face account connected: seeding earns no points. Connect one with `flotilla points connect`.")
    else:
        print(f"Key file: {kpath}")
        if readable_by_others(kpath):
            print(f"  others on this computer can read it: chmod 600 {kpath}")
        try:
            url = check(key, PirateFace(tuple(cfg.allowed_hosts)))
            how = " (a seeding tracker address, used as it is)" if kind_of(key) == "tracker" else ""
            print(f"Connected: seeding counts toward your account's points (tracker {tracker_host(url)}){how}.")
        except PFKeyError as e:
            print(f"Not used: {e}")
        except FetchError as e:
            print(f"Couldn't check it with Pirate Face: {e}")
    status = Path(args.state) / "status.json"
    if status.exists():
        _print_per_file(json.loads(status.read_text()))
    if not cfg.per_file_swarms:
        print("Per-file swarms are off (per_file_swarms under [pirateface] in the config).")
    return 0


def _print_per_file(doc: dict) -> None:
    pf = doc.get("per_file") or {}
    if pf.get("enabled"):
        line = f"Per-file swarms: seeding {pf.get('joined', 0)} file(s) in their own swarms"
        if pf.get("waiting"):
            line += f", {pf['waiting']} to look up"
        if pf.get("hashing"):
            line += f", hashing {pf['hashing']}"
        print(line)
    mi = doc.get("magnet_index") or {}
    if mi.get("enabled") and mi.get("build"):
        print(f"Magnet index: keeping build {mi['build']} ({mi.get('models') or 0} models)"
              + (", seeding it" if mi.get("seeding") else ", fetching it") + "; used if Pirate Face can't be reached")
    acct = doc.get("pirateface") or {}
    if acct.get("state") == "ok":
        print(f"Points: seeding counts toward your Pirate Face account ({acct.get('tracker')})")
    elif acct.get("why"):
        print(f"Points: {acct['why']}")


def hf_connect(tpath: Path, source: str | None = None) -> bool:
    """Ask for a token (or read it from `source`, a file or - for stdin), check it, save it."""
    import getpass

    from .hfauth import TOKENS_PAGE, TokenError, check, clean_token, save_token
    from .netsafe import FetchError

    if source is None:
        print("\nFlotilla uses a read-only Hugging Face access token, only for gated models no peer is sending:")
        print(f"  * Make one at {TOKENS_PAGE}: Create new token, type Read. Flotilla refuses tokens that can")
        print("    write or spend.")
        print("  * Ask for access on each gated model's Hugging Face page yourself. The author sees your username")
        print("    and email, and may set terms of their own.")
        print("  * Hugging Face then sees your account fetch those models, from the address Flotilla uses.")
        print(f"  * The token is saved in {tpath}, readable only by you, and sent only to huggingface.co.")
        raw = getpass.getpass("Paste the token (it won't show): ")
    elif source == "-":
        raw = sys.stdin.readline()
    else:
        raw = Path(source).expanduser().read_text()
    try:
        token = clean_token(raw)
        print("Checking it with Hugging Face...")
        acct = check(token)
    except TokenError as e:
        print(f"Not connected: {e}")
        return False
    except FetchError as e:
        print(f"Not connected: couldn't reach Hugging Face to check the token ({e}). Try again in a while.")
        return False
    try:
        save_token(tpath, token)
    except OSError as e:
        print(f"Not connected: couldn't save the token to {tpath} ({e})")
        return False
    kind = "fine-grained, read only" if acct.role == "fineGrained" else "read-only"
    print(f"Connected: {acct.name}'s token {acct.token_name!r} ({kind}), saved to {tpath}.")
    if acct.scoped:
        print(f"  It's limited to: {', '.join(acct.scoped)}")
    print("A running node starts using it within a minute, for gated models only.")
    return True


def _print_gated(gated: list[dict]) -> None:
    if not gated:
        return
    print("\nGated on Hugging Face (no peer is sending some of their pieces, and Hugging Face won't serve them yet):")
    for g in gated:
        what = "request access there" if g.get("kind") == "access" else "needs an account with access: flotilla hf connect"
        print(f"  {g['repo']}: https://huggingface.co/{g['repo']} ({what})")


def cmd_hf(args) -> int:
    from .hfauth import TokenError, check, read_token, readable_by_others, remove_token, token_path
    from .netsafe import FetchError

    cfg_path = Path(args.config)
    load_config(cfg_path)                    # sets the network pin, so Hugging Face sees the address Flotilla uses
    tpath = token_path(cfg_path)
    if args.action == "connect":
        return 0 if hf_connect(tpath, args.from_file) else 1
    if args.action == "disconnect":
        try:
            gone = remove_token(tpath)
        except OSError as e:
            print(f"Couldn't remove {tpath}: {e}")
            return 1
        print(f"Disconnected: removed {tpath}. If the token is no longer needed, delete it at "
              f"huggingface.co/settings/tokens too." if gone else "No Hugging Face account was connected.")
        return 0
    # status
    try:
        token = read_token(tpath)
    except (TokenError, OSError) as e:
        print(f"The token file {tpath} can't be used: {e}")
        return 1
    if not token:
        print("No Hugging Face account connected: gated models wait for peers. Connect one with `flotilla hf connect`.")
    else:
        print(f"Token file: {tpath}")
        if readable_by_others(tpath):
            print(f"  others on this computer can read it: chmod 600 {tpath}")
        try:
            acct = check(token)
            print(f"Connected as {acct.name}, token {acct.token_name!r} "
                  f"({'fine-grained, read only' if acct.role == 'fineGrained' else 'read-only'}).")
            if acct.scoped:
                print(f"  It's limited to: {', '.join(acct.scoped)}")
        except TokenError as e:
            print(f"Not used: {e}")
        except FetchError as e:
            print(f"Couldn't check it with Hugging Face: {e}")
    status = Path(args.state) / "status.json"
    if status.exists():
        _print_gated(json.loads(status.read_text()).get("hf_gated") or [])
    return 0


# ------------------------------------------------------------------ service / uninstall


def cmd_add(args) -> int:
    from .feed import add_pick
    from .netsafe import FetchError
    from .sources import PirateFace

    cfg = load_config(Path(args.config))
    repo, new = add_pick(cfg.local_feed, args.link, args.priority, args.note or "")
    print(f"{'Added' if new else 'Updated'} {repo} in your picks ({cfg.local_feed}).")
    pf = PirateFace(tuple(cfg.allowed_hosts))
    try:
        info = pf.torrent(repo)
        if not info or not info.get("magnet"):
            print("Pirate Face has no torrent for it yet, so there's nothing to seed until someone adds one.")
            if args.rescue or (sys.stdin.isatty() and _yes(
                    "Ask Pirate Face to add it? (sends only the model's name; its volunteers build the torrent "
                    "from Hugging Face)", False)):
                _ask_rescue(pf, repo)
            else:
                print("  To ask later: flotilla add --rescue " + repo + "  (or upload it: https://pirateface.co/submit)")
        else:
            print(f"Pirate Face has it: {info.get('seeders', 0)} seeders, {info.get('leechers', 0)} leechers.")
    except FetchError:
        pass
    print("Flotilla picks it up at its next replan; press Replan now in the dashboard to do it right away.")
    return 0


def _ask_rescue(pf, repo: str) -> bool:
    from .netsafe import FetchError

    try:
        doc = pf.request_rescue(repo)
    except FetchError as e:
        print(f"Couldn't ask Pirate Face to add it ({e}). Try again later, or upload it at https://pirateface.co/submit")
        return False
    status = doc.get("status") or doc.get("message")
    print(f"Asked Pirate Face to add {repo}" + (f" ({status})." if isinstance(status, str) and status else ".")
          + " Flotilla picks it up once it's listed.")
    return True


def cmd_remove(args) -> int:
    from .feed import remove_pick

    cfg = load_config(Path(args.config))
    repo = remove_pick(cfg.local_feed, args.link)
    print(f"Removed {repo} from your picks." if repo else "That model isn't in your picks.")
    return 0 if repo else 1


def cmd_picks(args) -> int:
    from .feed import list_picks

    cfg = load_config(Path(args.config))
    picks = list_picks(cfg.local_feed)
    if not picks:
        print("No picks yet. Add one with: flotilla add <Hugging Face or Pirate Face link>")
        return 0
    for m in picks:
        scores = m.get("scores") or {}
        prio = max(scores.values()) if scores else "?"
        print(f"  {m['repo']}  priority {prio}  {m.get('note', '')}")
    return 0


def cmd_dashboard(args) -> int:
    import os
    import urllib.error
    import urllib.request
    import webbrowser

    from .access import ACCESS_ENV, full_address, listen_for, serve_command, tailscale_serving
    from .remote import dashboard_key

    cfg = load_config(Path(args.config))
    state_dir = Path(args.state)
    port = cfg.dashboard_port if args.port is None else args.port     # 0: any free port
    access = args.access or os.environ.get(ACCESS_ENV) or cfg.dashboard_access

    def tailnet_hint() -> None:
        if access == "tailnet" and tailscale_serving(port) is False:
            print(f"Tailscale isn't passing this on to the dashboard yet. Run once: {serve_command(port)}",
                  file=sys.stderr)

    if args.url:
        _, keyed = listen_for(access)
        from . import access as acc

        on_network = args.serve or os.environ.get(ACCESS_ENV) or acc.in_container()   # a dashboard container has a key
        print(full_address(access, port, dashboard_key(state_dir) if keyed or on_network else None))
        tailnet_hint()
        return 0
    if args.serve:
        import signal
        import threading

        from .dashboard import Dashboard
        from .remote import RemoteEngine

        on_network = args.listen not in ("127.0.0.1", "localhost", "::1")
        key = dashboard_key(state_dir) if on_network or access != "local" else None
        dash = Dashboard(RemoteEngine(state_dir), port, host=args.listen, key=key, access=access)
        dash.start()
        if access == "public":
            from .access import PUBLIC_WARNING

            print(f"WARNING: {PUBLIC_WARNING}", file=sys.stderr, flush=True)
        if key:
            print(f"Flotilla dashboard listening on {args.listen}:{dash.port}, for the node whose state is in {state_dir}.")
            print(f"Open {full_address(access if access != 'local' else 'lan', dash.port, key)}")
            print("Anyone with that address can steer this node; `flotilla dashboard --url` prints it again."
                  + ("" if access == "public" else " Visitors from the public internet are turned away."), flush=True)
        else:
            print(f"Flotilla dashboard: {dash.url} (for the node whose state is in {state_dir})", flush=True)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        stop.wait()
        dash.stop()
        return 0
    _, keyed = listen_for(cfg.dashboard_access)
    url = f"http://127.0.0.1:{cfg.dashboard_port}"
    try:
        urllib.request.urlopen(url + "/api/state", timeout=3).read()
    except urllib.error.HTTPError:
        pass                       # it answered (asking for its key): it's up
    except OSError:
        print(f"The dashboard isn't up: start Flotilla with `flotilla run` or `flotilla service install`. ({url})")
        return 1
    if keyed:
        key = dashboard_key(state_dir)
        url += f"/?key={key}"
        print(full_address(cfg.dashboard_access, cfg.dashboard_port, key))
        tailnet_hint()
    else:
        print(url)
    if not args.no_open:
        webbrowser.open(url)
    return 0


def cmd_service(args) -> int:
    from . import service

    config, state = Path(args.config).expanduser().absolute(), Path(args.state).expanduser().absolute()
    if args.action == "status":
        print(service.status())
        return 0
    if args.action == "uninstall":
        return 0 if service.execute(service.uninstall_plan(), args.dry_run) else 1
    if not config.exists():
        print("No config yet. Run `flotilla init` first.", file=sys.stderr)
        return 2
    ok = service.execute(service.install_plan(config, state, linger=args.linger), args.dry_run)
    if ok and not args.dry_run:
        cfg = load_config(config)
        print(f"Flotilla is running in the background. Dashboard: http://127.0.0.1:{cfg.dashboard_port}")
    return 0 if ok else 1


def cmd_uninstall(args) -> int:
    """Stop the service and, if asked, delete what Flotilla downloaded and wrote. Nothing else."""
    from . import service

    service.execute(service.uninstall_plan(), dry_run=False, say=lambda m: print(f"service: {m}"))
    if not args.delete_data:
        print("Kept your seeded files, config and state. Add --delete-data to remove them.")
        print("Remove the program itself with: uv tool uninstall flotilla-face")
        return 0
    config = Path(args.config).expanduser()
    state_dir = Path(args.state).expanduser()
    cfg = load_config(config)
    state = State(state_dir)
    targets = [Path(h["save_path"]) for h in state.holdings.values()] + [cfg.folder / ".probe"]
    targets = [t for t in targets if t.exists() and _inside_dir(cfg.folder, t)]
    held = sum(_du(t) for t in targets)
    print(f"This deletes {human(held)} of seeded files in {cfg.folder}, the state in {state_dir}, and {config}.")
    print("Only files Flotilla downloaded are removed; anything else in that folder is left alone.")
    if not args.yes and input("Type 'delete' to confirm: ").strip() != "delete":
        print("Nothing deleted.")
        return 1
    for t in targets:
        shutil.rmtree(t, ignore_errors=True)
    try:
        cfg.folder.rmdir()  # only if now empty
    except OSError:
        pass
    shutil.rmtree(state_dir, ignore_errors=True)
    config.unlink(missing_ok=True)
    leftover = [p.name for p in config.parent.glob("*")] if config.parent.exists() else []
    if leftover:
        print(f"Left in {config.parent}: {', '.join(leftover)} (not created by `flotilla init`).")
    else:
        try:
            config.parent.rmdir()
        except OSError:
            pass
    print("Done. Remove the program itself with: uv tool uninstall flotilla-face")
    return 0


def _inside_dir(base: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(base.resolve())
        return p.resolve() != base.resolve()
    except ValueError:
        return False


def _du(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_blocks * 512
        except OSError:
            pass
    return total


# ------------------------------------------------------------------ plan


def _discover(cfg: Config, state: State):
    from .candidates import gather
    from .sources import HuggingFace, PirateFace

    keys = _keys(cfg)
    feed = load_feed(cfg.feed_urls, keys, state.data["feed_version"], state.dir / "cache",
                     tuple(cfg.feed_allowed_hosts), log=log, bundled=BUNDLED_FEED)
    feed = apply_local(feed, cfg.local_feed, log=log)
    allowed = tuple(cfg.allowed_hosts)
    held = {h["repo"] for h in state.holdings.values()}
    ledger = load_ledger(keys, state.dir, tuple(cfg.feed_allowed_hosts), log=log)
    cands, rejected = gather(cfg, feed, PirateFace(allowed, cache_dir=state.dir / "cache"),
                             HuggingFace(allowed, state.dir / "cache"), held, log=log, ledger=ledger)
    block = set(state.data.get("blocklist", []))
    return feed, [c for c in cands if c.infohash not in block], rejected


def _keys(cfg: Config) -> list[PublicKey]:
    out = []
    for k in BUILTIN_EDITOR_KEYS + list(cfg.trusted_keys):
        try:
            out.append(PublicKey.parse(k))
        except SignatureError as e:
            log(f"ignoring bad trusted key: {e}")
    return out


def print_mismatches(cands) -> None:
    bad = [c for c in cands if c.manifest_mismatches]
    if not bad:
        return
    print("\nPirate Face manifest disagrees with Hugging Face or the hash ledger "
          "(verifying against those; worth reporting):")
    for c in bad:
        for m in c.manifest_mismatches:
            print(f"  {c.repo}@{c.revision[:12]}  {m['path']}")
            print(f"      pirateface  {m['pirateface']}")
            if "huggingface" in m:
                print(f"      huggingface {m['huggingface']}")
            else:
                print(f"      ledger      {m['ledger']}")


def print_plan(p: Plan, rejected, verbose: bool) -> None:
    print()
    print(f"Budget {human(p.budget_bytes)}   planned {human(p.total_bytes)} "
          f"({human(p.new_bytes)} new)   swarms {len(p.picks)}   est. fill {duration(p.est_fill_seconds)}")
    print()
    print(f"{'model':<52} {'prio':>4} {'hf':>4} {'seeds':>5} {'files':>9} {'size':>10}  why")
    print("-" * 110)
    for pick in sorted(p.picks.values(), key=lambda x: -x.value):
        c = pick.cand
        weights = [u for u in pick.units if not u.whole]
        rare = min((u.availability for u in pick.units), default=0)
        if pick.whole:
            why = f"whole model; held by {rare:g} other peer(s)"
        else:
            why = f"{len(weights)} shard(s); rarest held by {rare:g} other peer(s)"
        if rare >= c.target_replicas:
            why += f" (≥ target {c.target_replicas}: serving {c.leechers} leecher(s))"
        elif c.hf_backed is False:
            why += "; Hugging Face copy gone"
        hf = {True: "yes", False: "GONE", None: "?"}[c.hf_backed] + ("*" if c.manifest_mismatches else "")
        nfiles = f"{len(pick.files)}/{len([f for f in c.files if f.kind != 'skip'])}"
        name = c.repo if len(c.repo) <= 52 else c.repo[:49] + "..."
        print(f"{name:<52} {c.priority:>4.2f} {hf:>4} {c.seeders:>5} {nfiles:>9} {human(pick.bytes):>10}  {why}")
        if verbose:
            for u in sorted(pick.units, key=lambda u: -u.key)[:12]:
                print(f"    {u.label:<60} avail {u.availability:>4g}  rate {u.rate:.3f}/GB")
    if p.swap_out:
        gb = lambda units: sum(u.size for u in units) / GB
        print(f"\nNext reorientation: would drop {gb(p.swap_out):.1f} GB of the lowest-priority pieces held "
              f"for {gb(p.swap_in):.1f} GB the swarm needs more:")
        for sign, units in (("-", p.swap_out), ("+", p.swap_in)):
            for u in units[: (len(units) if verbose else 6)]:
                print(f"  {sign} {u.cand.repo}  {u.label}  ({human(u.size)}, held by {u.availability:g} other peer(s))")
            if not verbose and len(units) > 6:
                print(f"  {sign} … and {len(units) - 6} more (use -v)")
    elif p.churn_short:
        print(f"\nNext reorientation: nothing fits. The smallest swap worth making is "
              f"{p.churn_short / GB:.1f} GB, more than reorient.churn_gb allows.")
    if len(p.drops) > sum(len(u.files) for u in p.swap_out):
        print(f"\nWould drop {len(p.drops)} held file(s) that no longer fit the plan.")
    if rejected:
        print(f"\nSkipped {len(rejected)} model(s):")
        for r in rejected[: (len(rejected) if verbose else 12)]:
            print(f"  {r.repo}: {r.reason}")
        if not verbose and len(rejected) > 12:
            print(f"  … and {len(rejected) - 12} more (use -v)")


def cmd_plan(args) -> int:
    cfg = load_config(Path(args.config))
    state = State(Path(args.state))
    feed, cands, rejected = _discover(cfg, state)
    print(f"\nfeed: {feed.source} (v{feed.version}{', signer ' + feed.signer if feed.signer else ''})")
    for c in cands:
        h = state.holdings.get(c.infohash)
        if h:
            for f in c.files:
                e = h["files"].get(str(f.index))
                if e and e["status"] in ("wanted", "verifying", "verified"):
                    f.held, f.held_since = True, e.get("since")
    if args.probe:
        from .engine import apply_probe, probe

        result = probe(cands, cfg, cfg.folder / ".probe", cfg.probe_seconds,
                       f"-FL0100-{state.node_id[:6]}".encode(), log=log)
        apply_probe(cands, result)
    else:
        print("(no swarm probe: per-file availability estimated from seeder counts; use --probe for real counts)")
    cfg.folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(cfg.folder).free
    held_bytes = sum(e["size"] for h in state.holdings.values() for e in h["files"].values())
    budget = int(max(0, min(cfg.budget_gb * GB, held_bytes + free - cfg.reserve_gb * GB)))
    params = Params(budget, cfg.upload_bps, cfg.download_bps, cfg.max_torrents, cfg.spread, cfg.hold_bonus,
                    cfg.min_hold_days, int(cfg.whole_model_under_gb * GB), time.time(),
                    allot={r: int(gb * GB) for r, gb in feed.allot_gb.items()},
                    churn_bytes=int(cfg.churn_gb * GB), manual=feed.manual, focus=cfg.focus)
    p = make_plan(cands, params, state.node_id)
    if args.json:
        print(json.dumps({
            "budget_bytes": p.budget_bytes, "total_bytes": p.total_bytes, "new_bytes": p.new_bytes,
            "picks": [{"repo": pk.cand.repo, "infohash": ih, "bytes": pk.bytes, "whole": pk.whole,
                       "files": [f.repo_path for f in pk.files]} for ih, pk in p.picks.items()],
            "rejected": [{"repo": r.repo, "reason": r.reason} for r in rejected],
            "manifest_mismatches": [{"repo": c.repo, "revision": c.revision, **m}
                                    for c in cands for m in c.manifest_mismatches],
        }, indent=1))
    else:
        print_plan(p, rejected, args.verbose)
        print_mismatches(cands)
    return 0


# ------------------------------------------------------------------ run / status / verify


def cmd_run(args) -> int:
    from .engine import Engine

    cfg = load_config(Path(args.config))
    if not Path(args.config).exists():
        print("No config yet. Run `flotilla init` first.", file=sys.stderr)
        return 2
    state = State(Path(args.state))
    eng = Engine(cfg, state, log=log)
    where = f" bound to {cfg.bind_interface}" if cfg.bind_interface else ""
    log(f"node {state.node_id[:8]} listening on port {eng.ses.listen_port()}{where}; Ctrl-C to stop")
    until = time.time() + args.hours * 3600 if args.hours else None
    eng.run(once=args.once, until=until)
    return 0


def cmd_history(args) -> int:
    from .history import History

    path = Path(args.state) / "history.db"
    if not path.exists():
        print("No history yet. It starts with `flotilla run` and adds a point every minute.")
        return 1
    tz = int(datetime.now().astimezone().utcoffset().total_seconds())
    h = History(path)
    try:
        doc = h.query(args.range, args.model, tz)
    finally:
        h.close()
    if args.csv:
        print("start,upload_bytes,download_bytes,seconds_seeding")
        for t, up, down, online in doc["points"]:
            print(f"{datetime.fromtimestamp(t).astimezone().isoformat()},{up},{down},{online}")
        return 0
    span = max(1, min(time.time(), doc["end"]) - doc["start"])
    what = f" for {args.model}" if args.model else ""
    print(f"{args.range}{what}: up {human(doc['up'])}, down {human(doc['down'])}, "
          f"seeding {min(1.0, doc['online'] / span):.0%} of the time, "
          f"averaging {human(doc['up'] / doc['online']) if doc['online'] else '0 B'}/s up while seeding")
    if doc["models"] and not args.model:
        print(f"\n{'model':<64} {'up':>10} {'down':>10}")
        for m in doc["models"]:
            print(f"{m['repo'][:64]:<64} {human(m['up']):>10} {human(m['down']):>10}")
    print("\n--csv prints every point; --model author/model shows one model.")
    return 0


def cmd_status(args) -> int:
    path = Path(args.state) / "status.json"
    if not path.exists():
        print("No status yet. Is `flotilla run` running?")
        return 1
    doc = json.loads(path.read_text())
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(doc["at"])).total_seconds()
    up = doc.get("upload_month", {})
    print(f"node {doc['node']}  port {doc['listen_port']}  {'PAUSED  ' if doc.get('paused') else ''}"
          f"status {age:.0f}s old  uploaded this month {human(up.get('bytes', 0))}")
    fwd, reach = doc.get("vpn_port"), doc.get("reach") or {}
    if fwd and fwd.get("port"):
        print(f"inbound: {fwd['source']} forwards port {fwd['port']}: peers can connect in")
    elif fwd:
        print(f"inbound: no port forwarded: {fwd['note']}")
    if reach:
        how = ("dialing out harder, as peers can't connect in" if reach.get("dialing_out")
               else "peers can connect in")
        punched = f" ({reach['peers_holepunched']} hole-punched)" if reach.get("peers_holepunched") else ""
        print(f"peers: {reach.get('peers_in', 0)} connected in, {reach.get('peers_out', 0)} dialed{punched}; {how}")
    if doc.get("feed"):
        print(f"feed v{doc['feed']['version']} from {doc['feed']['source']}")
    up = doc.get("update")
    if up:
        how = "on the machine running Docker: flotilla update" if up.get("where") == "docker" else "flotilla update"
        print(f"Flotilla {up['version']} is out ({up['page']}). To install it, {how}")
    print(f"\n{'model':<48} {'state':<20} {'verified':>9} {'held':>10} {'up/s':>9} {'total up':>10} {'peers':>5}")
    for t in sorted(doc["torrents"], key=lambda t: -t["bytes"]):
        prog = f"{t['done'] / t['wanted']:.0%}" if t["wanted"] else "-"
        name = t["repo"] if len(t["repo"]) <= 48 else t["repo"][:45] + "..."
        state = t["state"] if t["state"] not in ("downloading",) else f"downloading {prog}"
        print(f"{name:<48} {state:<20} {t['verified']:>4}/{t['files']:<4} {human(t['bytes']):>10} "
              f"{human(t['up_rate']):>9} {human(t['uploaded']):>10} {t['peers']:>5}")
    flagged = [t for t in doc["torrents"] if t.get("manifest_mismatches")]
    if flagged:
        print("\nPirate Face's manifest disagrees with Hugging Face for these (verified against Hugging Face):")
        for t in flagged:
            print(f"  {t['repo']}: {', '.join(m['path'] for m in t['manifest_mismatches'])}")
    print()
    _print_per_file(doc)
    _print_gated(doc.get("hf_gated") or [])
    return 0


def cmd_verify(args) -> int:
    from .sources import git_blob_sha1, sha256_file

    if args.folder:
        return verify_folder(args)
    state = State(Path(args.state))
    bad = good = 0
    for ih, hold in state.holdings.items():
        base = Path(hold["save_path"])
        for k, e in hold["files"].items():
            if e["status"] not in ("verified", "failed"):
                continue
            p = base / e["tpath"]
            try:
                ok = (sha256_file(p) == e["sha256"]) if e.get("sha256") else (git_blob_sha1(p, e["size"]) == e["git_oid"])
            except OSError as err:
                ok = False
                print(f"  {hold['repo']}/{e['path']}: {err}")
            good += ok
            bad += not ok
            if not ok:
                print(f"BAD  {hold['repo']}/{e['path']}")
    print(f"{good} file(s) verified, {bad} bad")
    return 1 if bad else 0


def _load_ledger_for(args, offline: bool = False):
    from .ledger import DEFAULT_LEDGER_URL

    cfg = load_config(Path(args.config))
    quiet = lambda msg: None if msg.startswith("hash ledger: published copy unavailable") else log(msg)  # noqa: E731
    return load_ledger(_keys(cfg), Path(args.state), tuple(cfg.feed_allowed_hosts),
                       url=None if offline else DEFAULT_LEDGER_URL, log=quiet)


def verify_folder(args) -> int:
    """Check a downloaded model folder against the hash ledger. Needs no running node."""
    from .ledger import check_file, folder_extras, match_folder

    led = _load_ledger_for(args, offline=args.offline)
    if not led.entries:
        print("The hash ledger has no entries (no signed copy could be loaded).")
        return 1
    entry, root = match_folder(Path(args.folder).expanduser(), led, args.repo, args.revision)
    src = {"huggingface": "Hugging Face", "pirateface": "Pirate Face (Hugging Face no longer had it)"}[entry.source]
    print(f"{entry.repo} at {entry.revision}")
    print(f"  hashes recorded {entry.captured} from {src}; ledger: {', '.join(led.sources) or 'local cache'}")
    print(f"  checking {root}")
    good = bad = missing = 0
    for lf in sorted(entry.files.values(), key=lambda f: f.path):
        p = root / lf.path
        if not p.is_file():
            missing += 1
            continue
        ok, why = check_file(p, lf)
        good += ok
        bad += not ok
        print(f"OK    {lf.path}" if ok else f"BAD   {lf.path}: {why}", flush=True)
    extras = folder_extras(root, entry)
    for x in extras:
        print(f"EXTRA {x}  (not part of this model; don't load it)")
    print(f"\n{good} good, {bad} bad, {missing} not in this folder, {len(extras)} extra")
    for c in led.conflicts:
        if c["key"] == entry.key:
            print(f"WARNING: another copy of the ledger lists a different record for this model ({c['refused'][:12]}…); "
                  f"this check used the first one seen ({c['kept'][:12]}…).")
    if entry.stamp:
        print(f"Timestamp proof: {entry.stamp}\n  check it against Bitcoin with: ots verify {entry.stamp}")
    else:
        print("No timestamp proof for this record in this install.")
    return 1 if bad or extras else 0


def cmd_ledger(args) -> int:
    from .ledger import accept_override

    if args.accept:
        key, sha = args.accept
        accept_override(Path(args.state), key, sha)
        print(f"This node now accepts ledger entry {sha[:12]}… for {key}. It takes effect on the next refresh.")
        return 0
    led = _load_ledger_for(args, offline=args.offline)
    print(f"hash ledger: {len(led.entries)} model revision(s); sources: {', '.join(led.sources) or 'local cache only'}")
    for e in sorted(led.entries.values(), key=lambda e: e.key):
        stamp = "stamped" if e.stamp else "-"
        print(f"  {e.repo}@{e.revision[:12]}  {len(e.files):4} files  {e.source:11}  {e.captured[:10]}  {stamp}")
    for c in led.conflicts:
        print(f"CONFLICT {c['key']}: {c['source']} lists {c['refused']}, kept {c['kept']}")
    return 1 if led.conflicts else 0


# ------------------------------------------------------------------ feed + editor


def cmd_feed(args) -> int:
    cfg = load_config(Path(args.config))
    if args.init_local:
        if cfg.local_feed.exists():
            print(f"{cfg.local_feed} already exists; edit it directly.")
            return 1
        cfg.local_feed.parent.mkdir(parents=True, exist_ok=True)
        cfg.local_feed.write_text(json.dumps(LOCAL_EXAMPLE, indent=2) + "\n")
        print(f"Wrote {cfg.local_feed}. Edit it, then `flotilla feed` to check. Flotilla picks it up on its next replan.")
        return 0
    state = State(Path(args.state))
    feed = load_feed(cfg.feed_urls, _keys(cfg), state.data["feed_version"], state.dir / "cache",
                     tuple(cfg.feed_allowed_hosts), log=log, bundled=BUNDLED_FEED)
    feed = apply_local(feed, cfg.local_feed, log=log)
    print(f"source {feed.source}\nversion {feed.version}  signer {feed.signer or '-'}  expires {feed.expires.isoformat()}")
    if feed.local_source:
        print(f"plus your changes from {feed.local_source}")
    print(f"weights {feed.weights}")
    for m in sorted(feed.models, key=lambda m: -feed.priority(m)):
        pin = " (pinned)" if m.infohash or m.revision else ""
        mine = " (yours)" if m.local else ""
        print(f"  {feed.priority(m):.2f}  {m.repo}{pin}{mine}  {m.scores}  {m.note}")
        ov = origin_view(m.repo, m.origin)
        if ov:
            print(f"        origin: {ov['text']}" + (f"  [{ov['tag']}]" if ov["tag"] else ""))
    if feed.deny:
        print(f"deny: {sorted(feed.deny)}")
    return 0


# ------------------------------------------------------------------ update


def _install_kind() -> str:
    """How this copy of Flotilla was installed: "uv-tool" (the installer), "pip", or "dev" (a checkout)."""
    import subprocess

    import flotilla

    here = Path(flotilla.__file__).resolve()
    if "site-packages" not in here.parts:
        return "dev"
    uv = _uv()
    if uv:
        try:
            tools = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            tools = ""
        if tools and Path(sys.prefix).resolve().is_relative_to(Path(tools).resolve()):
            return "uv-tool"
    return "pip"


def _container_label(e, rel) -> str:
    """What updating a container would change. One set up before there were images installs the
    newest code from GitHub each time it starts, unsigned: it moves to the signed image."""
    if e.image.split("@")[0].split(":")[0] != rel.image.split("@")[0].split(":")[0]:
        return f"installs unsigned code from GitHub when it starts → the signed {rel.version} image"
    return "→ " + rel.version


def _uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    for p in (Path.home() / ".local" / "bin" / "uv", Path.home() / ".cargo" / "bin" / "uv"):
        if p.exists():
            return str(p)
    return None


def _install_wheel(kind: str, wheel: Path) -> bool:
    import subprocess

    import importlib.util

    if kind == "uv-tool":
        cmd = [_uv() or "uv", "tool", "install", "--force", "--python",
               f"{sys.version_info.major}.{sys.version_info.minor}", str(wheel)]
    elif importlib.util.find_spec("pip") is None and _uv():     # environments uv made have no pip
        cmd = [_uv(), "pip", "install", "--python", sys.executable, "--upgrade", str(wheel)]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", str(wheel)]
    ui.info("installing: " + " ".join(cmd[:4]) + " …")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        ui.caution(f"install failed:\n{(r.stderr or r.stdout).strip()[-2000:]}")
        return False
    return True


def cmd_update(args) -> int:
    from . import containers as dc
    from . import release, service
    from .access import in_container
    from .netsafe import FetchError

    if in_container():
        print("This is Flotilla inside a Docker container. Update from the machine running Docker, where you "
              "set it up: flotilla update")
        return 2
    try:
        rel = release.latest()
    except (release.ReleaseError, FetchError) as e:
        print(f"Couldn't check for a new release: {e}", file=sys.stderr)
        return 1
    here = release.current()
    kind = _install_kind()
    native = release.newer(rel.version, here) and kind != "dev"
    try:
        for name in dc.recover() if shutil.which("docker") else []:
            ui.info(f"put back {name}, which an interrupted update had left renamed")
        ours = {n: e for n, e in dc.installed().items() if e.ours} if shutil.which("docker") else {}
    except Exception:          # Docker installed but not answering: nothing of ours to update there
        ours = {}
    stale = {n: e for n, e in ours.items() if e.image != rel.image}

    ui.title(f"Flotilla {rel.version}", rel.page)
    ui.table([("This machine", f"{here}" + (" (a development checkout: update it with git)" if kind == "dev"
                                            else " → " + rel.version if native else ", up to date"))]
             + [(f"Docker: {n}", _container_label(e, rel) if n in stale else "up to date") for n, e in ours.items()])
    if not native and not stale:
        ui.done("Everything is up to date")
        return 0
    if rel.notes:
        print()
        for line in rel.notes.splitlines()[:40]:
            print(f"  {line}")
    if args.check:
        print()
        ui.info("run `flotilla update` to install it")
        return 0
    print()
    try:
        if not args.yes and not _yes("Update now", True):
            return 0
    except EOFError:            # no one to ask (input closed): don't update unasked
        print("\nNot updating: no answer. Run `flotilla update --yes` to update without being asked.")
        return 1

    failed = False
    if native:
        try:
            wheel = release.download(rel, rel.wheel)
        except (release.ReleaseError, FetchError) as e:
            ui.caution(str(e))
            return 1
        ui.done(f"downloaded {wheel.name}, matching the signed release")
        if _install_wheel(kind, wheel):
            ui.done(f"Flotilla {rel.version} installed")
            if service.installed():
                ok = service.execute(service.restart_plan(), dry_run=False, say=ui.info)
                failed |= not ok
            if stale:   # the containers are updated by the version just installed, not this one
                import subprocess

                sys.stdout.flush()      # so this copy's output comes before the new one's, even in a log
                r = subprocess.run([*service.executable(), "update", "--yes"])
                return 1 if failed or r.returncode else 0
        else:
            failed = True
    if stale:
        try:
            ui.info(f"downloading the {rel.version} image")
            dc.pull(rel.image)
            for name in (dc.DASHBOARD, dc.NODE):       # dashboard first: a failed node update stays visible
                if name in stale:
                    dc.replace(stale[name], rel.image)
                    ui.done(f"{name} now runs {rel.version}")
        except dc.DockerError as e:
            ui.caution(str(e))
            failed = True
    return 1 if failed else 0


def _editor_release(args) -> int:
    """Check, sign and publish the draft release CI built for a tag (see .github/workflows/release.yml)."""
    import subprocess
    import tempfile

    from . import release

    tag = (args.file or "").strip()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("give the release tag, e.g. flotilla editor release v0.2.0")
    version = tag[1:]
    if not shutil.which("gh"):
        raise ValueError("needs the GitHub CLI (gh), logged in to the repository")

    def gh(*a: str, capture: bool = True) -> str:
        r = subprocess.run(["gh", *a, "--repo", release.REPO], capture_output=capture, text=True)
        if r.returncode:
            raise ValueError(f"gh {' '.join(a)} failed: {(r.stderr or '').strip()}")
        return r.stdout or ""

    work = Path(tempfile.mkdtemp(prefix="flotilla-release-"))
    dist = work / "dist"
    dist.mkdir()
    gh("release", "download", tag, "--dir", str(dist), "--pattern", "*.whl", "--pattern", "*.tar.gz",
       "--pattern", "image.txt")
    image = (dist / "image.txt").read_text().strip()
    (dist / "image.txt").unlink()
    wheels = [p.name for p in dist.glob("*.whl")]
    if wheels != [f"flotilla_face-{version}-py3-none-any.whl"]:
        raise ValueError(f"expected the {version} wheel in the draft release, found {wheels}")
    notes = gh("release", "view", tag, "--json", "body", "-q", ".body")
    manifest = release.make_manifest(version, notes, image, dist)
    sk = SecretKey.load(Path(args.secret).expanduser())
    sig = sk.sign(manifest, trusted_comment=release.trusted_comment(version),
                  untrusted_comment=f"Flotilla {version} release, signed with key {sk.public.key_id_hex}")
    rel = release.parse(manifest, sig, release.builtin_keys())   # what every install will check
    (work / release.MANIFEST).write_bytes(manifest)
    (work / release.SIGNATURE).write_text(sig)
    print(f"Flotilla {rel.version}: {', '.join(rel.files)}; image {rel.image}")
    if args.dry_run:
        print(f"Not published (--dry-run). Signed manifest: {work / release.MANIFEST}")
        return 0
    gh("release", "upload", tag, str(work / release.MANIFEST), str(work / release.SIGNATURE), "--clobber")
    gh("release", "edit", tag, "--draft=false", "--latest")
    print(f"Published {rel.page}")
    return 0


def cmd_editor(args) -> int:
    if args.action == "release":
        return _editor_release(args)
    if args.action == "keygen":
        sk = SecretKey.generate()
        out = Path(args.secret).expanduser()
        sk.save(out)
        pub = out.with_suffix(".pub")
        pub.write_text(f"untrusted comment: minisign public key {sk.public.key_id_hex}\n{sk.public.line()}\n")
        print(f"secret key: {out} (mode 0600; keep it offline and backed up)")
        print(f"public key: {pub}")
        print(f"\nPaste this into BUILTIN_EDITOR_KEYS in flotilla/config.py:\n  {sk.public.line()}")
        return 0
    if args.action == "sign":
        sk = SecretKey.load(Path(args.secret).expanduser())
        path = Path(args.file)
        data = path.read_bytes()
        if _is_ledger(data):
            _sign_ledger(path, sk)
            print(f"Timestamp new entries next: flotilla editor stamp {path}")
            return 0
        feed = parse_feed(data)  # refuse to sign something clients would reject
        if feed.expires <= datetime.now(timezone.utc):
            raise FeedError("feed is already expired; bump `expires`")
        tc = f"timestamp:{int(time.time())}\tfile:{path.name}\tversion:{feed.version}"
        Path(str(path) + ".minisig").write_text(sk.sign(data, trusted_comment=tc))
        print(f"signed {path} (v{feed.version}, {len(feed.models)} models) -> {path}.minisig")
        index_path = _ledger_index_path(args)
        if args.no_ledger or not (args.ledger or index_path.exists()):
            missing = _unledgered(feed, index_path)
            if missing:
                print(f"\nNote: {len(missing)} pinned model(s) have no hash-ledger entry yet:")
                for m in missing:
                    print(f"  {m}")
                print(f"Record them while Hugging Face still has them: flotilla editor ledger-capture {path}")
            return 0
        return update_ledger(feed, index_path, sk)
    if args.action == "check":
        from .ledger import check_index_files

        path = Path(args.file)
        keys = [PublicKey.parse(Path(k).expanduser().read_text()) for k in args.pubkey] if args.pubkey else _keys(load_config(Path(args.config)))
        key, tc = verify(path.read_bytes(), Path(str(path) + ".minisig").read_text(), keys)
        if _is_ledger(path.read_bytes()):
            version, n = check_index_files(path)
            print(f"OK: signed by {key.key_id_hex}; hash ledger v{version}; {n} entries, all unedited; "
                  f"trusted comment: {tc}")
            return 0
        feed = parse_feed(path.read_bytes())
        print(f"OK: signed by {key.key_id_hex}; v{feed.version}; expires {feed.expires.isoformat()}; trusted comment: {tc}")
        return 0
    if args.action == "ledger-capture":
        return _ledger_capture(args)
    if args.action == "stamp":
        return _ledger_stamp(args)
    if args.action in ("sync-worth-rescuing", "sync-fallback"):
        return _editor_sync(args)
    if args.action == "template":
        now = datetime.now(timezone.utc).replace(microsecond=0)
        doc = {
            "schema": 1, "version": 1, "issued": now.isoformat(), "expires": (now + timedelta(days=30)).isoformat(),
            "weights": {"capability": 0.35, "cyber": 0.25, "takedown_risk": 0.40},
            "defaults": {"target_replicas": 5, "unlisted_priority": 0.15},
            "models": [{"repo": "author/model", "scores": {"capability": 50, "cyber": 50, "takedown_risk": 50},
                        "target_replicas": 5, "note": "why this model matters"}],
            "deny": [],
        }
        print(json.dumps(doc, indent=2))
        return 0
    return 2


def _editor_sync(args) -> int:
    """sync-worth-rescuing / sync-fallback: update the list's automatically chosen entries."""
    import tempfile

    from .config import Config
    from .netsafe import DEFAULT_ALLOWED_HOSTS, fetch
    from .sources import HuggingFace, PirateFace
    from .worthrescuing import license_mismatches

    path = Path(args.file)
    doc = json.loads(path.read_text())
    licenses = set(Config().licenses)
    with tempfile.TemporaryDirectory() as tmp:
        pf, hf = PirateFace(DEFAULT_ALLOWED_HOSTS), HuggingFace(DEFAULT_ALLOWED_HOSTS, Path(tmp))
        if args.action == "sync-worth-rescuing":
            from .worthrescuing import PAGE_URL, parse_page, sync

            html = fetch(PAGE_URL, allowed=DEFAULT_ALLOWED_HOSTS, max_bytes=8 << 20, accept="text/html").body
            page = parse_page(html.decode("utf-8", "replace"))
            print(f"{PAGE_URL}: {len(page)} models")
            summary = sync(doc, page, pf, hf, licenses)
        else:
            from . import fallback

            groups = fallback.group_hits(fallback.gather_hits(hf))
            print(f"{len(groups)} refusal-removed builds on Hugging Face (quantisations grouped with their source)")
            summary = fallback.sync(doc, groups, pf, hf, licenses, max_entries=args.max)
        mismatches = license_mismatches(doc, hf)
    for k in ("added", "updated", "removed", "kept", "skipped"):
        for line in summary[k]:
            print(f"  {k:8} {line}")
    for line in mismatches:
        print(f"  license  {line}")
    if not (summary["added"] or summary["updated"] or summary["removed"]):
        print("No changes to the list.")
        return 0
    if args.dry_run:
        print("Dry run: nothing written.")
        return 0
    now = datetime.now(timezone.utc).replace(microsecond=0, second=0)
    doc["version"] = int(doc.get("version", 0)) + 1
    doc["issued"], doc["expires"] = now.isoformat(), (now + timedelta(days=90)).isoformat()
    parse_feed(json.dumps(doc).encode())   # refuse to write something clients would reject
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {path} (v{doc['version']}). Review it, then: flotilla editor sign {path}\n"
          f"(signing also records new models in the hash ledger)")
    return 0


def _is_ledger(data: bytes) -> bool:
    from .ledger import KIND

    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(doc, dict) and doc.get("kind") == KIND


def _unledgered(feed, index_path: Path) -> list[str]:
    from .ledger import ledger_key, parse_index

    try:
        _, index = parse_index(index_path.read_bytes())
    except (OSError, LedgerError):
        index = {}
    return sorted({f"{m.repo}@{m.revision}" for m in feed.models
                   if m.revision and ledger_key(m.repo, m.revision) not in index})


def _ledger_index_path(args) -> Path:
    from .ledger import BUNDLED_LEDGER

    if args.ledger:
        return Path(args.ledger)
    if args.action == "stamp" and args.file:
        return Path(args.file)
    if args.action in ("ledger-capture", "sign") and args.file:
        return Path(args.file).parent / "ledger" / "index.json"
    return BUNDLED_LEDGER


def record_new_models(feed, index_path: Path, hf=None, pf=None, repo: str | None = None,
                      dry_run: bool = False) -> tuple[int, int]:
    """Add a ledger entry for every pinned model in the feed that has none yet. Returns (added, failed).

    Entries are captured from Hugging Face while it still has the model, so this is worth running
    as soon as a model joins the list."""
    import tempfile

    from .ledger import add_entry, capture, ledger_key, read_index_doc
    from .netsafe import DEFAULT_ALLOWED_HOSTS, FetchError, UnsafeURL
    from .sources import HuggingFace, PirateFace

    have = {ledger_key(*k.rsplit("@", 1)) for k in read_index_doc(index_path)["entries"]}
    added = failed = known = 0
    with tempfile.TemporaryDirectory() as tmp:
        pf = pf or PirateFace(DEFAULT_ALLOWED_HOSTS)
        hf = hf or HuggingFace(DEFAULT_ALLOWED_HOSTS, Path(tmp))
        for m in feed.models:
            if repo and m.repo.lower() != repo.lower():
                continue
            if not m.revision:
                print(f"  skip   {m.repo}: no pinned revision")
                continue
            key = ledger_key(m.repo, m.revision)
            if key in have:
                known += 1
                continue
            try:
                doc, note = capture(m.repo, m.revision, hf, pf)
            except (FetchError, UnsafeURL) as e:
                print(f"  FAILED {m.repo}@{m.revision[:12]}: {e}")
                failed += 1
                continue
            if doc is None:
                print(f"  skip   {m.repo}@{m.revision[:12]}: {note}")
                continue
            have.add(key)
            if dry_run:
                print(f"  would add {m.repo}@{m.revision[:12]}: {note}")
                continue
            add_entry(index_path, doc)
            added += 1
            print(f"  added  {m.repo}@{m.revision[:12]}: {note}")
    if known:
        print(f"  {known} pinned model(s) already recorded")
    return added, failed


def _ledger_capture(args) -> int:
    """Record the hashes of every pinned model in the priority list that has no ledger entry yet."""
    feed_path = Path(args.file) if args.file else BUNDLED_FEED
    index_path = _ledger_index_path(args)
    added, failed = record_new_models(parse_feed(feed_path.read_bytes()), index_path, repo=args.repo,
                                      dry_run=args.dry_run)
    if added:
        print(f"\nAdded {added} entr{'y' if added == 1 else 'ies'} to {index_path}. Review them, then:\n"
              f"  flotilla editor sign {index_path}\n  flotilla editor stamp {index_path}")
    elif not args.dry_run and not failed:
        print("Nothing new to record.")
    return 1 if failed else 0


def _sign_ledger(path: Path, sk: SecretKey) -> None:
    from .ledger import check_index_files

    version, n = check_index_files(path)   # every entry present and unedited
    tc = f"timestamp:{int(time.time())}\tfile:{path.name}\tversion:{version}"
    Path(str(path) + ".minisig").write_text(sk.sign(path.read_bytes(), trusted_comment=tc))
    print(f"signed {path} (hash ledger v{version}, {n} entries) -> {path}.minisig")


def _ledger_sig_ok(path: Path, key: PublicKey) -> bool:
    try:
        verify(path.read_bytes(), Path(str(path) + ".minisig").read_text(), [key])
        return True
    except (OSError, SignatureError):
        return False


# OpenTimestamps attestation tags (python-opentimestamps, core/notary.py).
OTS_PENDING = bytes.fromhex("83dfe30d2ef90c8e")
OTS_BITCOIN = bytes.fromhex("0588960d73d71901")


def _ledger_files(index_path: Path) -> list[Path]:
    from .ledger import parse_index

    _, index = parse_index(index_path.read_bytes())
    return [index_path.parent / f for f, _ in index.values()]


def _stamp_new(ots: str, index_path: Path) -> tuple[list[Path], int]:
    """`ots stamp` every entry that has no proof yet. Returns (stamped, return code)."""
    import subprocess

    new = [f for f in _ledger_files(index_path) if not Path(str(f) + ".ots").exists()]
    if not new:
        return [], 0
    print(f"Stamping {len(new)} entr{'y' if len(new) == 1 else 'ies'}: only their SHA-256 is sent to the public "
          f"OpenTimestamps calendars.")
    r = subprocess.run([ots, "stamp", *map(str, new)], capture_output=True, text=True)
    if r.returncode:
        print((r.stderr or r.stdout).strip())
    return new, r.returncode


def _upgrade_pending(ots: str, index_path: Path, skip: set[Path] = frozenset()) -> tuple[int, int]:
    """Complete proofs whose Bitcoin transaction has confirmed. Returns (completed now, still pending).

    `ots upgrade` stops at the first incomplete proof, so each one is upgraded on its own; proofs
    that already carry a Bitcoin attestation are skipped without asking the calendars."""
    import subprocess

    completed = pending = 0
    for f in _ledger_files(index_path):
        proof = Path(str(f) + ".ots")
        if f in skip or not proof.exists():
            continue
        data = proof.read_bytes()
        if OTS_BITCOIN in data or OTS_PENDING not in data:
            continue
        r = subprocess.run([ots, "upgrade", str(proof)], capture_output=True, text=True)
        Path(str(proof) + ".bak").unlink(missing_ok=True)   # ots keeps the old proof beside the new one
        if r.returncode == 0 and OTS_BITCOIN in proof.read_bytes():
            completed += 1
        else:
            pending += 1
    return completed, pending


def _no_ots() -> None:
    print("The `ots` command isn't installed, so nothing was timestamped. Install the OpenTimestamps client "
          "(an editor-only tool):\n  uv tool install opentimestamps-client   (or: pip install opentimestamps-client)")


def _stamp_report(stamped: list[Path], completed: int, pending: int) -> None:
    if stamped:
        print(f"Stamped {len(stamped)} new entr{'y' if len(stamped) == 1 else 'ies'}; their proofs complete in a "
              f"few hours, once the calendars' Bitcoin transaction confirms.")
    if completed:
        print(f"Completed {completed} proof(s): they now verify from Bitcoin alone, without the calendars.")
    if pending:
        print(f"{pending} proof(s) still waiting for Bitcoin; the next `flotilla editor sign` or "
              f"`flotilla editor stamp --upgrade` completes them.")


def _ledger_stamp(args) -> int:
    """OpenTimestamps proofs for entry files: stamp new ones (and complete any that are ready), or with
    --upgrade only complete pending ones."""
    ots = shutil.which("ots")
    if not ots:
        _no_ots()
        return 1
    index_path = _ledger_index_path(args)
    stamped, rc = ([], 0) if args.upgrade else _stamp_new(ots, index_path)
    completed, pending = _upgrade_pending(ots, index_path, skip=set(stamped))
    if not (stamped or completed or pending):
        print("Every entry has a complete timestamp proof.")
    _stamp_report(stamped, completed, pending)
    if stamped or completed:
        print("Commit the new and changed .ots files.")
    return rc


def update_ledger(feed, index_path: Path, sk: SecretKey, hf=None, pf=None) -> int:
    """The ledger half of signing the priority list: record new pinned models, sign the ledger if it
    changed, stamp new entries and complete pending proofs."""
    print(f"\nHash ledger ({index_path}):")
    added, failed = record_new_models(feed, index_path, hf, pf)
    if not index_path.exists():
        print("  nothing recorded, so there is no ledger to sign")
        return 1 if failed else 0
    if added or not _ledger_sig_ok(index_path, sk.public):
        _sign_ledger(index_path, sk)
    else:
        print("  ledger unchanged; its signature is current")
    ots = shutil.which("ots")
    if ots:
        stamped, rc = _stamp_new(ots, index_path)
        completed, pending = _upgrade_pending(ots, index_path, skip=set(stamped))
        _stamp_report(stamped, completed, pending)
        failed += bool(rc)
    else:
        _no_ots()
    if failed:
        print("Some steps failed (see above). Fix them and sign again: finished steps are skipped.")
    print("Commit the priority list and its .minisig, plus the ledger's new entries, index.json, "
          "index.json.minisig and .ots files.")
    return 1 if failed else 0


# ------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="flotilla", description="Flotilla Face: a shard-level seeding router for Pirate Face.")
    ap.add_argument("--config", default=str(CONFIG_PATH), help=f"config file (default {CONFIG_PATH})")
    ap.add_argument("--state", default=str(STATE_DIR), help=f"state dir (default {STATE_DIR})")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="interactive setup: disk, bandwidth, licenses")
    p = sub.add_parser("plan", help="show what this node would seed (no downloading)")
    p.add_argument("--probe", action="store_true", help="briefly join candidate swarms for real per-file counts")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("run", help="download assigned files and seed them; replans every few hours")
    p.add_argument("--once", action="store_true", help="exit when everything assigned is downloaded and verified")
    p.add_argument("--hours", type=float, help="seed for this many hours, then stop cleanly")
    sub.add_parser("status", help="show what the running node is doing")
    p = sub.add_parser("history", help="upload and download over time (hourly totals are kept for good)")
    p.add_argument("--range", default="30d", choices=["1h", "24h", "7d", "30d", "90d", "1y", "all"])
    p.add_argument("--model", help="only this model (author/model)")
    p.add_argument("--csv", action="store_true", help="every point as CSV, for a spreadsheet")
    p = sub.add_parser("add", help="prioritize a model: paste a Hugging Face or Pirate Face link (or author/model)")
    p.add_argument("link")
    p.add_argument("--priority", type=float, default=100, help="0-100 (default 100: top priority)")
    p.add_argument("--note", help="why you care about it")
    p.add_argument("--rescue", action="store_true",
                   help="if Pirate Face has no torrent for it, ask Pirate Face to add it without asking first")
    p = sub.add_parser("remove", help="remove a model you added with `flotilla add`")
    p.add_argument("link")
    sub.add_parser("picks", help="list the models you added")
    p = sub.add_parser("dashboard", help="open the local web dashboard of the running node")
    p.add_argument("--no-open", action="store_true", help="just print the address")
    p.add_argument("--serve", action="store_true",
                   help="serve the dashboard from this process, for a node running in another one that shares "
                        "its state folder (a node inside a VPN container, say)")
    p.add_argument("--listen", default="127.0.0.1",
                   help="with --serve: address to listen on, e.g. 0.0.0.0 to reach it from your network "
                        "(then it needs its access key)")
    p.add_argument("--port", type=int, help="with --serve: port (default: the config's dashboard port; 0 for any free one)")
    p.add_argument("--url", action="store_true", help="print the network dashboard's address with its access key")
    p.add_argument("--access", choices=["local", "lan", "tailnet", "public"],
                   help="who may reach it (default: the config's dashboard.access); anything but public turns "
                        "away visitors from public internet addresses")
    p = sub.add_parser("update", help="update to the newest signed release: this machine's Flotilla, "
                                      "and Flotilla's Docker containers if it set any up")
    p.add_argument("--check", action="store_true", help="only say whether there's a newer release")
    p.add_argument("--yes", action="store_true", help="don't ask before updating")
    p = sub.add_parser("hf", help="connect a Hugging Face account (a read-only token) for gated models")
    p.add_argument("action", choices=["connect", "disconnect", "status"])
    p.add_argument("--from-file", metavar="PATH",
                   help="connect: read the token from this file, or - for standard input, instead of asking")
    p = sub.add_parser("points", help="connect a Pirate Face account so your seeding earns points")
    p.add_argument("action", nargs="?", default="status", choices=["connect", "disconnect", "status"])
    p.add_argument("--from-file", metavar="PATH",
                   help="connect: read the seeding tracker address or key from this file, or - for standard input")
    p = sub.add_parser("service", help="run Flotilla in the background (launchd on macOS, systemd on Linux)")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument("--dry-run", action="store_true", help="show what would be written and run")
    p.add_argument("--linger", action="store_true", help="Linux: keep running while you're logged out")
    p = sub.add_parser("uninstall", help="stop the service; with --delete-data also remove seeded files and state")
    p.add_argument("--delete-data", action="store_true")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("verify", help="re-hash every held file against its published hash, or check a "
                                      "downloaded model folder against the hash ledger")
    p.add_argument("folder", nargs="?", help="a downloaded model folder to check against the hash ledger")
    p.add_argument("--repo", help="with a folder: which model it is (author/model), if it can't be told from the files")
    p.add_argument("--revision", help="with a folder: which revision (40-hex commit)")
    p.add_argument("--offline", action="store_true", help="use only the ledger shipped with Flotilla and its cache")
    p = sub.add_parser("ledger", help="show the hash ledger this node uses")
    p.add_argument("--accept", nargs=2, metavar=("REPO@REVISION", "DIGEST"),
                   help="deliberately accept a different ledger entry for one model (see `flotilla ledger`)")
    p.add_argument("--offline", action="store_true", help="use only the ledger shipped with Flotilla and its cache")
    p = sub.add_parser("feed", help="show the priority list in use (published + your own changes)")
    p.add_argument("--init-local", action="store_true", help="create your own my-priorities.json to edit")
    p = sub.add_parser("editor", help="tools for feed editors")
    p.add_argument("action", choices=["keygen", "sign", "check", "template", "sync-worth-rescuing",
                                      "sync-fallback", "ledger-capture", "stamp", "release"],
                   help="sync-worth-rescuing: update FILE from Pirate Face's Worth Rescuing page; "
                        "sync-fallback: update FILE's fallback entries (popular refusal-removed models "
                        "on Hugging Face that Pirate Face has torrents for); "
                        "ledger-capture: record hashes for FILE's pinned models in the hash ledger; "
                        "stamp: OpenTimestamps proofs for new ledger entries; "
                        "release: check, sign and publish the draft release CI built for a tag")
    p.add_argument("file", nargs="?", help="feed JSON or ledger index.json (sign/check), feed JSON "
                                           "(sync-*/ledger-capture), ledger index.json (stamp), "
                                           "release tag like v0.2.0 (release)")
    p.add_argument("--dry-run", action="store_true",
                   help="sync-*/ledger-capture: show changes, write nothing; release: sign but don't publish")
    p.add_argument("--max", type=int, default=40, help="sync-fallback: most fallback entries (default 40)")
    p.add_argument("--ledger", help="ledger-capture/stamp: the ledger index.json (default: next to the feed)")
    p.add_argument("--repo", help="ledger-capture: only this model")
    p.add_argument("--upgrade", action="store_true", help="stamp: complete proofs stamped a few hours ago")
    p.add_argument("--no-ledger", action="store_true",
                   help="sign: sign the priority list only, without recording new models in the hash ledger")
    p.add_argument("--secret", default="~/.config/flotilla/editor.key")
    p.add_argument("--pubkey", action="append", help="public key file(s) to check against")

    args = ap.parse_args(argv)
    handlers = {"init": cmd_init, "plan": cmd_plan, "run": cmd_run, "status": cmd_status,
                "service": cmd_service, "uninstall": cmd_uninstall, "dashboard": cmd_dashboard,
                "add": cmd_add, "remove": cmd_remove, "picks": cmd_picks,
                "verify": cmd_verify, "history": cmd_history, "feed": cmd_feed, "editor": cmd_editor,
                "ledger": cmd_ledger, "hf": cmd_hf, "update": cmd_update,
                "points": cmd_points}
    try:
        return handlers[args.cmd](args)
    except (FeedError, SignatureError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
