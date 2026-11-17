"""Signed releases, `flotilla update`, the daily notice, and `flotilla editor release`."""

import argparse
import hashlib
import json
import subprocess
import tomllib
import types
from pathlib import Path

import pytest

import flotilla
from flotilla import cli, containers, release, service
from flotilla.netsafe import Response
from flotilla.signing import SecretKey

IMG = release.IMAGE_REPO + "@sha256:" + "c" * 64


@pytest.fixture
def key():
    return SecretKey.generate()


def _dist(tmp_path, version="9.9.9"):
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / f"flotilla_face-{version}-py3-none-any.whl").write_bytes(b"wheel " + version.encode())
    (dist / f"flotilla_face-{version}.tar.gz").write_bytes(b"sdist " + version.encode())
    return dist


def _signed(tmp_path, key, version="9.9.9", image=IMG, comment=None):
    manifest = release.make_manifest(version, "What's new", image, _dist(tmp_path, version))
    return manifest, key.sign(manifest, trusted_comment=comment or release.trusted_comment(version))


def test_version_is_set_in_one_place():
    doc = tomllib.loads(Path("pyproject.toml").read_text())
    assert "version" in doc["project"]["dynamic"] and "version" not in doc["project"]
    assert doc["tool"]["hatch"]["version"]["path"] == "flotilla/__init__.py"
    assert release.version_tuple(flotilla.__version__)


def test_a_signed_manifest_reads_back(tmp_path, key):
    manifest, sig = _signed(tmp_path, key)
    rel = release.parse(manifest, sig, [key.public])
    assert rel.version == "9.9.9" and rel.tag == "v9.9.9" and rel.image == IMG
    assert rel.wheel == "flotilla_face-9.9.9-py3-none-any.whl"
    assert rel.files[rel.wheel]["sha256"] == hashlib.sha256(b"wheel 9.9.9").hexdigest()
    assert rel.url(rel.wheel).endswith("/releases/download/v9.9.9/flotilla_face-9.9.9-py3-none-any.whl")


def test_tampering_or_another_key_is_refused(tmp_path, key):
    manifest, sig = _signed(tmp_path, key)
    with pytest.raises(release.ReleaseError, match="signature"):
        release.parse(manifest.replace(b"c" * 64, b"d" * 64), sig, [key.public])
    with pytest.raises(release.ReleaseError, match="not trusted"):
        release.parse(manifest, sig, [SecretKey.generate().public])


def test_a_signature_made_for_something_else_is_refused(tmp_path, key):
    """A priority-list signature (or one for another version) can't pass as a release's."""
    manifest, sig = _signed(tmp_path, key, comment="timestamp:1\tfile:release.json\tversion:3")
    with pytest.raises(release.ReleaseError, match="made for something else"):
        release.parse(manifest, sig, [key.public])
    manifest, sig = _signed(tmp_path, key, comment=release.trusted_comment("9.9.8"))
    with pytest.raises(release.ReleaseError, match="made for something else"):
        release.parse(manifest, sig, [key.public])


def test_the_image_must_be_ours_and_by_digest(tmp_path, key):
    for image in ("ghcr.io/nickmontag/flotilla-face:latest", "ghcr.io/someone/else@sha256:" + "c" * 64):
        manifest, sig = _signed(tmp_path, key, image=image)
        with pytest.raises(release.ReleaseError, match="by digest"):
            release.parse(manifest, sig, [key.public])


def test_only_ever_newer():
    assert release.newer("0.3.0", "0.2.0") and release.newer("0.10.0", "0.9.9")
    assert not release.newer("0.2.0", "0.2.0") and not release.newer("0.1.9", "0.2.0")
    assert not release.newer("0.3.0.dev1", "0.2.0") and not release.newer("0.3.0", "0.2.0.dev1")


