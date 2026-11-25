"""Regression tests for the config / feed / cli audit fixes: feed shape errors, the deny list across an
expired feed, concurrent edits of my-priorities.json, `init --yes` honesty, numeric limits, [net] in the
saved config, state.json's backup, `verify`, `uninstall --delete-data`, and temp folders."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from flotilla import cli, netinfo, release
from flotilla.config import (NUMBER_LIMITS, Config, State, StateError, check_number, load_config, render_config,
                             write_config)
from flotilla.feed import FeedError, add_pick, apply_local, list_picks, load_feed, neutral_feed, parse_feed, verify_feed
from flotilla.signing import SecretKey


def doc(version=3, days=30, **extra):
    now = datetime.now(timezone.utc)
    d = {"schema": 1, "version": version, "issued": now.isoformat(),
         "expires": (now + timedelta(days=days)).isoformat(),
         "weights": {"capability": 1, "cyber": 1, "takedown_risk": 2},
         "models": [{"repo": "zai-org/GLM-5.2", "scores": {"capability": 90, "cyber": 70, "takedown_risk": 50}}],
         "deny": ["bad/model"]}
    d.update(extra)
    return json.dumps(d).encode()


def put(tmp_path, name, data, sk):
    (tmp_path / name).write_bytes(data)
    (tmp_path / (name + ".minisig")).write_text(sk.sign(data))
    return tmp_path / name


# ---------------------------------------------------------------- 1: feed shapes


@pytest.mark.parametrize("bad", [
    {"defaults": []},
    {"defaults": "x"},
    {"defaults": {"target_replicas": None}},
    {"defaults": {"target_replicas": True}},
    {"defaults": {"target_replicas": 0}},
    {"defaults": {"target_replicas": "5"}},
    {"defaults": {"unlisted_priority": None}},
    {"defaults": {"unlisted_priority": [1]}},
    {"defaults": {"unlisted_priority": 7}},
    {"weights": []},
    {"weights": {"capability": True}},
    {"version": True},
    {"models": [{"repo": "a/b", "quant": ["q4"]}]},
    {"models": [{"repo": "a/b", "quant": {"x": 1}}]},
    {"models": [{"repo": "a/b", "scores": []}]},
    {"models": [{"repo": "a/b", "scores": {"capability": None}}]},
    {"models": [{"repo": "a/b", "scores": {"capability": True}}]},
    {"models": [{"repo": "a/b", "target_replicas": True}]},
    {"models": [{"repo": "a/b", "boost": None}]},
    {"models": [{"repo": "a/b", "origin": []}]},
    {"deny": [["x"]]},
])
def test_a_misshapen_signed_feed_is_a_feed_error_not_a_crash(bad):
    sk = SecretKey.generate()
    data = doc(**bad)
    with pytest.raises(FeedError):
        parse_feed(data)
    with pytest.raises(FeedError):          # and the same once the signature has checked out
        verify_feed(data, sk.sign(data), [sk.public], 0)


def test_non_finite_numbers_in_a_feed_are_refused():
    for text in (b'{"x": NaN}',):
        assert json.loads(text)                     # Python's JSON accepts NaN, so the feed must not
    for bad in ({"weights": {"capability": float("nan")}},
                {"defaults": {"unlisted_priority": float("inf")}},
                {"models": [{"repo": "a/b", "scores": {"capability": float("nan")}}]},
                {"models": [{"repo": "a/b", "scores": {"capability": 10 ** 400}}]}):
        with pytest.raises(FeedError):
            parse_feed(json.dumps({**json.loads(doc()), **bad}).encode())


def test_a_good_feed_still_parses():
    f = parse_feed(doc(defaults={"unlisted_priority": 0.2, "target_replicas": 4},
                       models=[{"repo": "a/b", "quant": "q4_k_m", "scores": {"capability": 5}, "target_replicas": 3}]))
    assert f.unlisted_priority == 0.2 and f.target_replicas == 4 and f.models[0].quant == "q4_k_m"


def test_a_bad_local_entry_is_not_fatal_and_leaves_the_published_list_alone(tmp_path):
    sk = SecretKey.generate()
    data = doc()
    feed = verify_feed(data, sk.sign(data), [sk.public], 0)
    for bad in ({"models": [{"repo": "a/b", "quant": ["q4"]}]},
                {"models": [{"repo": "a/b", "quant": {"k": 1}}]},
                {"models": [{"repo": "a/b", "scores": {"capability": []}}]},
                {"weights": {"capability": None}},
                {"weights": {"capability": [1]}},
                {"order": "x"}, {"allot_gb": []}, {"models": 5}):
        local = tmp_path / "my.json"
        local.write_text(json.dumps(bad))
        logs = []
        out = apply_local(feed, local, log=logs.append)          # must not raise
        assert out is feed and [m.repo for m in out.models] == ["zai-org/GLM-5.2"] and "ignoring" in logs[0]


# ---------------------------------------------------------------- 2: the deny list across an expired feed


def test_an_expired_cached_feed_keeps_its_deny_list_but_not_its_priorities(tmp_path):
    sk = SecretKey.generate()
    (tmp_path / "c").mkdir()
    put(tmp_path / "c", "feed.json", doc(days=-5), sk)
    logs = []
    feed = load_feed([], [sk.public], 0, tmp_path / "c", (), log=logs.append)
    assert feed.version == 0 and feed.models == []              # the expired list's priorities are not used
    assert feed.deny == {"bad/model"}                           # but what it denied stays denied
    assert any("keeping" in l for l in logs) and "deny list" in feed.source


def test_an_expired_mirror_copy_keeps_its_deny_list_too(tmp_path):
    sk = SecretKey.generate()
    put(tmp_path, "r.json", doc(days=-1, deny=["gone/model"]), sk)
    feed = load_feed([f"file://{tmp_path}/r.json"], [sk.public], 0, tmp_path / "cache", (), log=lambda *_: None)
    assert feed.models == [] and feed.deny == {"gone/model"}


def test_a_bundled_list_older_than_what_was_seen_still_contributes_its_deny_list(tmp_path):
    sk = SecretKey.generate()
    b = put(tmp_path, "b.json", doc(version=3), sk)
    feed = load_feed([], [sk.public], 9, tmp_path / "c", (), log=lambda *_: None, bundled=b)
    assert feed.version == 0 and feed.models == [] and feed.deny == {"bad/model"}


def test_denies_from_every_verified_source_are_joined(tmp_path):
    sk = SecretKey.generate()
    (tmp_path / "c").mkdir()
    put(tmp_path / "c", "feed.json", doc(version=4, days=-2, deny=["one/model"]), sk)
    b = put(tmp_path, "b.json", doc(version=3, deny=["two/model"]), sk)
    feed = load_feed([], [sk.public], 4, tmp_path / "c", (), log=lambda *_: None, bundled=b)
    assert feed.deny == {"one/model", "two/model"} and feed.models == []


def test_a_forged_expired_feed_carries_nothing(tmp_path):
    sk, mallory = SecretKey.generate(), SecretKey.generate()
    (tmp_path / "c").mkdir()
    put(tmp_path / "c", "feed.json", doc(days=-5, deny=["victim/model"]), mallory)
    feed = load_feed([], [sk.public], 0, tmp_path / "c", (), log=lambda *_: None)
    assert feed.deny == set()


def test_a_current_feed_is_not_polluted_by_an_old_deny_list(tmp_path):
    sk = SecretKey.generate()
    (tmp_path / "c").mkdir()
    put(tmp_path / "c", "feed.json", doc(version=2, days=-5, deny=["old/denied"]), sk)
    r = put(tmp_path, "r.json", doc(version=5, deny=[]), sk)
    feed = load_feed([f"file://{r}"], [sk.public], 0, tmp_path / "c", (), log=lambda *_: None)
    assert feed.version == 5 and feed.deny == set()          # the editors' current word wins


def test_neutral_feed_takes_a_deny_set():
    assert neutral_feed({"a/b"}).deny == {"a/b"} and neutral_feed().deny == set()


# ---------------------------------------------------------------- 3: my-priorities.json under concurrent edits


def test_concurrent_picks_all_survive_and_leave_no_temp_files(tmp_path):
    path = tmp_path / "cfg" / "my-priorities.json"
    errors = []

    def add(i):
        try:
            add_pick(path, f"author/model-{i}")
        except Exception as e:          # noqa: BLE001 - the test reports whatever went wrong
            errors.append(e)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert {m["repo"] for m in list_picks(path)} == {f"author/model-{i}" for i in range(40)}
    assert [p.name for p in path.parent.iterdir()] == ["my-priorities.json"]


# ---------------------------------------------------------------- init --yes


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(netinfo, "internet_route", lambda: None)
    monkeypatch.setattr(cli, "_split_vpns", lambda route: [])
    monkeypatch.setattr(cli, "_vpn_containers", lambda: [])
    monkeypatch.setattr(cli, "_in_setup_container", lambda: False)
    monkeypatch.setattr(cli, "_exclude_from_time_machine", lambda folder: None)
    monkeypatch.setattr(netinfo, "virtualization", lambda: "")


REL = "rel/seeds"      # passed as given, to see it made absolute


def init(tmp_path, *flags, folder="seed"):
    args = ["--config", str(tmp_path / "config.toml"), "--state", str(tmp_path / "state"), "init", "--yes"]
    if folder:
        args += ["--folder", folder if os.path.isabs(folder) or folder.startswith("~") or folder == REL
                                else str(tmp_path / folder)]
    return cli.main([*args, *flags])


def test_init_keeps_the_router_port_unless_told_otherwise(tmp_path, no_network):
    assert init(tmp_path, "--seed-from-home-ip", "--router-port") == 0
    assert load_config(tmp_path / "config.toml").port_forwarding is True
    assert init(tmp_path, "--seed-from-home-ip", "--budget-gb", "30") == 0
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.port_forwarding is True and cfg.budget_gb == 30                   # untouched
    assert init(tmp_path, "--seed-from-home-ip", "--no-router-port") == 0
    assert load_config(tmp_path / "config.toml").port_forwarding is False


def test_pinned_to_a_vpn_never_opens_a_router_port(tmp_path, no_network):
    assert init(tmp_path, "--seed-from-home-ip", "--router-port") == 0
    assert init(tmp_path, "--vpn", "wg0") == 0
    assert load_config(tmp_path / "config.toml").port_forwarding is False


@pytest.mark.parametrize("flags", [
    ("--budget-gb", "nan"), ("--budget-gb", "inf"), ("--budget-gb", "0"), ("--budget-gb", "-3"),
    ("--reserve-gb", "nan"), ("--reserve-gb", "-1"),
    ("--upload-mbps", "nan"), ("--download-mbps", "-5"), ("--upload-mbps", "0"),
    ("--upload-share", "nan"), ("--download-share", "0"), ("--upload-share", "1.5"),
    ("--monthly-upload-cap-gb", "nan"), ("--monthly-upload-cap-gb", "-1"),
    ("--churn-gb", "inf"), ("--hf-gb-per-day", "nan"),
])
def test_init_refuses_nonsense_numbers_before_writing_anything(tmp_path, no_network, capsys, flags):
    assert init(tmp_path, "--seed-from-home-ip", *flags) == 2
    assert flags[0] in capsys.readouterr().err
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "seed").exists()                 # not even the folder


def test_the_all_nan_case_from_the_audit_is_refused(tmp_path, no_network):
    assert init(tmp_path, "--budget-gb", "nan", "--reserve-gb", "nan", "--upload-mbps", "nan",
                "--download-mbps", "-5") == 2
    assert not (tmp_path / "config.toml").exists() and not (tmp_path / "seed").exists()


def test_a_refused_init_creates_no_folder_for_other_bad_flags_either(tmp_path, no_network):
    for flags in (("--focus", "everything"), ("--reorient", "sometimes"), ("--dashboard", "world")):
        assert init(tmp_path, *flags) == 2
        assert not (tmp_path / "seed").exists() and not (tmp_path / "config.toml").exists()


def test_init_stores_the_folder_absolute(tmp_path, no_network, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert init(tmp_path, "--seed-from-home-ip", folder="rel/seeds") == 0
    assert load_config(tmp_path / "config.toml").folder == Path.cwd() / "rel" / "seeds"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert init(tmp_path, "--seed-from-home-ip", folder="~/tilde") == 0
    assert load_config(tmp_path / "config.toml").folder == tmp_path / "home" / "tilde"


def test_init_writes_the_config_atomically_and_keeps_net_hosts(tmp_path, no_network):
    (tmp_path / "config.toml").write_text('[net]\nallowed_hosts = ["pirateface.co", "mirror.example.org"]\n')
    assert init(tmp_path, "--seed-from-home-ip") == 0
    assert load_config(tmp_path / "config.toml").allowed_hosts == ["pirateface.co", "mirror.example.org"]
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("config")) == ["config.toml"]


def test_interactive_init_asks_again_for_a_nan(tmp_path, monkeypatch, no_network):
    # Enter (the default) for every question, except the folder and the upload speed, which gets nonsense first.
    speeds = iter(["nan", "-4", "25"])

    def ask(prompt=""):
        if "Folder" in prompt:
            return str(tmp_path / "s")
        return next(speeds) if "Upload speed" in prompt else ""

    monkeypatch.setattr("builtins.input", ask)
    assert cli.cmd_init(argparse.Namespace(config=str(tmp_path / "c.toml"))) == 0
    assert load_config(tmp_path / "c.toml").upload_mbps == 25


# ---------------------------------------------------------------- numeric limits


@pytest.mark.parametrize("attr, value", [
    ("budget_gb", "nan"), ("budget_gb", "inf"), ("budget_gb", '"100"'), ("budget_gb", "true"),
    ("reserve_gb", "nan"), ("upload_mbps", "nan"), ("download_mbps", "inf"),
    ("upload_share", "nan"), ("monthly_upload_cap_gb", "nan"), ("memory_gb", "nan"), ("hf_gb_per_day", "nan"),
    ("churn_gb", "nan"), ("refresh_hours", "nan"), ("spread", "nan"), ("hold_bonus", "nan"),
    ("min_hold_days", "nan"), ("listen_port", "70000"), ("dashboard_port", "0"), ("whole_model_under_gb", "nan"),
    ("max_models", "2.5"),
    # refused by earlier versions too
    ("budget_gb", "0"), ("upload_share", "0"), ("download_share", "1.5"), ("max_models", "0"),
    ("sweep_candidates", "999"), ("hf_gb_per_day", "-1"), ("churn_gb", "-1"),
])
def test_load_config_refuses_nonsense_numbers(tmp_path, attr, value):
    section, key = NUMBER_LIMITS[attr][0].split(".")
    p = tmp_path / "c.toml"
    p.write_text(f"[{section}]\n{key} = {value}\n")
    with pytest.raises(ValueError, match="must be"):
        load_config(p)


@pytest.mark.parametrize("attr, value, expect", [
    ("budget_gb", "0.5", 1), ("reserve_gb", "-1", 0), ("upload_share", "0.03", 0.05),
    ("refresh_hours", "0", 0.1), ("refresh_hours", "-2", 0.1), ("probe_seconds", "-1", 0),
    ("max_candidates", "-1", 0), ("max_models", "3.0", 3), ("upload_mbps", "0.01", 0.1),
])
def test_an_out_of_range_number_from_an_older_config_is_moved_to_the_limit(tmp_path, capsys, attr, value, expect):
    # Older versions accepted these, so an upgraded node must keep starting, and say what it changed.
    section, key = NUMBER_LIMITS[attr][0].split(".")
    p = tmp_path / "c.toml"
    p.write_text(f"[{section}]\n{key} = {value}\n")
    got = getattr(load_config(p), attr)
    assert got == expect and (type(got) is int) == NUMBER_LIMITS[attr][3]
    if value not in ("3.0",):
        assert "outside" in capsys.readouterr().err


def test_the_defaults_and_the_rendered_config_pass_the_limits(tmp_path):
    cfg = Config()
    for attr in NUMBER_LIMITS:
        check_number(attr, getattr(cfg, attr))
    write_config(cfg, tmp_path / "c.toml")
    assert load_config(tmp_path / "c.toml").budget_gb == cfg.budget_gb


def test_the_limits_agree_with_the_dashboards():
    from flotilla.engine import Engine

    for k, (lo, hi) in Engine.SETTINGS.items():
        assert NUMBER_LIMITS[k][1:3] == (lo, hi), k


def test_check_number_messages_name_the_setting():
    with pytest.raises(ValueError, match=r"--budget-gb must be a number from 1 to 1,000,000"):
        check_number("budget_gb", float("nan"), "--budget-gb")
    with pytest.raises(ValueError, match="network.max_models must be a whole number"):
        check_number("max_models", 2.5)


def test_parse_gb_refuses_nan_and_inf():
    for raw in ("nan", "inf", "-inf", "0", "-1"):
        with pytest.raises(ValueError):
            cli.parse_gb(raw)


# ---------------------------------------------------------------- [net] allowed_hosts survives saving


def test_net_allowed_hosts_round_trips_through_a_save(tmp_path):
    cfg = Config()
    cfg.allowed_hosts = ["pirateface.co", "mirror.example.org"]
    write_config(cfg, tmp_path / "c.toml")
    assert "[net]" in (tmp_path / "c.toml").read_text()
    again = load_config(tmp_path / "c.toml")
    assert again.allowed_hosts == ["pirateface.co", "mirror.example.org"]
    write_config(again, tmp_path / "c2.toml")                       # a second save (the dashboard's) keeps it
    assert load_config(tmp_path / "c2.toml").allowed_hosts == ["pirateface.co", "mirror.example.org"]


def test_net_is_left_out_while_it_is_the_default(tmp_path):
    assert "[net]" not in render_config(Config())          # the defaults keep following the program's


def test_write_config_is_atomic_and_keeps_the_files_permissions(tmp_path):
    p = tmp_path / "c.toml"
    write_config(Config(), p)
    os.chmod(p, 0o640)
    write_config(Config(), p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o640
    assert [x.name for x in tmp_path.iterdir()] == ["c.toml"]
    with pytest.raises(ValueError):
        write_config(Config())                              # no path anywhere


# ---------------------------------------------------------------- state.json


def test_state_save_keeps_the_previous_copy_and_fsyncs(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real(fd))
    st = State(tmp_path)
    st.holdings["a/b"] = {"save_path": "/x"}
    st.save()
    assert len(synced) >= 2 and not (tmp_path / "state.json.bak").exists()       # the file, then the folder
    st.holdings["c/d"] = {"save_path": "/y"}
    st.save()
    assert set(json.loads((tmp_path / "state.json.bak").read_text())["holdings"]) == {"a/b"}
    assert set(json.loads((tmp_path / "state.json").read_text())["holdings"]) == {"a/b", "c/d"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json", "state.json.bak"]


def test_a_torn_state_file_falls_back_to_the_backup_loudly(tmp_path, capsys):
    st = State(tmp_path)
    st.holdings["a/b"] = {"save_path": "/x"}
    st.save()
    st.holdings["c/d"] = {"save_path": "/y"}
    st.save()
    (tmp_path / "state.json").write_text('{"holdings": {"a/b": {"sa')           # a power cut mid-write
    again = State(tmp_path)
    assert set(again.holdings) == {"a/b"} and again.node_id == st.node_id
    err = capsys.readouterr().err
    assert "state.json" in err and "previous copy" in err
    # the first save afterwards must not make the torn file the backup
    again.save()
    assert json.loads((tmp_path / "state.json.bak").read_text())["holdings"].keys() == {"a/b"}
    assert State(tmp_path).holdings.keys() == {"a/b"}


def test_a_missing_state_file_with_a_backup_uses_the_backup(tmp_path, capsys):
    st = State(tmp_path)
    st.save()
    st.save()
    (tmp_path / "state.json").unlink()
    assert State(tmp_path).node_id == st.node_id and "is missing" in capsys.readouterr().err


def test_both_state_files_bad_stops_with_instructions(tmp_path):
    (tmp_path / "state.json").write_text("{torn")
    (tmp_path / "state.json.bak").write_text("also torn")
    with pytest.raises(StateError, match="won't start empty") as e:
        State(tmp_path)
    assert "move both files" in str(e.value)
    (tmp_path / "state.json.bak").unlink()
    (tmp_path / "state.json").write_text("{torn")
    with pytest.raises(StateError):                 # a bad file and no backup is not "a first run"
        State(tmp_path)
    (tmp_path / "state.json").write_text("[1, 2]")
    with pytest.raises(StateError):                 # valid JSON that isn't a state document
        State(tmp_path)


def test_a_first_run_starts_empty(tmp_path):
    st = State(tmp_path / "new")
    assert st.holdings == {} and len(st.node_id) == 32


def test_the_cli_reports_unreadable_state_as_an_error(tmp_path, capsys):
    (tmp_path / "state.json").write_text("{torn")
    rc = cli.main(["--config", str(tmp_path / "c.toml"), "--state", str(tmp_path), "verify"])
    assert rc == 1 and "won't start empty" in capsys.readouterr().err


# ---------------------------------------------------------------- verify


def test_verify_compares_hashes_ignoring_case(tmp_path, capsys):
    import hashlib

    f = tmp_path / "seeds" / "m" / "w.bin"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"weights")
    digest = hashlib.sha256(b"weights").hexdigest()
    st = State(tmp_path / "state")
    st.holdings["ih"] = {"repo": "a/b", "save_path": str(tmp_path / "seeds" / "m"),
                         "files": {"k": {"status": "verified", "tpath": "w.bin", "path": "w.bin",
                                         "sha256": digest.upper()}}}
    st.save()
    assert cli.cmd_verify(argparse.Namespace(state=str(tmp_path / "state"), folder=None)) == 0
    assert "1 file(s) verified, 0 bad" in capsys.readouterr().out
    st.holdings["ih"]["files"]["k"]["sha256"] = ("0" if digest[0] != "0" else "1") + digest[1:].upper()
    st.save()
    assert cli.cmd_verify(argparse.Namespace(state=str(tmp_path / "state"), folder=None)) == 1


# ---------------------------------------------------------------- uninstall --delete-data


def _node(tmp_path, monkeypatch):
    from flotilla import service

    conf = tmp_path / "conf"
    conf.mkdir()
    cfg = Config(folder=tmp_path / "seeds")
    cfg.local_feed = conf / "my-priorities.json"
    write_config(cfg, conf / "config.toml")
    model = tmp_path / "seeds" / "a--b"
    model.mkdir(parents=True)
    (model / "w.safetensors").write_bytes(b"0" * 100)
    monkeypatch.setattr(service, "uninstall_plan", lambda: service.Plan())
    monkeypatch.setattr(service, "service_log", lambda home=None: tmp_path / "Logs" / "flotilla.log")
    return conf, model


def _args(conf, state):
    return argparse.Namespace(config=str(conf / "config.toml"), state=str(state), delete_data=True, yes=True)


def test_uninstall_through_a_symlinked_state_dir_removes_only_the_link(tmp_path, monkeypatch, capsys):
    conf, model = _node(tmp_path, monkeypatch)
    real = tmp_path / "elsewhere" / "real-state"
    real.mkdir(parents=True)
    st = State(real)
    st.holdings["a/b"] = {"save_path": str(model)}
    st.save()
    (real / "precious.txt").write_text("not Flotilla's")
    link = tmp_path / "state"
    link.symlink_to(real, target_is_directory=True)
    assert cli.cmd_uninstall(_args(conf, link)) == 1                   # not everything was removed
    cap = capsys.readouterr()
    assert not link.exists() and not link.is_symlink()                  # the link is gone
    assert (real / "precious.txt").read_text() == "not Flotilla's" and (real / "state.json").exists()
    assert "only the link is removed" in cap.err and "Done." not in cap.out
    assert not model.exists()                                           # the seeded files were removed


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs a user that permissions apply to")
def test_uninstall_says_what_it_could_not_remove(tmp_path, monkeypatch, capsys):
    conf, model = _node(tmp_path, monkeypatch)
    state = tmp_path / "state"
    st = State(state)
    st.holdings["a/b"] = {"save_path": str(model)}
    st.save()
    os.chmod(model, 0o500)                  # can't delete what's inside a read-only folder
    try:
        rc = cli.cmd_uninstall(_args(conf, state))
    finally:
        os.chmod(model, 0o700)
    cap = capsys.readouterr()
    assert rc == 1 and "Not everything was removed" in cap.err and "w.safetensors" in cap.err
    assert "Done." not in cap.out
    assert (model / "w.safetensors").exists() and not state.exists()


def test_uninstall_removes_a_symlinked_model_folder_without_following_it(tmp_path, monkeypatch, capsys):
    conf, model = _node(tmp_path, monkeypatch)
    inside = tmp_path / "seeds" / "linked"
    inside.symlink_to(model, target_is_directory=True)               # a held "folder" that's a link, inside seeds
    state = tmp_path / "state"
    st = State(state)
    st.holdings["x/y"] = {"save_path": str(inside)}
    st.holdings["a/b"] = {"save_path": str(model)}
    st.save()
    assert cli.cmd_uninstall(_args(conf, state)) == 0
    assert not inside.is_symlink() and not model.exists()


def test_uninstall_with_unreadable_state_reports_it(tmp_path, monkeypatch, capsys):
    conf, model = _node(tmp_path, monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    (state / "state.json").write_text("{torn")
    assert cli.cmd_uninstall(_args(conf, state)) == 1
    assert "can't tell which are its own" in capsys.readouterr().err
    assert (model / "w.safetensors").exists()                          # nothing guessed at


# ---------------------------------------------------------------- temp folders


def test_download_removes_its_own_folder_if_the_write_fails(tmp_path, monkeypatch):
    import hashlib
    import tempfile

    from flotilla import netsafe

    data = b"wheel"
    rel = release.Release("9.9.9", "v9.9.9", "", "x", {"w.whl": {"sha256": hashlib.sha256(data).hexdigest(),
                                                              "size": len(data)}})
    monkeypatch.setattr(netsafe, "fetch", lambda *a, **k: SimpleNamespace(body=data))
    made = []
    real = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kw: made.append(real(dir=str(tmp_path), **kw)) or made[-1])
    monkeypatch.setattr(Path, "write_bytes", lambda self, b: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        release.download(rel, "w.whl")
    assert made and not Path(made[0]).exists()


def test_update_removes_the_downloaded_wheel_folder(tmp_path, monkeypatch):
    import tempfile

    from flotilla import containers, service

    from .test_release import _rel

    made = []
    real = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kw: made.append(Path(real(dir=str(tmp_path), **kw))) or made[-1])

    def download(rel, name, into=None):
        (into / name).write_bytes(b"wheel")
        return into / name

    monkeypatch.setattr(release, "latest", lambda: _rel())
    monkeypatch.setattr(release, "download", download)
    monkeypatch.setattr(cli, "_install_kind", lambda: "uv-tool")
    monkeypatch.setattr(cli, "_install_wheel", lambda kind, wheel: wheel.exists())     # the wheel is there to install
    monkeypatch.setattr(service, "installed", lambda: False)
    monkeypatch.setattr(cli.shutil, "which", lambda n: None)
    monkeypatch.setattr(containers, "recover", lambda: [])
    assert cli.cmd_update(argparse.Namespace(check=False, yes=True)) == 0
    assert len(made) == 1 and not made[0].exists()
    # ...also when the install fails
    monkeypatch.setattr(cli, "_install_wheel", lambda kind, wheel: False)
    assert cli.cmd_update(argparse.Namespace(check=False, yes=True)) == 1
    assert len(made) == 2 and not made[1].exists()


def test_editor_release_removes_its_work_folder_but_keeps_a_dry_runs(tmp_path, monkeypatch, capsys):
    import tempfile

    from .test_release import IMG, _dist

    key = SecretKey.generate()
    secret = tmp_path / "editor.key"
    key.save(secret)
    monkeypatch.setattr(release, "builtin_keys", lambda: [key.public])
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/gh")
    made = []
    real = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kw: made.append(Path(real(dir=str(tmp_path), **kw))) or made[-1])
    (tmp_path / "ci").mkdir()

    def run(cmd, capture_output=True, text=True, **kw):
        if cmd[1:3] == ["release", "download"]:
            out = Path(cmd[cmd.index("--dir") + 1])
            for f in _dist(tmp_path / "ci", "0.9.0").iterdir():
                (out / f.name).write_bytes(f.read_bytes())
            (out / "image.txt").write_text(IMG + "\n")
        return subprocess.CompletedProcess(cmd, 0, "Notes" if cmd[2] == "view" else "", "")

    monkeypatch.setattr(subprocess, "run", run)
    args = argparse.Namespace(file="v0.9.0", secret=str(secret), dry_run=False, action="release", config="x")
    assert cli.cmd_editor(args) == 0 and not made[0].exists()
    args.dry_run = True
    assert cli.cmd_editor(args) == 0 and (made[1] / release.MANIFEST).exists()       # the path it printed
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(ValueError("gh failed")))
    with pytest.raises(ValueError):
        cli.cmd_editor(argparse.Namespace(file="v0.9.0", secret=str(secret), dry_run=False, action="release",
                                          config="x"))
    assert not made[2].exists()


def test_a_release_with_two_wheels_is_refused():
    with pytest.raises(release.ReleaseError, match="single wheel"):
        _ = release.Release("1.0.0", "v1.0.0", "", "x", {"a.whl": {}, "b.whl": {}}).wheel
