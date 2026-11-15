"""Background service files and small CLI helpers."""

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
