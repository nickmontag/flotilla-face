import argparse
import json
import subprocess

import pytest

from flotilla import cli, containers, netinfo
from flotilla.config import load_config


@pytest.mark.parametrize("iface", ["wg0", "wg0-mullvad", "wg-windscribe", "us-nyc-wg-001", "nordlynx", "nordtun",
                                   "proton0", "pvpnksintrf0", "tun0", "utun4", "home-vpn", "expressvpn", "ppp0"])
def test_vpn_interface_names(iface):
    assert netinfo.Route(iface, "10.2.0.2").looks_like_vpn


@pytest.mark.parametrize("iface", ["eth0", "ens18", "en0", "wlan0", "docker0", "br-5cb4c057fd84", "veth09a8fe5",
                                   "tailscale0", "ztabcdef12", "lo"])
def test_not_vpn_interface_names(iface):
    assert not netinfo.Route(iface, "10.0.0.5").looks_like_vpn


def test_tailscale_utun_on_macos_is_not_a_vpn(monkeypatch):
    monkeypatch.setattr(netinfo.sys, "platform", "darwin")
    assert not netinfo.Route("utun0", "100.104.141.14").looks_like_vpn
    assert netinfo.Route("utun9", "10.5.0.2").looks_like_vpn


def test_other_vpns_only_those_that_reach_the_internet(monkeypatch):
    monkeypatch.setattr(netinfo, "_interfaces", lambda: [
        netinfo.Route("ens18", "10.0.0.206"), netinfo.Route("wg0", "10.64.0.2"),
        netinfo.Route("wg-work", "10.9.0.3"), netinfo.Route("tailscale0", "100.127.83.108")])
    monkeypatch.setattr(netinfo, "carries_internet", lambda r: r.iface != "wg-work")
    found = netinfo.other_vpns(netinfo.Route("ens18", "10.0.0.206"))
    assert [r.iface for r in found] == ["wg0"]


@pytest.mark.parametrize("image,name,shared,want", [
    ("qmcgaw/gluetun", "gluetun", False, True),
    ("ghcr.io/qdm12/gluetun:v3.40", "vpn", False, True),
    ("binhex/arch-qbittorrentvpn:latest", "qbit", False, True),
    ("haugene/transmission-openvpn", "transmission", False, True),
    ("lscr.io/linuxserver/wireguard", "wireguard", True, True),
    ("lscr.io/linuxserver/wireguard", "wireguard", False, False),   # as likely a server
    ("ghcr.io/wg-easy/wg-easy", "wg-easy", True, False),
    ("kylemanna/openvpn", "openvpn", True, False),
    ("tailscale/tailscale", "ts", True, False),
    ("lscr.io/linuxserver/transmission:latest", "transmission", False, False),
    ("alpine", "sidecar", True, False),
])
def test_is_vpn_client(image, name, shared, want):
    assert containers.is_vpn_client(image, name, shared) is want


def _inspect(cid, name, image, mode, health=None):
    d = {"Id": cid, "Name": "/" + name, "Config": {"Image": image}, "HostConfig": {"NetworkMode": mode},
         "State": {"Status": "running"}}
    if health:
        d["State"]["Health"] = {"Status": health}
    return d


def _fake_docker(monkeypatch, docs, ps_rc=0, ps_err=""):
    monkeypatch.setattr(containers.shutil, "which", lambda n: "/usr/bin/docker")

    def docker(*args):
        if args[:1] == ("ps",) and "-a" in args:
            return subprocess.CompletedProcess(args, 0, "\n".join(d["Name"][1:] for d in docs), "")
        if args[:1] == ("ps",):
            return subprocess.CompletedProcess(args, ps_rc, "\n".join(d["Id"] for d in docs), ps_err)
        return subprocess.CompletedProcess(args, 0, json.dumps([d for d in docs if d["Id"] in args]), "")

    monkeypatch.setattr(containers, "_docker", docker)


