import pytest

TEST_IMAGE = "ghcr.io/nickmontag/flotilla-face@sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _no_real_config_folder(monkeypatch, tmp_path):
    """Defaults that point into the config folder (your picks, my-priorities.json) point into a
    temporary one: a test must never read or change the real ~/.config/flotilla."""
    from flotilla import config

    monkeypatch.setattr(config, "_default_config_dir", lambda: tmp_path / "config-home")


@pytest.fixture(autouse=True)
def _fresh_pirate_face_pacing(monkeypatch):
    """Pirate Face pacing is process-wide: no pause carries from one test to the next, and no gap slows them."""
    from flotilla import sources

    monkeypatch.setattr(sources, "PF_MIN_GAP", 0.0)
    monkeypatch.setattr(sources, "_pf_last", 0.0)
    monkeypatch.setattr(sources, "_pf_not_before", 0.0)


@pytest.fixture(autouse=True)
def _enter_at_hidden_prompts(monkeypatch):
    """A hidden prompt (setup's Pirate Face paste, say) gets Enter unless a test pastes something."""
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")


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
    monkeypatch.delenv("FLOTILLA_PF_KEY_FILE", raising=False)
    monkeypatch.setattr(containers, "release_image", lambda: (TEST_IMAGE, "0.2.0"))
    monkeypatch.setattr(containers, "_docker", lambda *a: None)     # never the machine's real Docker
    yield
    netsafe.pin("")
