import pytest

TEST_IMAGE = "ghcr.io/nickmontag/flotilla-face@sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _no_host_vpn_lookups(monkeypatch):
    """Setup tests must not depend on the test machine's VPN tunnels, Docker containers, home network
    or Tailscale, and a setup that binds to a VPN mustn't leave its network pin behind for the next test."""
    from flotilla import access, cli, containers, netinfo, netsafe

    monkeypatch.setattr(cli, "_vpn_containers", lambda: [])
    monkeypatch.setattr(cli, "_split_vpns", lambda route: [])
    monkeypatch.setattr(netinfo, "lan_address", lambda: None)
    monkeypatch.setattr(access, "tailscale_name", lambda: None)
    monkeypatch.setattr(access, "tailscale_serving", lambda port: None)
    monkeypatch.setattr(access, "public_address", lambda: None)
    monkeypatch.setattr(access, "in_container", lambda: False)
    monkeypatch.delenv(access.ACCESS_ENV, raising=False)
    monkeypatch.delenv(access.URL_ENV, raising=False)
    monkeypatch.setattr(containers, "release_image", lambda: (TEST_IMAGE, "0.2.0"))
    monkeypatch.setattr(containers, "_docker", lambda *a: None)     # never the machine's real Docker
    yield
    netsafe.pin("")