def test_download_checks_the_hash(tmp_path, key, monkeypatch):
    manifest, sig = _signed(tmp_path, key)
    rel = release.parse(manifest, sig, [key.public])
    served = {rel.url(rel.wheel): b"wheel 9.9.9"}
    monkeypatch.setattr("flotilla.netsafe.fetch", lambda url, **kw: Response(url, 200, {}, served[url]))
    assert release.download(rel, rel.wheel, tmp_path).read_bytes() == b"wheel 9.9.9"
    served[rel.url(rel.wheel)] = b"wheel 9.9.9 with something extra"
    with pytest.raises(release.ReleaseError, match="doesn't match the signed release"):
        release.download(rel, rel.wheel, tmp_path)


def test_latest_fetches_both_files_from_github_only(tmp_path, key, monkeypatch):
    manifest, sig = _signed(tmp_path, key)
    asked = []

    def fetch(url, **kw):
        asked.append((url, kw["allowed"]))
        return Response(url, 200, {}, manifest if url.endswith(".json") else sig.encode())

    monkeypatch.setattr("flotilla.netsafe.fetch", fetch)
    assert release.latest(keys=[key.public]).version == "9.9.9"
    assert [u for u, _ in asked] == [f"https://github.com/{release.REPO}/releases/latest/download/release.json",
                                     f"https://github.com/{release.REPO}/releases/latest/download/release.json.minisig"]
    assert all(a == release.RELEASE_HOSTS for _, a in asked)


# ---------------------------------------------------------------- flotilla update


def _rel(version="9.9.9", image=IMG):
    files = {f"flotilla_face-{version}-py3-none-any.whl": {"sha256": "0" * 64, "size": 1}}
    return release.Release(version, f"v{version}", "- faster\n- safer", image, files)


def _installed(name, image, args, ours=True):
    return containers.Installed(name, ours, True, "", "flotilla", "", image, args, {})


