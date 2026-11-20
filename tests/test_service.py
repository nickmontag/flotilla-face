"""Background service files and small CLI helpers."""

import json
import plistlib
import shlex
from pathlib import Path

import pytest

from flotilla import service
from flotilla.cli import parse_gb


def test_launchd_plist_is_valid_and_handles_spaces(tmp_path):
    exe = ["/Users/x/My Apps/bin/flotilla"]
    text = service.launchd_plist(exe, Path("/Users/x/c & d.toml"), Path("/Users/x/st ate"), Path("/Users/x"))
    doc = plistlib.loads(text.encode())
    assert doc["Label"] == service.LABEL
    assert doc["ProgramArguments"] == exe + ["--config", "/Users/x/c & d.toml", "--state", "/Users/x/st ate", "run"]
    assert doc["RunAtLoad"] and doc["KeepAlive"] == {"SuccessfulExit": False}


def test_systemd_unit_quotes_paths():
    text = service.systemd_unit(["/home/x/my apps/flotilla"], Path("/home/x/.config/flotilla/config.toml"),
                                Path("/home/x/state"))
    line = next(l for l in text.splitlines() if l.startswith("ExecStart="))
    assert shlex.split(line.removeprefix("ExecStart=")) == [
        "/home/x/my apps/flotilla", "--config", "/home/x/.config/flotilla/config.toml", "--state", "/home/x/state", "run"]
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    plan = service.launchd_install(["/bin/flotilla"], tmp_path / "c.toml", tmp_path / "s", tmp_path)
    out = []
    assert service.execute(plan, dry_run=True, say=out.append)
    assert not (tmp_path / "Library").exists()
    assert any("launchctl bootstrap" in line for line in out)


@pytest.mark.parametrize("raw,gb", [("50", 50), ("50G", 50), ("50 GB", 50), ("1.5TB", 1500), ("2t", 2000)])
def test_parse_gb(raw, gb):
    assert parse_gb(raw) == gb


@pytest.mark.parametrize("raw", ["", "-5", "lots", "0"])
def test_parse_gb_rejects(raw):
    with pytest.raises(ValueError):
        parse_gb(raw)


def test_proc_route_longest_prefix(tmp_path, monkeypatch):
    """gluetun-style split routes: 0.0.0.0/1 + 128.0.0.0/1 via tun0 beat the eth0 default."""
    from flotilla import netinfo

    table = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "tun0\t00000000\t0100640A\t0003\t0\t0\t0\t00000080\t0\t0\t0\n"
        "tun0\t00000080\t0100640A\t0003\t0\t0\t0\t00000080\t0\t0\t0\n"
    )
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: __import__("io").StringIO(table)
                        if p == "/proc/net/route" else real_open(p, *a, **k))
    monkeypatch.setattr(netinfo, "_iface_ipv4", lambda i: "10.100.0.2" if i == "tun0" else None)
    r = netinfo._proc_route("1.1.1.1")
    assert r.iface == "tun0" and r.address == "10.100.0.2" and r.looks_like_vpn


def _run_logging(log, env_file, code):
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != service.LOG_ENV}
    env["HOME"] = str(log.parent)                     # so the macOS fallback never finds a real log
    if env_file:
        env[service.LOG_ENV] = str(env_file)
    with open(log, "a") as out:
        subprocess.run([sys.executable, "-c", code], stdout=out, stderr=out, env=env, check=True)


ROTATE = ("import sys\nfrom flotilla.service import rotate_own_log as r\nprint('before', flush=True)\n"
          "print(r(max_bytes=1000, keep=2, every=0), flush=True)\n"
          "print('after', flush=True)\nprint('err', file=sys.stderr, flush=True)\n"
          "print(r(max_bytes=1000, keep=2, every=0), flush=True)\n")


def test_the_service_log_is_rotated_keeping_a_few_old_copies(tmp_path):
    """launchd's StandardOutPath is never rotated, so the node rotates its own log file."""
    log = tmp_path / "flotilla.log"
    (tmp_path / "flotilla.log.1").write_text("older\n")
    (tmp_path / "flotilla.log.2").write_text("oldest\n")
    log.write_text("x" * 3000 + "\n")
    _run_logging(log, log, ROTATE)
    assert log.read_text() == "True\nafter\nerr\nFalse\n"
    assert (tmp_path / "flotilla.log.1").read_text().endswith("before\n")
    assert (tmp_path / "flotilla.log.2").read_text() == "older\n"     # moved up; "oldest" is gone
    assert not (tmp_path / "flotilla.log.3").exists()


