import pytest


@pytest.fixture(autouse=True)
def _no_host_vpn_lookups(monkeypatch):
    """Setup tests must not depend on the test machine's VPN tunnels or Docker containers, and a
    setup that binds to a VPN mustn't leave its network pin behind for the next test."""
    from flotilla import cli, netsafe

    monkeypatch.setattr(cli, "_vpn_containers", lambda: [])
    monkeypatch.setattr(cli, "_split_vpns", lambda route: [])
    yield
    netsafe.pin("")
