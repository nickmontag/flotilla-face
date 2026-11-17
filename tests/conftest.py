import pytest


@pytest.fixture(autouse=True)
def _no_host_vpn_lookups(monkeypatch):
    """Setup tests must not depend on the test machine's VPN tunnels, Docker containers, home network
    or Tailscale, and a setup that binds to a VPN mustn't leave its network pin behind for the next test."""
    from flotilla import access, cli, netinfo, netsafe

    monkeypatch.setattr(cli, "_vpn_containers", lambda: [])
    monkeypatch.setattr(cli, "_split_vpns", lambda route: [])
    monkeypatch.setattr(netinfo, "lan_address", lambda: None)
    monkeypatch.setattr(access, "tailscale_name", lambda: None)
    yield
    netsafe.pin("")
