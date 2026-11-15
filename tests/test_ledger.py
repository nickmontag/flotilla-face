"""The hash ledger: signatures, append-only memory, the trust chain without Hugging Face, folder checks."""

import hashlib
import json
from pathlib import Path

import pytest

from flotilla.candidates import Rejection, TorrentCand, build_candidate
from flotilla.config import Config
from flotilla.feed import neutral_feed
from flotilla.ledger import (BUNDLED_LEDGER, SEEN_FILE, LedgerError, accept_override, add_entry, capture,
                             check_file, check_index_files, folder_extras, load_ledger, match_folder)
from flotilla.signing import SecretKey
from flotilla.sources import HFFile, lfs_pointer_oid

from .fakes import FakeHF, FakePF, FakeRepo, make_repo


def quiet(*_):
    pass


def make_ledger(tmp_path: Path, repos, sk, hf=None, pf=None, name="pub") -> Path:
    """A signed ledger directory with one entry per repo, captured from fake HF / PF."""
    index = tmp_path / name / "index.json"
    hf = hf or FakeHF(repos)
    pf = pf or FakePF(repos)
    for r in repos:
        doc, note = capture(r.repo, r.revision, hf, pf)
        assert doc, note
        add_entry(index, doc)
    sign(index, sk)
    return index


def sign(index: Path, sk) -> None:
    Path(str(index) + ".minisig").write_text(sk.sign(index.read_bytes()))


def load(state: Path, sk, bundled=None, url=None, logs=None):
    return load_ledger([sk.public], state, (), url=url, bundled=bundled,
                       log=(logs.append if logs is not None else quiet))


def cand(repo, pf, hf, ledger):
    return build_candidate(repo.repo, None, neutral_feed(), None, pf, hf, Config(), ledger=ledger)


@pytest.fixture
def repo():
    return FakeRepo("lost/model", make_repo(n_shards=3, shard_size=50_000, seed=3))


# ---------------------------------------------------------------- loading


def test_signed_ledger_loads_and_wrong_key_is_ignored(tmp_path, repo):
    sk = SecretKey.generate()
    index = make_ledger(tmp_path, [repo], sk)
    led = load(tmp_path / "state", sk, bundled=index)
    e = led.get(repo.repo, repo.revision)
    assert e and e.source == "huggingface" and set(e.files) == set(repo.files)
    big = next(p for p in repo.files if p.endswith(".safetensors"))
    assert e.files[big].sha256 == hashlib.sha256(repo.files[big]).hexdigest()
    assert e.files["config.json"].git_oid and not e.files["config.json"].sha256

    logs = []
    other = load_ledger([SecretKey.generate().public], tmp_path / "s2", (), url=None, bundled=index, log=logs.append)
    assert not other.entries and any("unusable" in l for l in logs)


def test_unsigned_or_tampered_entries_are_not_used(tmp_path, repo):
    sk = SecretKey.generate()
    index = make_ledger(tmp_path, [repo], sk)
    entry_file = next((index.parent / "entries").glob("*.json"))
    entry_file.write_bytes(entry_file.read_bytes().replace(b'"size": ', b'"size":  '))
    logs = []
    assert not load(tmp_path / "state", sk, bundled=index, logs=logs).entries
    assert any("doesn't match its digest" in l for l in logs)
    with pytest.raises(LedgerError):
        check_index_files(index)

    Path(str(index) + ".minisig").unlink()
    logs = []
    assert not load(tmp_path / "s2", sk, bundled=index, logs=logs).entries
    assert any("isn't signed" in l for l in logs)