MEDIA_SERVER = [   # the shape of a real media server: two gluetuns, clients sharing their networks
    _inspect("aaa1", "transmission", "lscr.io/linuxserver/transmission:latest", "container:ddd4", "healthy"),
    _inspect("ddd4", "gluetun", "qmcgaw/gluetun", "media-server_default", "healthy"),
    _inspect("eee5", "jellyfin", "jellyfin/jellyfin", "media-server_default"),
    _inspect("fff6", "gluetun_seed", "qmcgaw/gluetun", "pirateface-seed_default", "healthy"),
    _inspect("bbb2", "transmission_seed", "lscr.io/linuxserver/transmission:latest", "container:fff6"),
    _inspect("ccc3", "sidecar", "alpine", "container:gluetun_seed"),   # by name, as `docker run` writes it
]


def test_vpn_containers_finds_gluetun_and_who_uses_it(monkeypatch):
    _fake_docker(monkeypatch, MEDIA_SERVER)
    found = containers.vpn_containers()
    assert [(c.name, c.shared_by) for c in found] == [
        ("gluetun_seed", ["sidecar", "transmission_seed"]), ("gluetun", ["transmission"])]


def test_vpn_containers_without_docker(monkeypatch):
    monkeypatch.setattr(containers.shutil, "which", lambda n: None)
    assert containers.vpn_containers() == []


def test_vpn_containers_docker_refuses(monkeypatch):
    _fake_docker(monkeypatch, [], ps_rc=1, ps_err="permission denied while trying to connect to the Docker daemon")
    with pytest.raises(containers.DockerUnavailable, match="permission denied"):
        containers.vpn_containers()


def test_container_commands_use_the_vpn_network():
    vpn = containers.VpnContainer("gluetun_seed", "qmcgaw/gluetun")
    setup = containers.setup_command(vpn, "/srv/flotilla")
    node = containers.node_command(vpn, "/srv/flotilla")
    dash = containers.dashboard_command(7421, "/srv/flotilla")
    for cmd in (setup, node):
        assert cmd[cmd.index("--network") + 1] == "container:gluetun_seed"
        assert cmd[cmd.index("-v") + 1] == "/srv/flotilla:/root"
    assert "--network" not in dash and "7421:7421" in dash
    assert "flotilla init" in setup[-1] and "exec flotilla run" in node[-1]


def _answers(monkeypatch, replies):
    seen = []

    def ask(prompt=""):
        seen.append(prompt)
        for key, value in replies:
            if key in prompt:
                return value
        return ""

    monkeypatch.setattr("builtins.input", ask)
    return seen


def test_setup_moves_into_the_vpn_container(tmp_path, monkeypatch):
    found = [containers.VpnContainer("gluetun_seed", "qmcgaw/gluetun", "healthy", ["transmission_seed"]),
             containers.VpnContainer("gluetun", "qmcgaw/gluetun", "healthy", ["transmission"])]
    monkeypatch.setattr(netinfo, "internet_route", lambda: netinfo.Route("ens18", "10.0.0.206"))
    monkeypatch.setattr(cli, "_vpn_containers", lambda: found)
    monkeypatch.setattr(containers, "existing", lambda: [])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    ran = []
    monkeypatch.setattr(containers, "run", lambda cmd, interactive=False: ran.append(cmd) or 0)
    _answers(monkeypatch, [("Which one", "2")])
    path = tmp_path / "c.toml"
    assert cli.cmd_init(argparse.Namespace(config=str(path))) == 0
    assert not path.exists()                      # the setup inside the container wrote its own config
    assert [c[c.index("--network") + 1] for c in ran if "--network" in c] == ["container:gluetun"] * 2
    assert any("flotilla-dashboard" in c for c in ran) and len(ran) == 3


def test_setup_prints_commands_when_names_are_taken(tmp_path, monkeypatch, capsys):
    found = [containers.VpnContainer("gluetun", "qmcgaw/gluetun")]
    monkeypatch.setattr(netinfo, "internet_route", lambda: netinfo.Route("ens18", "10.0.0.206"))
    monkeypatch.setattr(cli, "_vpn_containers", lambda: found)
    monkeypatch.setattr(containers, "existing", lambda: ["flotilla"])
    monkeypatch.setattr(containers, "run", lambda *a, **k: pytest.fail("ran docker"))
    _answers(monkeypatch, [])
    assert cli.cmd_init(argparse.Namespace(config=str(tmp_path / "c.toml"))) == 1
    out = capsys.readouterr().out
    assert "docker rm -f flotilla" in out and "--network container:gluetun" in out


