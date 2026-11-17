"""Who can reach the dashboard (dashboard.access), and the guard against public internet visitors."""

import argparse
import socket

import pytest

from flotilla import access, cli, containers, netinfo
from flotilla.config import Config, load_config, render_config


@pytest.mark.parametrize("client", ["127.0.0.1", "10.0.0.5", "192.168.1.20", "172.17.0.1", "100.104.141.14",
                                    "::1", "fd00::5", "fd7a:115c:a1e0::1", "fe80::1%eth0", "::ffff:192.168.1.2"])
def test_guard_lets_in_this_machine_home_networks_and_tailscale(client):
    for mode in ("local", "lan", "tailnet"):
        assert access.allows(client, mode)


@pytest.mark.parametrize("client", ["8.8.8.8", "1.1.1.1", "203.0.114.7", "2001:4860:4860::8888", "::ffff:8.8.8.8",
                                    "not-an-address"])
def test_guard_turns_away_the_internet_unless_public(client):
    for mode in ("local", "lan", "tailnet"):
        assert not access.allows(client, mode)
    assert access.allows(client, "public")


def test_where_each_choice_listens():
    assert access.listen_for("local") == ("127.0.0.1", False)
    assert access.listen_for("tailnet") == ("127.0.0.1", True)      # tailscale serve passes requests in
    assert access.listen_for("lan") == ("0.0.0.0", True)
    assert access.listen_for("public") == ("0.0.0.0", True)


def test_addresses_to_open():
    assert access.address("local", 7421, None) == "http://127.0.0.1:7421"
    assert access.address("lan", 7421, "k", lan="10.0.0.206") == "http://10.0.0.206:7421/?key=k"
    assert access.address("lan", 7421, "k") == "http://<this computer's address>:7421/?key=k"
    assert access.address("tailnet", 7421, "k", tailnet="box.tail1.ts.net") == "https://box.tail1.ts.net:7421/?key=k"
    assert "public address" in access.address("public", 7421, "k")
    assert access.key_in("http://<this computer's address>:7421/?key=Ab_c-9") == "Ab_c-9"


def test_dashboard_drops_internet_visitors_without_a_reply(tmp_path):
    pytest.importorskip("libtorrent")
    from flotilla.dashboard import Dashboard

    for mode, outsider_ok in (("lan", False), ("public", True)):
        dash = Dashboard(engine=None, port=0, host="127.0.0.1", key="k" * 43, access=mode)
        try:
            assert dash.httpd.verify_request(None, ("10.0.0.7", 50000))
            assert dash.httpd.verify_request(None, ("8.8.8.8", 50000)) is outsider_ok
        finally:
            dash.httpd.server_close()


def test_config_keeps_and_checks_dashboard_access(tmp_path):
    path = tmp_path / "c.toml"
    cfg = Config()
    cfg.dashboard_access = "tailnet"
    path.write_text(render_config(cfg))
    assert load_config(path).dashboard_access == "tailnet"
    path.write_text(path.read_text().replace('access = "tailnet"', 'access = "everyone"'))
    with pytest.raises(ValueError, match="dashboard.access"):
        load_config(path)
    old = tmp_path / "old.toml"                                    # written before this setting existed
    old.write_text(render_config(Config()).replace('access = "local"', ""))
    assert load_config(old).dashboard_access == "local"


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


def test_question_offers_only_what_this_machine_has(monkeypatch, capsys):
    seen = _answers(monkeypatch, [])
    assert cli._ask_access("local", 7421) == "local"                 # a VPS: no home network, no Tailscale
    out = capsys.readouterr().out
    assert "home network" not in out and "Tailscale" not in out and "Anyone on the internet" in out
    assert f"ssh -L 7421:localhost:7421 {socket.gethostname()}" in out
    assert "1-2" in seen[-1]

    monkeypatch.setattr(netinfo, "lan_address", lambda: "10.0.0.206")
    monkeypatch.setattr(access, "tailscale_name", lambda: "box.tail1.ts.net")
    _answers(monkeypatch, [("Who can reach", "3")])
    assert cli._ask_access("local", 7421) == "tailnet"
    assert "tailscale serve --bg --https=7421 http://127.0.0.1:7421" in capsys.readouterr().out


