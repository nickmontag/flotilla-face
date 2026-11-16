"""Run Flotilla in the background: a launchd agent on macOS, a systemd user unit on Linux.

Both run as the current user (no root), start at login, restart if Flotilla
crashes, and stay out of the way: low CPU and I/O priority.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

LABEL = "flotilla.seeder"
UNIT = "flotilla.service"


@dataclass
class Step:
    describe: str
    cmd: list[str] | None = None
    write: tuple[Path, str] | None = None
    remove: Path | None = None
    check: bool = False  # fail the whole operation if this command fails


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


LOG_ENV = "FLOTILLA_LOG_FILE"   # set by the launchd agent: the log file this node may rotate
LOG_MAX = 10 * 1024 * 1024       # rotated past this...
LOG_KEEP = 3                     # ...keeping flotilla.log.1 to .3: at most about 40 MB in all
_log_checked = 0.0


def service_log(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "Logs" / "flotilla.log"


def rotate_own_log(max_bytes: int = LOG_MAX, keep: int = LOG_KEEP, every: float = 60.0) -> bool:
    """Rotate the background service's log file, which launchd writes and never rotates.

    Only that file: stdout must be the file FLOTILLA_LOG_FILE names (the launchd agent sets it),
    or, for agents installed before it did, the usual ~/Library/Logs/flotilla.log on macOS.
    Output you redirect yourself, a terminal, a pipe, journald or Docker is never touched.
    Past max_bytes, flotilla.log becomes flotilla.log.1 (older copies move up, the oldest past
    `keep` is deleted) and writing carries on in a fresh flotilla.log. Checked at most every
    `every` seconds. True if it rotated."""
    global _log_checked
    import stat
    import time

    now = time.monotonic()
    if now - _log_checked < every:
        return False
    _log_checked = now
    named = os.environ.get(LOG_ENV, "").strip()
    if named:
        path = Path(named)
    elif sys.platform == "darwin":
        path = service_log()
    else:
        return False
    try:
        out = os.fstat(1)
        if not stat.S_ISREG(out.st_mode) or out.st_size < max_bytes:
            return False
        st = path.stat()
        if (st.st_dev, st.st_ino) != (out.st_dev, out.st_ino):     # stdout is some other file
            return False
        sys.stdout.flush()
        sys.stderr.flush()
        for i in range(keep - 1, 0, -1):
            older = path.with_name(f"{path.name}.{i}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{i + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            err = os.fstat(2)
            os.dup2(fd, 1)
            if (err.st_dev, err.st_ino) == (out.st_dev, out.st_ino):
                os.dup2(fd, 2)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def executable() -> list[str]:
    """The command that runs this flotilla. Symlinks are kept as-is on purpose:
    `uv tool` puts a stable shim in ~/.local/bin that survives upgrades."""
    argv0 = Path(sys.argv[0])
    if argv0.name == "flotilla" and argv0.exists():
        return [str(argv0.absolute())]
    found = shutil.which("flotilla")
    if found:
        return [str(Path(found).absolute())]
    return [sys.executable, "-m", "flotilla.cli"]


def _args(exe: list[str], config: Path, state: Path) -> list[str]:
    return list(exe) + ["--config", str(config), "--state", str(state), "run"]


# ---------------------------------------------------------------- macOS


def launchd_plist(exe: list[str], config: Path, state: Path, home: Path) -> str:
    args = "\n".join(f"    <string>{escape(a)}</string>" for a in _args(exe, config, state))
    log = escape(str(service_log(home)))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>EnvironmentVariables</key><dict><key>{LOG_ENV}</key><string>{log}</string></dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def _launchd_paths(home: Path) -> tuple[Path, str]:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist", f"gui/{os.getuid()}"


def launchd_install(exe: list[str], config: Path, state: Path, home: Path) -> Plan:
    plist, domain = _launchd_paths(home)
    return Plan([
        Step(f"write {plist}", write=(plist, launchd_plist(exe, config, state, home))),
        Step("stop any running copy", cmd=["launchctl", "bootout", f"{domain}/{LABEL}"]),
        Step("start it now and at every login", cmd=["launchctl", "bootstrap", domain, str(plist)], check=True),
    ], [f"logs: {service_log(home)} (rotated at {LOG_MAX >> 20} MB, {LOG_KEEP} old copies kept)"])


def launchd_uninstall(home: Path) -> Plan:
    plist, domain = _launchd_paths(home)
    return Plan([
        Step("stop it", cmd=["launchctl", "bootout", f"{domain}/{LABEL}"]),
        Step(f"remove {plist}", remove=plist),
    ])


def launchd_status(home: Path) -> str:
    plist, domain = _launchd_paths(home)
    if not plist.exists():
        return "not installed"
    out = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("state ="):
            return f"installed, {line.split('=', 1)[1].strip()}"
    return "installed, not loaded"


# ---------------------------------------------------------------- Linux


def systemd_unit(exe: list[str], config: Path, state: Path) -> str:
    # systemd splits on whitespace; quote anything with spaces (and escape quotes/backslashes).
    def q(a: str) -> str:
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"' if (" " in a or '"' in a) else a
    cmd = " ".join(q(a) for a in _args(exe, config, state))
    return f"""[Unit]