def test_output_you_redirect_yourself_is_never_rotated(tmp_path):
    mine = tmp_path / "my-run.log"
    mine.write_text("x" * 3000 + "\n")
    _run_logging(mine, None, ROTATE)                            # no service log named
    _run_logging(mine, tmp_path / "flotilla.log", ROTATE)       # the service log is another file
    assert mine.read_text().count("False") == 4 and not list(tmp_path.glob("*.1"))


def test_a_terminal_or_pipe_is_never_rotated(tmp_path, monkeypatch):
    import subprocess
    import sys

    monkeypatch.setenv(service.LOG_ENV, str(tmp_path / "flotilla.log"))
    code = "from flotilla.service import rotate_own_log\nprint(rotate_own_log(max_bytes=0, every=0))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "False"


def test_launchd_agent_names_its_log_for_rotation():
    doc = plistlib.loads(service.launchd_plist(["/bin/flotilla"], Path("/c.toml"), Path("/st"), Path("/Users/x")).encode())
    assert doc["EnvironmentVariables"] == {service.LOG_ENV: "/Users/x/Library/Logs/flotilla.log"}
    assert doc["StandardOutPath"] == doc["StandardErrorPath"] == "/Users/x/Library/Logs/flotilla.log"


def test_setup_suggests_an_upload_cap_from_the_plans_data_cap(tmp_path, monkeypatch):
    import argparse

    from flotilla import cli, netinfo, sources
    from flotilla.config import load_config

    monkeypatch.setattr(netinfo, "internet_route", lambda: None)

    class NoPF:
        def __init__(self, *a, **kw):
            pass

        def licenses(self):
            raise OSError("offline")

    monkeypatch.setattr(sources, "PirateFace", NoPF)

    def setup(plan_cap):
        answers = [str(tmp_path / "seeds")]

        def ask(prompt=""):
            if "data cap" in prompt:
                return plan_cap
            return answers.pop(0) if answers else ""

        monkeypatch.setattr("builtins.input", ask)
        path = tmp_path / f"cap-{plan_cap or 'none'}.toml"
        assert cli.cmd_init(argparse.Namespace(config=str(path))) == 0
        return load_config(path).monthly_upload_cap_gb

    assert setup("1200") == 480          # 40% of a 1.2 TB plan, taken by pressing Enter
    assert setup("") == 0                # no plan cap: no upload cap


def _uninstall(tmp_path, monkeypatch, **kw):
    """A node set up in tmp_path: config, both account files, a pick, a held model and a macOS log."""
    import argparse
    import sys

    from flotilla import cli
    from flotilla.config import Config, render_config

    conf = tmp_path / "conf"
    conf.mkdir(parents=True)
    cfg = Config(folder=tmp_path / "seeds")
    cfg.local_feed = conf / "my-priorities.json"
    (conf / "config.toml").write_text(render_config(cfg))
    for name in ("hf-token", "pirateface-key", "my-priorities.json"):
        (conf / name).write_text("x")
    model = tmp_path / "seeds" / "a--b"
    model.mkdir(parents=True)
    (model / "w.safetensors").write_bytes(b"0" * 100)
    state = tmp_path / "state"
    state.mkdir()
    (state / "state.json").write_text(json.dumps({"holdings": {"a/b": {"save_path": str(model)}}}))
    logs = tmp_path / "Logs"
    logs.mkdir()
    for name in ("flotilla.log", "flotilla.log.1"):
        (logs / name).write_text("log")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "uninstall_plan", lambda: service.Plan())
    monkeypatch.setattr(service, "service_log", lambda home=None: logs / "flotilla.log")
    args = argparse.Namespace(config=str(conf / "config.toml"), state=str(state), delete_data=True, yes=True)
    for k, v in kw.items():
        setattr(args, k, v)
    return cli.cmd_uninstall(args), conf, logs


def test_uninstall_delete_data_removes_what_setup_made(tmp_path, monkeypatch, capsys):
    rc, conf, logs = _uninstall(tmp_path, monkeypatch)
    assert rc == 0
    assert sorted(p.name for p in conf.iterdir()) == ["my-priorities.json"]     # your picks stay
    assert not list(logs.iterdir())
    assert not (tmp_path / "seeds").exists() and not (tmp_path / "state").exists()
    out = capsys.readouterr().out
    assert "your picks from `flotilla add`" in out and "not created" not in out
    assert "deleted here, not revoked" in out


