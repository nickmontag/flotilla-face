import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import libtorrent as lt
import pytest

from flotilla import containers, vpnport
from flotilla.config import Config
from flotilla.engine import Engine, listen_interfaces


# ---------------------------------------------------------------- a fake NAT-PMP gateway


class Gateway:
    """A NAT-PMP server on 127.0.0.1. `give(internal, wish)` picks the public port."""

    def __init__(self, give):
        self.give, self.asked = give, []
        self.sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sk.bind(("127.0.0.1", 0))
        self.port = self.sk.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                data, peer = self.sk.recvfrom(64)
            except OSError:
                return
            _, op, _, internal, wish, life = struct.unpack("!BBHHHI", data[:12])
            self.asked.append((op, internal, wish, life))
            mapped = 0 if life == 0 else self.give(internal, wish)
            self.sk.sendto(struct.pack("!BBHIHHI", 0, 128 + op, 0, 1, internal, mapped, life), peer)

    def close(self):
        self.sk.close()


@pytest.fixture
def gateway(monkeypatch):
    made = []

    def make(give):
        g = Gateway(give)
        made.append(g)
        monkeypatch.setattr(vpnport, "NATPMP_PORT", g.port)
        return g

    yield make
    for g in made:
        g.close()


def _finder(monkeypatch, **kw):
    monkeypatch.setattr(vpnport, "gateways", lambda bind, addr: ["127.0.0.1"])
    monkeypatch.setattr(vpnport, "piactl_port", lambda: (0, ""))
    monkeypatch.setattr(vpnport, "in_container", lambda: False)
    return vpnport.PortFinder("wg0", address=lambda: "127.0.0.1", gluetun_auth="", log=lambda m: None, **kw)


def test_protonvpn_style_gateway_gives_its_own_port_and_we_listen_on_it(gateway, monkeypatch):
    gateway(lambda internal, wish: 51234)      # ProtonVPN: one port per connection, whatever you ask
    fw = _finder(monkeypatch).find(6881)
    assert (fw.port, fw.listen, fw.source) == (51234, 51234, "NAT-PMP 127.0.0.1")
    assert fw.renew == vpnport.RENEW


def test_gateway_that_honours_the_wish(gateway, monkeypatch):
    g = gateway(lambda internal, wish: wish or internal)
    fw = _finder(monkeypatch).find(6881)
    assert (fw.port, fw.listen) == (6881, 6881)
    assert {op for op, *_ in g.asked} == {vpnport.TCP, vpnport.UDP}      # both: TCP and uTP peers


def test_gateway_that_translates_keeps_our_port_and_announces_its(gateway, monkeypatch):
    table = {}

    def give(internal, wish):     # a real NAT: a fresh public port per internal port
        return table.setdefault(internal, 40000 + len(table))

    g = gateway(give)
    f = _finder(monkeypatch)
    fw = f.find(6881)
    assert (fw.port, fw.listen) == (40000, 6881)
    asked = len(g.asked)
    assert (f.find(6881).port, f.find(6881).listen) == (40000, 6881)
    assert len(g.asked) - asked == 4          # renewals don't test it again: TCP + UDP each time


def test_no_gateway_answers(monkeypatch):
    monkeypatch.setattr(vpnport, "NATPMP_PORT", 9)      # discard: nobody answers
    f = _finder(monkeypatch)
    f.bind = "nordlynx"
    fw = f.find(6881)
    assert fw.port == 0 and "NordVPN doesn't forward ports" in fw.note and "ProtonVPN" in fw.note
    assert fw.renew == vpnport.RETRY


def test_vpn_down_sends_nothing(monkeypatch):
    f = _finder(monkeypatch)
    f.address = lambda: None
    monkeypatch.setattr(vpnport, "natpmp_map", lambda *a, **k: pytest.fail("sent while the VPN is down"))
    assert f.find(6881).port == 0


def test_port_file(tmp_path, monkeypatch):
    monkeypatch.setattr(vpnport, "NATPMP_PORT", 9)
    (tmp_path / "port").write_text("43210\n")
    assert _finder(monkeypatch, port_file=str(tmp_path / "port")).find(6881).port == 43210
    (tmp_path / "port").write_text("soon")
    assert "doesn't hold a port" in _finder(monkeypatch, port_file=str(tmp_path / "port")).find(6881).note


def test_gateways_are_private_and_never_ourselves(monkeypatch):
    monkeypatch.setattr(vpnport, "_route_gateways", lambda iface: ["10.2.0.1", "8.8.8.8"])
    monkeypatch.setattr("flotilla.netinfo._interfaces", lambda: [])
    assert vpnport.gateways("wg0", "10.2.0.2") == ["10.2.0.1"]
    assert vpnport.gateways("tun0", "10.96.0.7") == ["10.2.0.1", "10.96.0.1"]
    assert vpnport.gateways("tun0", "203.0.113.5") == ["10.2.0.1"]


# ---------------------------------------------------------------- gluetun's control server


