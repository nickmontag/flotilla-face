"""`flotilla init --yes`: setup without questions, for scripts and agents."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from flotilla import cli, netinfo
from flotilla.config import load_config


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(netinfo, "internet_route", lambda: None)
    monkeypatch.setattr(cli, "_split_vpns", lambda route: [])
    monkeypatch.setattr(cli, "_vpn_containers", lambda: [])
    monkeypatch.setattr(cli, "_in_setup_container", lambda: False)
    monkeypatch.setattr(cli, "_exclude_from_time_machine", lambda folder: None)
    monkeypatch.setattr(netinfo, "virtualization", lambda: "")


def init(tmp_path, *flags):
    return cli.main(["--config", str(tmp_path / "config.toml"), "--state", str(tmp_path / "state"),
                     "init", "--yes", "--folder", str(tmp_path / "seed"), *flags])


def test_without_a_vpn_it_saves_but_wont_start_unless_told_to(tmp_path, no_network):
    assert init(tmp_path, "--budget-gb", "25") == cli.NOT_STARTED
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.budget_gb == 25 and cfg.bind_interface == "" and cfg.folder == tmp_path / "seed"
    assert init(tmp_path, "--seed-from-home-ip", "--focus", "balanced", "--dashboard", "lan") == 0
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.focus == "balanced" and cfg.dashboard_access == "lan" and cfg.budget_gb == 25   # kept


def test_it_pins_to_the_vpn_it_finds(tmp_path, no_network, monkeypatch):
    route = SimpleNamespace(looks_like_vpn=True, bind_value="10.2.0.2", iface="utun4", address="10.2.0.2")
    monkeypatch.setattr(netinfo, "internet_route", lambda: route)
    assert init(tmp_path, "--router-port") == 0
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.bind_interface == "10.2.0.2" and cfg.port_forwarding is False   # never UPnP when pinned
    assert init(tmp_path, "--vpn", "wg0") == 0 and load_config(tmp_path / "config.toml").bind_interface == "wg0"


def test_bad_answers_are_refused_and_nothing_is_written(tmp_path, no_network):
    assert init(tmp_path, "--focus", "everything") == 2
    assert init(tmp_path, "--upload-share", "2") == 2
    assert init(tmp_path, "--dashboard", "world") == 2
    assert not (tmp_path / "config.toml").exists()


def test_a_vm_without_a_vpn_gets_the_host_vpn_warning(tmp_path, no_network, monkeypatch, capsys):
    monkeypatch.setattr(netinfo, "virtualization", lambda: "a KVM/QEMU virtual machine (Proxmox, UTM, libvirt)")
    assert init(tmp_path) == cli.NOT_STARTED
    err = capsys.readouterr().err
    assert "KVM/QEMU" in err and "doesn't protect it" in err and "seedbox" in err
    # with a VPN pinned inside the VM there's nothing to warn about
    route = SimpleNamespace(looks_like_vpn=True, bind_value="wg0", iface="wg0", address="10.2.0.2")
    monkeypatch.setattr(netinfo, "internet_route", lambda: route)
    assert init(tmp_path) == 0 and "doesn't protect it" not in capsys.readouterr().err


def test_virtualization_is_read_from_the_usual_places():
    v = netinfo.virtualization
    assert v(run=lambda c: "1\n", platform="darwin").startswith("a macOS virtual machine")
    assert v(run=lambda c: "0\n", platform="darwin") == ""
    files = {"/proc/version": "Linux version 5.15 (microsoft-standard-WSL2)"}
    assert v(run=lambda c: "", read=lambda p: files.get(p, ""), platform="linux") == "WSL"
    assert "LXC" in v(run=lambda c: "lxc\n", read=lambda p: "", platform="linux")
    assert v(run=lambda c: "docker\n", read=lambda p: "", platform="linux") == ""     # setup handles Docker
    assert v(run=lambda c: "none\n", read=lambda p: "", platform="linux") == ""
    dmi = {"/sys/class/dmi/id/sys_vendor": "QEMU", "/sys/class/dmi/id/product_name": "Standard PC (Q35 + ICH9, 2009)"}
    assert "KVM" in v(run=lambda c: "", read=lambda p: dmi.get(p, ""), platform="linux")   # no systemd-detect-virt
    assert v(run=lambda c: "", read=lambda p: "", platform="linux") == ""