def test_first_digest_wins_forever_and_accept_overrides(tmp_path, repo):
    sk = SecretKey.generate()
    good = make_ledger(tmp_path, [repo], sk, name="good")
    state = tmp_path / "state"
    first = load(state, sk, bundled=good)
    assert first.get(repo.repo, repo.revision)

    # The same key later signs a rewritten entry (a stolen key, say), served as the published copy.
    evil_repo = FakeRepo(repo.repo, {p: (b"x" * len(d) if p.endswith(".safetensors") else d)
                                     for p, d in repo.files.items()})
    evil_repo.revision = repo.revision
    evil = make_ledger(tmp_path, [evil_repo], sk, name="evil")
    logs = []
    led = load(state, sk, url=f"file://{evil}", logs=logs)     # the honest copy isn't even offered
    kept = led.get(repo.repo, repo.revision)
    assert kept and kept.digest == first.get(repo.repo, repo.revision).digest   # from this node's own cache
    assert led.conflicts and any("HASH LEDGER CONFLICT" in l for l in logs)

    # A fresh install reads the bundled copy before the published one: the bundled one wins there too.
    led2 = load(tmp_path / "fresh", sk, bundled=good, url=f"file://{evil}")
    assert led2.get(repo.repo, repo.revision).digest == kept.digest and led2.conflicts

    # Deliberately accepting the other entry switches to it.
    new_digest = led.conflicts[0]["refused"]
    accept_override(state, f"{repo.repo}@{repo.revision}", new_digest)
    led3 = load(state, sk, url=f"file://{evil}")
    assert led3.get(repo.repo, repo.revision).digest == new_digest and not led3.conflicts
    assert json.loads((state / SEEN_FILE).read_text())[f"{repo.repo}@{repo.revision}"] == new_digest


def test_published_copy_adds_entries_and_is_cached(tmp_path, repo):
    sk = SecretKey.generate()
    other = FakeRepo("other/model", make_repo(n_shards=2, shard_size=10_000, seed=5))
    bundled = make_ledger(tmp_path, [repo], sk, name="bundled")
    published = make_ledger(tmp_path, [repo, other], sk, name="published")
    state = tmp_path / "state"
    led = load(state, sk, bundled=bundled, url=f"file://{published}")
    assert led.get(other.repo, other.revision) and led.get(repo.repo, repo.revision)
    # Published copy gone: the cached copy and cached entries still serve.
    led2 = load(state, sk, bundled=bundled, url=f"file://{tmp_path}/gone/index.json")
    assert led2.get(other.repo, other.revision)


def test_capture_never_rewrites_an_entry(tmp_path, repo):
    index = tmp_path / "l" / "index.json"
    doc, _ = capture(repo.repo, repo.revision, FakeHF([repo]), FakePF([repo]))
    add_entry(index, doc)
    with pytest.raises(LedgerError, match="never rewritten"):
        add_entry(index, doc)
    assert json.loads(index.read_text())["version"] == 1


def test_capture_from_pirateface_when_huggingface_is_gone(repo):
    doc, note = capture(repo.repo, repo.revision, FakeHF([repo], gone={repo.repo}), FakePF([repo]))
    assert doc["source"] == "pirateface" and len(doc["files"]) == len(repo.files)
    assert all(len(f["sha256"]) == 64 for f in doc["files"])
    doc, why = capture(repo.repo, "f" * 40, FakeHF([repo], gone={repo.repo}), FakePF([repo]))
    assert doc is None and "no torrent at this revision" in why


def test_capture_on_gated_repo_uses_pirateface_hash_only_if_it_matches_the_pointer(repo):
    hf = FakeHF([repo])
    tree = repo.tree()
    big = sorted(p for p in tree if p.endswith(".safetensors"))
    for p in big:
        real = tree[p].sha256
        tree[p] = HFFile(tree[p].size, None, None, lfs_pointer_oid(real, tree[p].size))
    hf.tree_overrides[repo.repo] = tree
    pf = FakePF([repo])
    listing = repo.listing()
    listing["files"] = [f if f["path"] != big[0] else {"path": big[0], "sha256": "0" * 64} for f in listing["files"]]
    pf.overrides[repo.repo] = listing
    doc, _ = capture(repo.repo, repo.revision, hf, pf)
    rec = {f["path"]: f for f in doc["files"]}
    assert "sha256" not in rec[big[0]] and rec[big[0]]["lfs_pointer_oid"]        # PF's wrong hash refused
    assert rec[big[1]]["sha256"] == hashlib.sha256(repo.files[big[1]]).hexdigest()