def _no_licenses(monkeypatch):
    from flotilla import sources

    class NoPF:
        def __init__(self, *a, **kw):
            pass

        def licenses(self):
            raise OSError("offline")

    monkeypatch.setattr(sources, "PirateFace", NoPF)


def test_setup_declining_the_container_warns(tmp_path, monkeypatch, capsys):
    _no_licenses(monkeypatch)
    monkeypatch.setattr(netinfo, "internet_route", lambda: netinfo.Route("ens18", "10.0.0.206"))
    monkeypatch.setattr(cli, "_vpn_containers", lambda: [containers.VpnContainer("gluetun", "qmcgaw/gluetun")])
    _answers(monkeypatch, [("inside the VPN container", "n"), ("Folder", str(tmp_path / "seeds"))])
    path = tmp_path / "c.toml"
    assert cli.cmd_init(argparse.Namespace(config=str(path))) == 0
    assert "gluetun is a VPN container, but Flotilla set up here doesn't use it" in capsys.readouterr().out
    assert load_config(path).bind_interface == ""


def test_setup_binds_to_a_split_tunnel_vpn_by_default(tmp_path, monkeypatch):
    _no_licenses(monkeypatch)
    monkeypatch.setattr(netinfo, "internet_route", lambda: netinfo.Route("eth0", "192.168.1.5"))
    monkeypatch.setattr(cli, "_split_vpns", lambda route: [netinfo.Route("wg0-mullvad", "10.64.0.2")])
    seen = _answers(monkeypatch, [("Folder", str(tmp_path / "seeds"))])
    path = tmp_path / "c.toml"
    assert cli.cmd_init(argparse.Namespace(config=str(path))) == 0
    bind_q = [p for p in seen if "Bind torrent traffic to wg0-mullvad" in p]
    assert bind_q and "(Y/n)" in bind_q[0] and "offline if the VPN drops" in bind_q[0]
    cfg = load_config(path)
    assert cfg.bind_interface in ("wg0-mullvad", "10.64.0.2") and cfg.port_forwarding is False


def test_setup_binds_to_the_default_route_vpn_by_default(tmp_path, monkeypatch):
    _no_licenses(monkeypatch)
    monkeypatch.setattr(netinfo, "internet_route", lambda: netinfo.Route("tun0", "10.8.0.2"))
    seen = _answers(monkeypatch, [("Folder", str(tmp_path / "seeds"))])
    path = tmp_path / "c.toml"
    assert cli.cmd_init(argparse.Namespace(config=str(path))) == 0
    assert any("Bind torrent traffic to the VPN" in p and "(Y/n)" in p for p in seen)
    assert load_config(path).bind_interface in ("tun0", "10.8.0.2")


def test_setup_tells_the_installer_where_flotilla_went(tmp_path, monkeypatch):
    """install.sh offers a background service only when setup put Flotilla on this machine."""
    _no_licenses(monkeypatch)
    result = tmp_path / "result"
    monkeypatch.setenv("FLOTILLA_SETUP_RESULT", str(result))
    monkeypatch.setattr(netinfo, "internet_route", lambda: netinfo.Route("ens18", "10.0.0.206"))
    monkeypatch.setattr(cli, "_vpn_containers", lambda: [containers.VpnContainer("gluetun", "qmcgaw/gluetun")])
    monkeypatch.setattr(containers, "existing", lambda: [])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(containers, "run", lambda cmd, interactive=False: 0)

    _answers(monkeypatch, [])                                        # yes: into the container
    assert cli.cmd_init(argparse.Namespace(config=str(tmp_path / "a.toml"))) == 0
    assert result.read_text() == "docker"

    _answers(monkeypatch, [("inside the VPN container", "n"), ("Folder", str(tmp_path / "seeds"))])
    assert cli.cmd_init(argparse.Namespace(config=str(tmp_path / "b.toml"))) == 0
    assert result.read_text() == "here"