def test_uninstall_offers_to_remove_the_docker_containers_and_volume(tmp_path, monkeypatch, capsys):
    import subprocess

    from flotilla import containers

    ran = []

    def docker(*a):
        ran.append(list(a))
        return subprocess.CompletedProcess(a, 0, "", "")

    ours = {n: containers.Installed(n, True, True, "container:gluetun", "flotilla")
            for n in (containers.NODE, containers.DASHBOARD)}
    monkeypatch.setattr(containers, "installed", lambda: ours)
    monkeypatch.setattr(containers, "_docker", docker)

    monkeypatch.setattr("builtins.input", lambda prompt="": "n")         # keep them, keep the data
    assert _uninstall(tmp_path / "keep", monkeypatch, delete_data=False, yes=False)[0] == 0
    out = capsys.readouterr().out
    assert not ran
    assert "still seeding" in out and "docker rm -f flotilla flotilla-dashboard" in out
    assert "Kept the Docker volume flotilla" in out and "docker volume rm flotilla" in out

    assert _uninstall(tmp_path / "gone", monkeypatch)[0] == 0          # --delete-data --yes
    assert ran == [["rm", "-f", "flotilla", "flotilla-dashboard"], ["volume", "rm", "flotilla"]]
    assert "the Docker volume flotilla" in capsys.readouterr().out


def test_uninstall_leaves_a_docker_volume_setup_didnt_name(tmp_path, monkeypatch, capsys):
    import subprocess

    from flotilla import containers

    ran = []
    monkeypatch.setattr(containers, "installed",
                        lambda: {"flotilla": containers.Installed("flotilla", True, True, "container:g", "media")})
    monkeypatch.setattr(containers, "_docker", lambda *a: ran.append(list(a)) or subprocess.CompletedProcess(a, 0))
    assert _uninstall(tmp_path, monkeypatch)[0] == 0
    assert ran == [["rm", "-f", "flotilla"]]
    assert "Left the Docker volume media" in capsys.readouterr().out


def test_a_missing_systemctl_is_reported_not_raised(tmp_path, monkeypatch):
    def no_such(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(service.subprocess, "run", no_such)
    out = []
    plan = service.systemd_install(["/bin/flotilla"], tmp_path / "c.toml", tmp_path / "s", tmp_path, linger=False)
    assert service.execute(plan, dry_run=False, say=out.append) is False
    assert any("failed: this system has no systemctl" in line and "flotilla run" in line for line in out)
    assert not any("start it now" in line for line in out)          # stopped at the first failure
    assert service.execute(service.systemd_uninstall(tmp_path), dry_run=False, say=out.append) is True
    assert service.systemd_status().startswith("not installed: this system has no systemctl")


def test_a_failed_linger_is_reported(tmp_path, monkeypatch):
    import subprocess

    def run(cmd, **kw):
        if cmd[0] == "loginctl":
            return subprocess.CompletedProcess(cmd, 1, "", "Could not enable linger: Access denied")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(service.subprocess, "run", run)
    monkeypatch.setattr("getpass.getuser", lambda: "sam")
    out = []
    plan = service.systemd_install(["/bin/flotilla"], tmp_path / "c.toml", tmp_path / "s", tmp_path, linger=True)
    assert plan.steps[-1].cmd == ["loginctl", "enable-linger", "sam"]
    assert service.execute(plan, dry_run=False, say=out.append) is True     # it runs, just not logged out
    warning = next(line for line in out if "couldn't turn on lingering" in line)
    assert "Access denied" in warning and "sudo loginctl enable-linger sam" in warning


def test_readme_lists_setups_sections():
    """README's Get started names every section `flotilla init` asks, in order."""
    import re

    from flotilla import cli

    root = Path(cli.__file__).parent.parent
    asked = re.findall(r'ui\.section\((\d+), SETUP_STEPS, "([^"]+)"\)', (root / "flotilla" / "cli.py").read_text())
    assert len(asked) == cli.SETUP_STEPS
    readme = (root / "README.md").read_text()
    get_started = readme[readme.index("## Get started"):readme.index("## Choose models yourself")]
    listed = re.findall(r"^(\d+)\. \*\*([^*]+)\*\*", get_started, re.M)
    assert listed == asked