# ---------------------------------------------------------------- trust chain


def test_hf_gone_ledger_hashes_are_expected_and_pf_disagreement_is_noted(tmp_path, repo):
    sk = SecretKey.generate()
    led = load(tmp_path / "state", sk, bundled=make_ledger(tmp_path, [repo], sk))
    pf = FakePF([repo])
    listing = repo.listing()
    shard = "model-00002-of-00003.safetensors"
    listing["files"] = [f if f["path"] != shard else {"path": shard, "sha256": "0" * 64} for f in listing["files"]]
    pf.overrides[repo.repo] = listing
    c = cand(repo, pf, FakeHF([repo], gone={repo.repo}), led)
    assert isinstance(c, TorrentCand), c
    assert c.verify_source.startswith("ledger")
    by = {f.repo_path: f for f in c.files}
    assert by[shard].sha256 == hashlib.sha256(repo.files[shard]).hexdigest()
    assert by["config.json"].git_oid
    assert c.manifest_mismatches == [{"path": shard, "pirateface": "0" * 64, "ledger": by[shard].sha256}]

    # Without the ledger the same listing would have been trusted.
    c0 = cand(repo, pf, FakeHF([repo], gone={repo.repo}), None)
    assert c0.verify_source == "pirateface-only"
    assert {f.repo_path: f for f in c0.files}[shard].sha256 == "0" * 64


def test_torrent_with_files_the_ledger_never_recorded_is_refused(tmp_path, repo):
    sk = SecretKey.generate()
    led = load(tmp_path / "state", sk, bundled=make_ledger(tmp_path, [repo], sk))
    bigger = FakeRepo(repo.repo, {**repo.files, "extra.safetensors": b"payload" * 100})
    bigger.revision = repo.revision
    r = cand(bigger, FakePF([bigger]), FakeHF([bigger], gone={repo.repo}), led)
    assert isinstance(r, Rejection) and "extra.safetensors" in r.reason and "ledger" in r.reason


def test_hf_gone_and_listing_at_an_unrecorded_revision_is_refused(tmp_path, repo):
    sk = SecretKey.generate()
    led = load(tmp_path / "state", sk, bundled=make_ledger(tmp_path, [repo], sk))
    moved = FakeRepo(repo.repo, repo.files)
    moved.revision = "e" * 40
    r = cand(moved, FakePF([moved]), FakeHF([moved], gone={repo.repo}), led)
    assert isinstance(r, Rejection) and "records it at" in r.reason


def test_hf_and_ledger_disagreeing_is_refused(tmp_path, repo):
    sk = SecretKey.generate()
    led = load(tmp_path / "state", sk, bundled=make_ledger(tmp_path, [repo], sk))
    hf = FakeHF([repo])
    tree = repo.tree()
    shard = "model-00001-of-00003.safetensors"
    tree[shard] = HFFile(tree[shard].size, "1" * 64, None)
    hf.tree_overrides[repo.repo] = tree
    r = cand(repo, FakePF([repo]), hf, led)
    assert isinstance(r, Rejection) and "disagree" in r.reason and shard in r.reason
    # HF agreeing with the ledger: business as usual.
    c = cand(repo, FakePF([repo]), FakeHF([repo]), led)
    assert isinstance(c, TorrentCand) and c.verify_source == "huggingface"


def test_gated_hf_takes_the_ledger_hash_that_matches_its_pointer(tmp_path, repo):
    sk = SecretKey.generate()
    led = load(tmp_path / "state", sk, bundled=make_ledger(tmp_path, [repo], sk))
    hf = FakeHF([repo])
    tree = repo.tree()
    for p, f in list(tree.items()):
        if f.sha256:
            tree[p] = HFFile(f.size, None, None, lfs_pointer_oid(f.sha256, f.size))
    hf.tree_overrides[repo.repo] = tree
    pf = FakePF([repo])
    listing = repo.listing()
    listing["files"] = []          # Pirate Face offers no hashes at all
    pf.overrides[repo.repo] = listing
    c = cand(repo, pf, hf, led)
    assert isinstance(c, TorrentCand), c
    shard = "model-00001-of-00003.safetensors"
    assert {f.repo_path: f for f in c.files}[shard].sha256 == hashlib.sha256(repo.files[shard]).hexdigest()


