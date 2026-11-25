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