def _control_server(routes: dict, need_key: str | None = None):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if need_key is not None and self.headers.get("X-API-Key") != need_key:
                self.send_response(401)
                self.end_headers()
                return
            if self.path not in routes:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(routes[self.path]).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_gluetun_port_new_and_old_routes():
    srv, base = _control_server({"/v1/portforward": {"port": 5914}})
    assert vpnport.gluetun_port(base=base) == 5914
    srv.shutdown()
    srv, base = _control_server({"/v1/openvpn/portforwarded": {"port": 0}})    # older gluetun, forwarding off
    assert vpnport.gluetun_port(base=base) == 0
    srv.shutdown()


def test_gluetun_key():
    srv, base = _control_server({"/v1/portforward": {"port": 5914}}, need_key="s3cret")
    with pytest.raises(vpnport.GluetunAuth):
        vpnport.gluetun_port("", base=base)
    assert vpnport.gluetun_port('{"auth":"apikey","apikey":"s3cret"}', base=base) == 5914
    assert vpnport.gluetun_port("s3cret", base=base) == 5914
    srv.shutdown()


def test_gluetun_only_on_this_machine():
    with pytest.raises(vpnport.GluetunAbsent):
        vpnport.gluetun_port("key", base="http://192.168.1.5:8000")


def test_something_else_on_port_8000_isnt_gluetun():
    srv, base = _control_server({"/v1/portforward": ["hello"]})
    with pytest.raises(vpnport.GluetunAbsent):
        vpnport.gluetun_port(base=base)
    srv.shutdown()


def test_finder_inside_gluetun(monkeypatch):
    srv, base = _control_server({"/v1/portforward": {"port": 5914}})
    monkeypatch.setattr(vpnport, "GLUETUN", base)
    monkeypatch.setattr(vpnport.gluetun_port, "__defaults__", ("", base, 3.0))
    f = _finder(monkeypatch)
    f.gluetun = True
    assert (f.find(6881).port, f.find(6881).source) == (5914, "gluetun")
    srv.shutdown()


def test_basic_auth_header():
    h = vpnport._gluetun_headers('{"auth":"basic","username":"u","password":"p"}')
    assert h == {"Authorization": "Basic dTpw"}
    assert vpnport._gluetun_headers('{"auth":"none"}') == {}


# ---------------------------------------------------------------- setup: what gluetun will do


@pytest.mark.parametrize("env,forwards,says", [
    ({"VPN_SERVICE_PROVIDER": "nordvpn"}, False, "forwards no ports"),
    ({"VPN_SERVICE_PROVIDER": "protonvpn"}, False, "VPN_PORT_FORWARDING=on"),
    ({"VPN_SERVICE_PROVIDER": "protonvpn", "VPN_PORT_FORWARDING": "on"}, True, "Flotilla listens on it"),
    ({"VPN_SERVICE_PROVIDER": "private internet access", "VPN_PORT_FORWARDING": "on"}, None,
     "HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE"),
    ({"VPN_SERVICE_PROVIDER": "private internet access", "VPN_PORT_FORWARDING": "on",
      "HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE": '{"auth":"apikey","apikey":"k"}'}, True, "Flotilla listens on it"),
])
def test_gluetun_forwarding_advice(env, forwards, says):
    ok, why = containers.VpnContainer("gluetun", "qmcgaw/gluetun", env=env).forwarding()
    assert ok is forwards and says in why


def test_node_gets_gluetuns_key_and_address():
    vpn = containers.VpnContainer("gluetun", "qmcgaw/gluetun", env={
        "HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE": '{"auth":"apikey","apikey":"k"}',
        "HTTP_CONTROL_SERVER_ADDRESS": ":9999"})
    cmd = containers.node_command(vpn, "flotilla", "img")
    assert f'{vpnport.GLUETUN_AUTH_ENV}={{"auth":"apikey","apikey":"k"}}' in cmd
    assert "FLOTILLA_GLUETUN_URL=http://127.0.0.1:9999" in cmd
    assert cmd[-2:] == ["img", "run"]
    plain = containers.node_command(containers.VpnContainer("wg", "linuxserver/wireguard"), "flotilla", "img")
    assert not any("GLUETUN" in part for part in plain)


# ---------------------------------------------------------------- the engine moves its listen socket


def test_engine_listens_on_the_forwarded_port():
    ses = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                      "enable_upnp": False, "enable_natpmp": False})
    cfg = Config(bind_interface="127.0.0.1")
    logs = []
    eng = SimpleNamespace(ses=ses, cfg=cfg, log=logs.append, listening_on=0, _announced=0)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free = probe.getsockname()[1]
    probe.close()
    Engine._listen_on(eng, vpnport.Forward(free, "NAT-PMP 10.2.0.1"))
    for _ in range(50):
        if ses.listen_port() == free:
            break
        ses.wait_for_alert(100)
    assert ses.listen_port() == free and eng.listening_on == free
    assert ses.get_settings()["announce_port"] == 0
    Engine._listen_on(eng, vpnport.Forward(40000, "NAT-PMP 10.2.0.1", listen=free))
    assert ses.get_settings()["announce_port"] == 40000
    assert "forwards port 40000 (to" in logs[-1]


def test_listen_interfaces():
    assert listen_interfaces(Config(bind_interface="wg0"), 5914) == "wg0:5914"
    assert listen_interfaces(Config(), 6881) == "0.0.0.0:6881,[::]:6881"