# ---------------------------------------------------------------- downloaders


def test_verify_folder_good_bad_extra(tmp_path, repo):
    sk = SecretKey.generate()
    led = load(tmp_path / "state", sk, bundled=make_ledger(tmp_path, [repo], sk))
    dl = tmp_path / "downloads"
    repo.write(dl)                              # dl/lost__model/...
    e, root = match_folder(dl, led)
    assert e.repo == repo.repo and root == dl / repo.name
    assert all(check_file(root / p, lf)[0] for p, lf in e.files.items())
    assert folder_extras(root, e) == []

    shard = "model-00002-of-00003.safetensors"
    data = bytearray((root / shard).read_bytes())
    data[100] ^= 1
    (root / shard).write_bytes(bytes(data))
    ok, why = check_file(root / shard, e.files[shard])
    assert not ok and "SHA-256" in why
    (root / "sneaky.safetensors").write_bytes(b"x")
    (root / ".DS_Store").write_bytes(b"x")
    (root / "model-00003-of-00003.safetensors.parts").write_bytes(b"x")
    assert folder_extras(root, e) == ["sneaky.safetensors"]

    with pytest.raises(LedgerError, match="no entry"):
        match_folder(dl, led, repo="someone/else")


def test_cli_verify_folder(tmp_path, repo, capsys, monkeypatch):
    from flotilla import cli

    sk = SecretKey.generate()
    index = make_ledger(tmp_path, [repo], sk)
    monkeypatch.setattr(cli, "_keys", lambda cfg: [sk.public])
    monkeypatch.setattr("flotilla.ledger.BUNDLED_LEDGER", index)
    monkeypatch.setattr(cli, "load_ledger", lambda keys, state, hosts, url=None, log=None:
                        load_ledger(keys, state, hosts, url=None, bundled=index, log=quiet))
    dl = repo.write(tmp_path / "dl") / repo.name
    args = ["--config", str(tmp_path / "none.toml"), "--state", str(tmp_path / "state"), "verify", str(dl), "--offline"]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert repo.repo in out and f"{len(repo.files)} good, 0 bad" in out
    (dl / "config.json").write_bytes(b"{}")
    assert cli.main(args) == 1
    assert "BAD   config.json" in capsys.readouterr().out


def test_bundled_ledger_if_signed_verifies_with_the_project_key(tmp_path):
    from flotilla.cli import _keys

    if not Path(str(BUNDLED_LEDGER) + ".minisig").exists():
        pytest.skip("bundled ledger not signed yet")
    check_index_files(BUNDLED_LEDGER)
    led = load_ledger(_keys(Config()), tmp_path, (), url=None, log=quiet)
    assert led.entries


# ---------------------------------------------------------------- editors: signing the list keeps the ledger current

FAKE_OTS = """#!/usr/bin/env python3
import sys
from pathlib import Path
PENDING, BITCOIN = bytes.fromhex("83dfe30d2ef90c8e"), bytes.fromhex("0588960d73d71901")
cmd, files = sys.argv[1], sys.argv[2:]
for f in files:
    if cmd == "stamp":
        Path(f + ".ots").write_bytes(b"proof" + PENDING)
    elif cmd == "upgrade":
        if Path(f).with_name("CONFIRMED").exists():
            Path(f + ".bak").write_bytes(Path(f).read_bytes())
            Path(f).write_bytes(b"proof" + BITCOIN)
        else:
            print("Failed! Timestamp not complete")
            sys.exit(1)
"""