def test_public_has_to_be_typed_out(monkeypatch, capsys):
    monkeypatch.setattr(netinfo, "lan_address", lambda: "10.0.0.206")
    _answers(monkeypatch, [("Who can reach", "public")])             # chose it, didn't confirm
    assert cli._ask_access("lan", 7421) == "lan"
    assert "kept to your home network" in capsys.readouterr().out
    _answers(monkeypatch, [("Who can reach", "3"), ("type public", "yes")])
    assert cli._ask_access("lan", 7421) == "lan"
    _answers(monkeypatch, [("Who can reach", "3"), ("type public", "public")])
    assert cli._ask_access("lan", 7421) == "public"


def test_public_is_never_the_default(monkeypatch):
    seen = _answers(monkeypatch, [])
    assert cli._ask_access("public", 7421) == "local"                # even when chosen before
    assert seen[-1].endswith("(1): ")


def test_setup_saves_the_choice(tmp_path, monkeypatch):
    from flotilla import sources

    class NoPF:
        def __init__(self, *a, **kw):
            pass

        def licenses(self):
            raise OSError("offline")

    monkeypatch.setattr(sources, "PirateFace", NoPF)
    monkeypatch.setattr(netinfo, "internet_route", lambda: None)
    monkeypatch.setattr(netinfo, "lan_address", lambda: "192.168.1.20")
    _answers(monkeypatch, [("Folder", str(tmp_path / "seeds")), ("Who can reach", "2")])
    path = tmp_path / "c.toml"
    assert cli.cmd_init(argparse.Namespace(config=str(path))) == 0
    assert load_config(path).dashboard_access == "lan"


def test_docker_publishes_the_dashboard_only_where_chosen():
    assert containers.publish("local", 7421, "10.0.0.206") == "127.0.0.1:7421:7421"
    assert containers.publish("tailnet", 7421, "10.0.0.206") == "127.0.0.1:7421:7421"
    assert containers.publish("lan", 7421, "10.0.0.206") == "10.0.0.206:7421:7421"
    assert containers.publish("lan", 7421, None) == "127.0.0.1:7421:7421"      # no home network: stay local
    assert containers.publish("public", 7421, None) == "7421:7421"
    cmd = containers.dashboard_command(7421, "flotilla", "tailnet", "10.0.0.206")
    assert "--access tailnet" in cmd[-1] and "127.0.0.1:7421:7421" in cmd


def test_a_public_dashboard_says_so_on_the_page():
    pytest.importorskip("libtorrent")
    from flotilla.dashboard import Dashboard

    pages = {}
    for mode in ("lan", "tailnet", "public"):
        dash = Dashboard(engine=None, port=0, host="127.0.0.1", key="k" * 43, access=mode)
        pages[mode] = dash.page.decode()
        dash.httpd.server_close()
    assert "Reachable from your network" in pages["lan"]
    assert "Tailscale devices" in pages["tailnet"]
    assert "Public: anyone on the internet can reach this page" in pages["public"]


def test_serving_a_public_dashboard_warns(tmp_path, monkeypatch, capsys):
    pytest.importorskip("libtorrent")
    import threading

    from flotilla.config import render_config

    path = tmp_path / "c.toml"
    path.write_text(render_config(Config()))
    started = threading.Event()
    monkeypatch.setattr(threading.Event, "wait", lambda self, *a: started.set() or True)
    args = argparse.Namespace(config=str(path), state=str(tmp_path / "state"), port=0, url=False, serve=True,
                              listen="127.0.0.1", access="public", no_open=True)
    assert cli.cmd_dashboard(args) == 0
    assert "WARNING: the dashboard is PUBLIC" in capsys.readouterr().err