@pytest.fixture
def updating(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(release, "latest", lambda: _rel())
    monkeypatch.setattr(cli, "_install_kind", lambda: "uv-tool")
    monkeypatch.setattr(release, "download", lambda rel, name: calls.append(("download", name)) or tmp_path / name)
    monkeypatch.setattr(cli, "_install_wheel", lambda kind, wheel: calls.append(("install", kind, wheel.name)) or True)
    monkeypatch.setattr(service, "installed", lambda: True)
    monkeypatch.setattr(service, "execute", lambda plan, dry_run, say: calls.append(("restart",)) or True)
    monkeypatch.setattr(cli.shutil, "which", lambda n: None)                 # no Docker unless a test adds it
    monkeypatch.setattr(containers, "pull", lambda image: calls.append(("pull", image)))
    monkeypatch.setattr(containers, "replace", lambda old, image: calls.append(("replace", old.name, image)))
    monkeypatch.setattr(containers, "recover", lambda: [])
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    return calls


def test_update_installs_the_signed_wheel_and_restarts_the_service(updating, capsys):
    assert cli.cmd_update(argparse.Namespace(check=False, yes=False)) == 0
    assert updating == [("download", "flotilla_face-9.9.9-py3-none-any.whl"),
                        ("install", "uv-tool", "flotilla_face-9.9.9-py3-none-any.whl"), ("restart",)]
    out = capsys.readouterr().out
    assert f"{flotilla.__version__} → 9.9.9" in out and "- faster" in out


def test_update_check_only_tells(updating, capsys):
    assert cli.cmd_update(argparse.Namespace(check=True, yes=False)) == 0
    assert updating == [] and "run `flotilla update` to install it" in capsys.readouterr().out


def test_nothing_to_do_when_up_to_date(updating, monkeypatch, capsys):
    monkeypatch.setattr(release, "latest", lambda: _rel(flotilla.__version__))
    assert cli.cmd_update(argparse.Namespace(check=False, yes=True)) == 0
    assert updating == [] and "Everything is up to date" in capsys.readouterr().out


def test_update_replaces_docker_containers_by_digest(updating, monkeypatch):
    monkeypatch.setattr(release, "latest", lambda: _rel())
    monkeypatch.setattr(cli, "_install_kind", lambda: "dev")                 # the host copy isn't updated here
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/docker" if n == "docker" else None)
    monkeypatch.setattr(containers, "installed", lambda: {
        "flotilla": _installed("flotilla", "python:3.13-slim", ["run"]),          # set up before images
        "flotilla-dashboard": _installed("flotilla-dashboard", IMG, ["dashboard", "--serve"])})   # already new
    assert cli.cmd_update(argparse.Namespace(check=False, yes=True)) == 0
    assert updating == [("pull", IMG), ("replace", "flotilla", IMG)]


def test_inside_a_container_it_points_at_the_host(updating, monkeypatch, capsys):
    monkeypatch.setattr("flotilla.access.in_container", lambda: True)
    assert cli.cmd_update(argparse.Namespace(check=False, yes=True)) == 2
    assert "Update from the machine running Docker" in capsys.readouterr().out and updating == []


def test_a_bad_download_installs_nothing(updating, monkeypatch, capsys):
    def bad(rel, name):
        raise release.ReleaseError("doesn't match the signed release")

    monkeypatch.setattr(release, "download", bad)
    assert cli.cmd_update(argparse.Namespace(check=False, yes=True)) == 1
    assert not any(c[0] == "install" for c in updating)


# ---------------------------------------------------------------- recreating containers


PIP_DASHBOARD = {
    "Name": "/flotilla-dashboard",
    "Config": {"Image": "python:3.13-slim", "Entrypoint": None,
               "Cmd": ["sh", "-c", "pip install -q x.tar.gz && exec flotilla dashboard --serve --listen 0.0.0.0 "
                                   "--port 7421 --access lan"],
               "Env": ["PATH=/usr/local/bin", "FLOTILLA_DASHBOARD_ACCESS=lan",
                       "FLOTILLA_DASHBOARD_URL=http://10.0.0.206:7421"]},
    "HostConfig": {"NetworkMode": "bridge", "RestartPolicy": {"Name": "unless-stopped"},
                   "PortBindings": {"7421/tcp": [{"HostIp": "10.0.0.206", "HostPort": "7421"}]},
                   "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}}},
    "Mounts": [{"Type": "bind", "Source": "/mnt/faststorage/apps/flotilla", "Destination": "/root"}],
    "State": {"Running": True},
}
IMAGE_NODE = {
    "Name": "/flotilla",
    "Config": {"Image": IMG, "Entrypoint": ["flotilla"], "Cmd": ["run"], "Env": ["HOME=/root"]},
    "HostConfig": {"NetworkMode": "container:" + "f" * 64, "RestartPolicy": {"Name": "unless-stopped"},
                   "Memory": 3221225472, "MemorySwap": 3221225472, "LogConfig": {"Config": {}}},
    "Mounts": [{"Type": "volume", "Name": "flotilla", "Destination": "/root"}],
    "State": {"Running": True},
}


def _from(doc):
    got = {}
    docs = [doc]
    import flotilla.containers as c

    old = c._docker
    c._docker = lambda *a: subprocess.CompletedProcess(a, 0, json.dumps(docs), "")
    try:
        got = c.installed((doc["Name"][1:],))
    finally:
        c._docker = old
    return got[doc["Name"][1:]]


def test_what_a_container_runs_old_and_new_style():
    assert containers.flotilla_args(PIP_DASHBOARD) == ["dashboard", "--serve", "--listen", "0.0.0.0", "--port",
                                                       "7421", "--access", "lan"]
    assert containers.flotilla_args(IMAGE_NODE) == ["run"]
    assert _from(PIP_DASHBOARD).ours and _from(IMAGE_NODE).ours


def test_recreated_with_the_same_settings_on_the_new_image(monkeypatch):
    new = "ghcr.io/nickmontag/flotilla-face@sha256:" + "e" * 64
    dash = containers.run_args(_from(PIP_DASHBOARD), new)
    assert dash[:5] == ["docker", "run", "-d", "--name", "flotilla-dashboard"]
    assert "10.0.0.206:7421:7421" in dash and "unless-stopped" in dash and "max-size=10m" in dash
    assert "FLOTILLA_DASHBOARD_URL=http://10.0.0.206:7421" in dash and "PATH=/usr/local/bin" not in dash
    assert "/mnt/faststorage/apps/flotilla:/root" in dash
    assert dash[dash.index(new):] == [new, "dashboard", "--serve", "--listen", "0.0.0.0", "--port", "7421",
                                      "--access", "lan"]
    monkeypatch.setattr(containers, "_docker", lambda *a: subprocess.CompletedProcess(a, 0, "/gluetun\n", ""))
    node = containers.run_args(_from(IMAGE_NODE), new)
    assert node[node.index("--network") + 1] == "container:gluetun"            # by name, not a stale id
    assert "3221225472" in node and "flotilla:/root" in node and node[-2:] == [new, "run"]


def _stop(monkeypatch, calls, rc=0):
    def run(cmd, **kw):
        calls.append(tuple(cmd[1:]))
        return subprocess.CompletedProcess(cmd, rc, "", "timed out" if rc else "")

    monkeypatch.setattr(containers.subprocess, "run", run)


def test_a_new_container_that_wont_start_puts_the_old_one_back(monkeypatch):
    calls = []
    monkeypatch.setattr(containers, "_docker", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    _stop(monkeypatch, calls)
    monkeypatch.setattr(containers, "run", lambda cmd, interactive=False: 125)
    with pytest.raises(containers.DockerError, match="old one is running again"):
        containers.replace(_from(PIP_DASHBOARD), IMG)
    keep = "flotilla-dashboard-before-update"
    assert ("rename", "flotilla-dashboard", keep) in calls and ("stop", "-t", "60", keep) in calls
    assert calls[-2:] == [("rename", keep, "flotilla-dashboard"), ("start", "flotilla-dashboard")]


def test_an_old_container_that_wont_stop_keeps_running_under_its_name(monkeypatch):
    calls = []
    monkeypatch.setattr(containers, "_docker", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    _stop(monkeypatch, calls, rc=1)
    monkeypatch.setattr(containers, "run", lambda *a, **k: pytest.fail("started a new one"))
    with pytest.raises(containers.DockerError, match="running as before"):
        containers.replace(_from(PIP_DASHBOARD), IMG)
    keep = "flotilla-dashboard-before-update"
    assert calls[-2:] == [("rename", keep, "flotilla-dashboard"), ("start", "flotilla-dashboard")]


def test_a_new_container_that_starts_replaces_the_old_one(monkeypatch):
    calls = []
    monkeypatch.setattr(containers, "_docker", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    _stop(monkeypatch, calls)
    monkeypatch.setattr(containers, "run", lambda cmd, interactive=False: calls.append(("run",)) or 0)
    containers.replace(_from(PIP_DASHBOARD), IMG)
    assert calls[-2:] == [("run",), ("rm", "-f", "flotilla-dashboard-before-update")]


# ---------------------------------------------------------------- the daily notice


def _node(check=True):
    logs = []
    node = types.SimpleNamespace(cfg=types.SimpleNamespace(check_updates=check), log=logs.append,
                                 update_available=None)
    return node, logs


def test_notice_when_a_newer_release_is_out(monkeypatch):
    from flotilla.engine import Engine

    monkeypatch.setattr(release, "latest", lambda: _rel())
    node, logs = _node()
    Engine.check_update(node)
    Engine.check_update(node)
    assert node.update_available == {"version": "9.9.9", "page": _rel().page, "where": "here"}
    assert len(logs) == 1 and "Flotilla 9.9.9 is out" in logs[0]           # said once, not every day


def test_no_notice_when_current_offline_or_turned_off(monkeypatch):
    from flotilla.engine import Engine
    from flotilla.netsafe import FetchError

    monkeypatch.setattr(release, "latest", lambda: _rel(flotilla.__version__))
    node, logs = _node()
    Engine.check_update(node)
    assert node.update_available is None

    def offline():
        raise FetchError("https://github.com", None, "offline")

    monkeypatch.setattr(release, "latest", offline)
    Engine.check_update(node)
    assert node.update_available is None and logs == []
    monkeypatch.setattr(release, "latest", lambda: pytest.fail("checked while turned off"))
    node, _ = _node(check=False)
    Engine.check_update(node)


# ---------------------------------------------------------------- flotilla editor release


def test_editor_release_signs_and_publishes_the_draft(tmp_path, key, monkeypatch, capsys):
    secret = tmp_path / "editor.key"
    key.save(secret)
    monkeypatch.setattr(release, "builtin_keys", lambda: [key.public])
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/gh")
    gh = []

    def run(cmd, capture_output=True, text=True, **kw):
        gh.append(cmd[1:3])
        if cmd[1:3] == ["release", "download"]:
            out = Path(cmd[cmd.index("--dir") + 1])
            for f in _dist(tmp_path / "ci", "0.9.0").iterdir():
                (out / f.name).write_bytes(f.read_bytes())
            (out / "image.txt").write_text(IMG + "\n")
        if cmd[1:3] == ["release", "upload"]:
            files = [Path(a) for a in cmd if a.endswith(("release.json", ".minisig"))]
            release.parse(files[0].read_bytes(), files[1].read_text(), [key.public])
        return subprocess.CompletedProcess(cmd, 0, "Notes for 0.9.0" if cmd[2] == "view" else "", "")

    monkeypatch.setattr(subprocess, "run", run)
    (tmp_path / "ci").mkdir()
    args = argparse.Namespace(file="v0.9.0", secret=str(secret), dry_run=False, action="release", config="x")
    assert cli.cmd_editor(args) == 0
    assert [g[1] for g in gh] == ["download", "view", "upload", "edit"]
    assert "Published" in capsys.readouterr().out


def test_editor_release_refuses_a_wrong_wheel(tmp_path, key, monkeypatch):
    secret = tmp_path / "editor.key"
    key.save(secret)
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/gh")

    def run(cmd, **kw):
        if cmd[1:3] == ["release", "download"]:
            out = Path(cmd[cmd.index("--dir") + 1])
            (out / "flotilla_face-0.8.0-py3-none-any.whl").write_bytes(b"old")
            (out / "image.txt").write_text(IMG)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="expected the 0.9.0 wheel"):
        cli.cmd_editor(argparse.Namespace(file="v0.9.0", secret=str(secret), dry_run=False, action="release",
                                          config="x"))


def test_no_answer_means_no_update(updating, monkeypatch, capsys):
    def closed(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    assert cli.cmd_update(argparse.Namespace(check=False, yes=False)) == 1
    assert updating == [] and "--yes" in capsys.readouterr().out


def test_an_environment_without_pip_installs_with_uv(monkeypatch, tmp_path):
    import importlib.util

    ran = []
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "pip" else object())
    monkeypatch.setattr(cli, "_uv", lambda: "/usr/local/bin/uv")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert cli._install_wheel("pip", tmp_path / "w.whl")
    assert ran[0][:3] == ["/usr/local/bin/uv", "pip", "install"]


def test_containers_are_updated_by_the_version_just_installed(updating, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/docker" if n == "docker" else None)
    monkeypatch.setattr(containers, "installed", lambda: {"flotilla": _installed("flotilla", "old", ["run"])})
    monkeypatch.setattr(service, "executable", lambda: ["/home/u/.local/bin/flotilla"])
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    assert cli.cmd_update(argparse.Namespace(check=False, yes=True)) == 0
    assert ran == [["/home/u/.local/bin/flotilla", "update", "--yes"]]
    assert not any(c[0] in ("pull", "replace") for c in updating)          # not by this, older, copy


def test_a_container_left_renamed_by_an_interrupted_update_is_put_back(monkeypatch):
    keep = dict(PIP_DASHBOARD, Name="/flotilla-dashboard-before-update")
    calls = []

    def docker(*a):
        calls.append(a)
        if a[0] == "inspect":
            return subprocess.CompletedProcess(a, 1, json.dumps([keep]), "No such object: flotilla-dashboard")
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(containers, "_docker", docker)
    assert containers.recover(("flotilla-dashboard",)) == ["flotilla-dashboard"]
    assert calls[-2:] == [("rename", "flotilla-dashboard-before-update", "flotilla-dashboard"),
                          ("start", "flotilla-dashboard")]