def test_signing_the_list_records_signs_and_stamps(tmp_path, monkeypatch, capsys):
    from flotilla import cli
    from flotilla.feed import parse_feed
    from flotilla.signing import verify

    old = FakeRepo("old/model", make_repo(n_shards=2, shard_size=5_000, seed=1))
    new = FakeRepo("new/model", make_repo(n_shards=2, shard_size=5_000, seed=2))
    sk = SecretKey.generate()
    index = make_ledger(tmp_path, [old], sk, name="ledger")
    feed = parse_feed(json.dumps({
        "schema": 1, "version": 1, "issued": "2026-11-01T00:00:00+00:00", "expires": "2099-01-01T00:00:00+00:00",
        "models": [{"repo": r.repo, "revision": r.revision, "scores": {}} for r in (old, new)]
        + [{"repo": "unpinned/model", "scores": {}}],
    }).encode())
    hf, pf = FakeHF([old, new]), FakePF([old, new])

    # No ots client: records and signs, says nothing was timestamped.
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.update_ledger(feed, index, sk, hf, pf) == 0
    out = capsys.readouterr().out
    assert "added  new/model" in out and "nothing was timestamped" in out
    verify(index.read_bytes(), Path(str(index) + ".minisig").read_text(), [sk.public])
    assert load(tmp_path / "state", sk, bundled=index).get(new.repo, new.revision)

    # With ots: stamps both entries, then later completes them once Bitcoin confirms.
    ots = tmp_path / "bin" / "ots"
    ots.parent.mkdir()
    ots.write_text(FAKE_OTS)
    ots.chmod(0o755)
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(ots))
    sig_before = Path(str(index) + ".minisig").read_text()
    assert cli.update_ledger(feed, index, sk, hf, pf) == 0
    out = capsys.readouterr().out
    assert "signature is current" in out and "Stamped 2 new entries" in out
    assert Path(str(index) + ".minisig").read_text() == sig_before          # nothing new: not re-signed
    proofs = sorted((index.parent / "entries").glob("*.ots"))
    assert len(proofs) == 2

    assert cli.update_ledger(feed, index, sk, hf, pf) == 0
    assert "2 proof(s) still waiting" in capsys.readouterr().out
    (index.parent / "entries" / "CONFIRMED").write_text("")
    assert cli.update_ledger(feed, index, sk, hf, pf) == 0
    assert "Completed 2 proof(s)" in capsys.readouterr().out
    assert not list((index.parent / "entries").glob("*.bak"))
    assert cli.update_ledger(feed, index, sk, hf, pf) == 0                  # complete proofs are left alone
    out = capsys.readouterr().out
    assert "Completed" not in out and "waiting" not in out
    assert led_has_stamps(tmp_path, sk, index)


def led_has_stamps(tmp_path, sk, index) -> bool:
    led = load(tmp_path / "state-stamps", sk, bundled=index)
    return all(e.stamp for e in led.entries.values())


def test_sign_command_runs_the_ledger_step_unless_told_not_to(tmp_path, monkeypatch, capsys):
    from flotilla import cli

    sk = SecretKey.generate()
    sk.save(tmp_path / "k.key")
    feed_path = tmp_path / "data" / "priorities.json"
    feed_path.parent.mkdir()
    feed_path.write_text(json.dumps({"schema": 1, "version": 1, "issued": "2026-11-01T00:00:00+00:00",
                                     "expires": "2099-01-01T00:00:00+00:00", "models": []}))
    calls = []
    monkeypatch.setattr(cli, "update_ledger", lambda feed, index, key: calls.append(index) or 0)
    base = ["editor", "sign", str(feed_path), "--secret", str(tmp_path / "k.key")]
    assert cli.main(base) == 0 and calls == []          # no ledger next to this list: just signs it
    (feed_path.parent / "ledger").mkdir()
    (feed_path.parent / "ledger" / "index.json").write_text("{}")
    assert cli.main(base + ["--no-ledger"]) == 0 and calls == []
    assert cli.main(base) == 0 and calls == [feed_path.parent / "ledger" / "index.json"]