Description=Flotilla: seed Pirate Face model shards
Documentation=https://pirateface.co
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={cmd}
Restart=on-failure
RestartSec=60
Nice=10
IOSchedulingClass=idle

[Install]
WantedBy=default.target
"""


def _unit_path(home: Path) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return base / "systemd" / "user" / UNIT


def systemd_install(exe: list[str], config: Path, state: Path, home: Path, linger: bool) -> Plan:
    unit = _unit_path(home)
    steps = [
        Step(f"write {unit}", write=(unit, systemd_unit(exe, config, state))),
        Step("reload systemd", cmd=["systemctl", "--user", "daemon-reload"], check=True),
        Step("start it now and at every login", cmd=["systemctl", "--user", "enable", "--now", UNIT], check=True),
    ]
    if linger:
        steps.append(Step("keep it running while you're logged out",
                          cmd=["loginctl", "enable-linger", os.environ.get("USER", "")]))
    return Plan(steps, ["logs: journalctl --user -u flotilla -f"])


def systemd_uninstall(home: Path) -> Plan:
    unit = _unit_path(home)
    return Plan([
        Step("stop it", cmd=["systemctl", "--user", "disable", "--now", UNIT]),
        Step(f"remove {unit}", remove=unit),
        Step("reload systemd", cmd=["systemctl", "--user", "daemon-reload"]),
    ])


def systemd_status() -> str:
    r = subprocess.run(["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True)
    state = r.stdout.strip() or "unknown"
    enabled = subprocess.run(["systemctl", "--user", "is-enabled", UNIT], capture_output=True, text=True).stdout.strip()
    if enabled in ("", "not-found"):
        return "not installed"
    return f"installed ({enabled}), {state}"


# ---------------------------------------------------------------- run plans


def install_plan(config: Path, state: Path, linger: bool = False, home: Path | None = None,
                 exe: list[str] | None = None) -> Plan:
    home = home or Path.home()
    exe = exe or executable()
    if sys.platform == "darwin":
        return launchd_install(exe, config, state, home)
    if sys.platform.startswith("linux"):
        return systemd_install(exe, config, state, home, linger)
    raise RuntimeError("background service is supported on macOS and Linux only")


def uninstall_plan(home: Path | None = None) -> Plan:
    home = home or Path.home()
    if sys.platform == "darwin":
        return launchd_uninstall(home)
    if sys.platform.startswith("linux"):
        return systemd_uninstall(home)
    raise RuntimeError("background service is supported on macOS and Linux only")


def status(home: Path | None = None) -> str:
    home = home or Path.home()
    if sys.platform == "darwin":
        return launchd_status(home)
    if sys.platform.startswith("linux"):
        return systemd_status()
    return "unsupported on this OS"


def execute(plan: Plan, dry_run: bool, say=print) -> bool:
    ok = True
    for s in plan.steps:
        say(f"{'would ' if dry_run else ''}{s.describe}")
        if dry_run:
            if s.write:
                say("    " + s.write[1].rstrip().replace("\n", "\n    "))
            if s.cmd:
                say(f"    $ {' '.join(s.cmd)}")
            continue
        if s.write:
            path, text = s.write
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if s.remove:
            s.remove.unlink(missing_ok=True)
        if s.cmd:
            r = subprocess.run(s.cmd, capture_output=True, text=True)
            if r.returncode != 0 and s.check:
                say(f"    failed: {(r.stderr or r.stdout).strip()}")
                ok = False
                break
    for n in plan.notes:
        say(n)
    return ok
