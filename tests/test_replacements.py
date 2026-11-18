"""Replaced torrents, the magnet index fallback, and rescue requests.

* A torrent Pirate Face replaced is followed: for a pinned model only with Hugging Face or the hash
  ledger vouching, and files already held move over instead of being downloaded again.
* While Pirate Face can't be reached, listings come from the node's copy of its magnet index and
  .torrent files from the swarm; the usual checks still apply.
* `flotilla add` and the dashboard can ask Pirate Face to add a model it has no torrent for.
"""

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

import pytest

lt = pytest.importorskip("libtorrent")

from flotilla import cli, magnetindex, netsafe, sources
from flotilla.candidates import Rejection, TorrentCand, build_candidate
from flotilla.config import Config, State
from flotilla.engine import Engine
from flotilla.feed import FeedModel, neutral_feed
from flotilla.magnetindex import Entry, check_build, entry_of, metadata_from_swarm, parse_shards
from flotilla.netsafe import FetchError, UnsafeURL
from flotilla.torrentmeta import make_torrent, parse_torrent

from .fakes import FakeHF, FakePF, FakeRepo, make_repo

TR = "udp%3A%2F%2Ftracker.pirateface.co%3A6969%2Fannounce"
REV = "a" * 40


def build(repo, pf, hf=None, entry=None, index=None):
    return build_candidate(repo.repo, None, neutral_feed(), entry, pf, hf or FakeHF([repo]), Config(), index=index)


def twin(repo: FakeRepo, piece_length=32768) -> FakeRepo:
    """The same model packed again: the same files in a torrent with another infohash."""
    r = FakeRepo(repo.repo, repo.files, piece_length=piece_length)
    r.revision = repo.revision
    return r


# ------------------------------------------------------------------ replaced pins


def test_a_replaced_pin_is_followed_only_with_independent_hashes():
    old = FakeRepo("a/b", make_repo(2))
    new = twin(old)
    entry = FeedModel("a/b", {"capability": 90}, infohash=old.infohash)
    pf = FakePF([new])
    assert "does not match the signed pin" in build(new, pf, entry=entry).reason
    pf.replaced[old.infohash] = new.infohash
    c = build(new, pf, entry=entry)
    assert isinstance(c, TorrentCand) and c.pinned and c.replaces == old.infohash and c.verify_source == "huggingface"
    # Hugging Face gone and no ledger record: the pin was the only anchor, so it isn't passed on.
    gone = build(new, pf, hf=FakeHF([new], gone={"a/b"}), entry=entry)
    assert isinstance(gone, Rejection) and "nothing independent vouches" in gone.reason
    # A replacement chain that leads elsewhere is still a mismatch.
    pf.replaced[old.infohash] = "c" * 40
    assert "does not match the signed pin" in build(new, pf, entry=entry).reason


def test_replacement_follows_pirate_faces_chain(monkeypatch):
    docs = {"1" * 40: {"supersededBy": "2" * 40}, "2" * 40: {"supersededBy": "3" * 40}, "3" * 40: {"supersededBy": None}}
    pf = sources.PirateFace(("pirateface.co",))
    monkeypatch.setattr(pf, "torrent_status", lambda ih: docs.get(ih))
    assert pf.replacement("1" * 40) == "3" * 40
    assert pf.replacement("3" * 40) is None
    docs["3" * 40] = {"supersededBy": "1" * 40}          # a loop ends where it started
    assert pf.replacement("1" * 40) == "3" * 40


# ------------------------------------------------------------------ moving held files over


def holding_engine(tmp_path, repo: FakeRepo, logs):
    cfg = Config()
    cfg.folder, cfg.listen_port, cfg.whole_model_under_gb = tmp_path / "seeds", 0, 100
    cfg.path = tmp_path / "cfg" / "config.toml"
    eng = Engine(cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False)
    c = build(repo, FakePF([repo]))
    save = eng.save_path(repo.repo, repo.infohash)
    repo.write(save)
    eng.torrent_dir.mkdir(parents=True, exist_ok=True)
    (eng.torrent_dir / f"{repo.infohash}.torrent").write_bytes(repo.raw)
    eng.state.holdings[repo.infohash] = {
        "repo": repo.repo, "quant": c.quant, "revision": repo.revision, "save_path": str(save),
        "files": {str(f.index): {"path": f.repo_path, "tpath": c.meta.files[f.index].path, "size": f.size,
                                 "sha256": f.sha256, "git_oid": f.git_oid, "status": "verified", "since": 1000.0,
                                 "prio": 4} for f in c.files if f.kind != "skip"}}
    eng.state.data["ledger"][repo.infohash] = {"repo": repo.repo, "seed_hours": 5.0, "gb_hours": 1.0, "points_est": 2.0}
    eng._add(repo.infohash, [4 if str(i) in eng.state.holdings[repo.infohash]["files"] else 0
                             for i in range(len(c.meta.files))])
    return eng


def drive(eng, until, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        eng.ses.wait_for_alert(100)
        eng.tick()
        if until():
            return True
    return False


def test_files_of_a_replaced_torrent_move_over_instead_of_downloading_again(tmp_path):
    logs: list[str] = []
    files = make_repo(3, shard_size=40_000)
    files.pop("modeling_test.py")      # skipped: its bytes would share pieces with the files around it
    old = FakeRepo("a/b", files)
    eng = holding_engine(tmp_path, old, logs)
    new = twin(old)
    c = build(new, FakePF([new]))
    eng.mark_held([c])
    eng._plan_and_apply([c], [])
    hold = eng.state.holdings
    assert old.infohash not in hold and new.infohash in hold, logs
    assert not Path(eng.save_path(old.repo, old.infohash)).exists()
    assert any("Pirate Face replaced its torrent" in m and "moved" in m for m in logs), logs
    assert eng.state.data["ledger"][new.infohash]["seed_hours"] == 5.0
    # No peer is connected: everything there was checked from disk, then against Hugging Face.
    assert drive(eng, eng.all_done), logs
    files = hold[new.infohash]["files"]
    assert files and all(e["status"] == "verified" and e["since"] == 1000.0 for e in files.values()), files
    base = Path(hold[new.infohash]["save_path"])
    for e in files.values():
        assert (base / e["tpath"]).read_bytes() == new.files[e["path"]]
    eng.shutdown()


def test_a_new_revision_moves_only_the_files_that_didnt_change(tmp_path):
    logs: list[str] = []
    files = make_repo(3, shard_size=40_000)
    old = FakeRepo("a/b", files)
    eng = holding_engine(tmp_path, old, logs)
    changed = dict(files)
    changed["model-00002-of-00003.safetensors"] = b"retrained" * 5000
    new = FakeRepo("a/b", changed)
    new.revision = "b" * 40
    c = build(new, FakePF([new]))
    eng._plan_and_apply([c], [])
    hold = eng.state.holdings[new.infohash]
    base = Path(hold["save_path"])
    on_disk = {e["path"] for e in hold["files"].values() if (base / e["tpath"]).exists()}
    assert "model-00001-of-00003.safetensors" in on_disk
    assert "model-00002-of-00003.safetensors" not in on_disk          # changed: downloaded again
    assert any("moved 5 verified file(s)" in m for m in logs), logs
    assert any("listed at a new revision" in m for m in logs), logs
    eng.shutdown()


# ------------------------------------------------------------------ the magnet index


def magnet(repo: str, ih: str, rev: str = REV, ws_host="pirateface.co", tracker=TR) -> str:
    ws = f"https%3A%2F%2F{ws_host}%2Fapi%2Fws%2F{repo.replace('/', '%2F')}%2F{rev}%2F{repo.split('/')[1]}%2F"
    return f"magnet:?xt=urn:btih:{ih}&tr={tracker}&ws={ws}"


def test_shards_are_read_in_order_with_removals_and_fail_closed(tmp_path):
    d = tmp_path / "shards"
    d.mkdir()
    (d / "2026-11-09.txt").write_text("\n".join([
        magnet("a/one", "1" * 40), magnet("a/two", "2" * 40), magnet("a/gone", "3" * 40),
        magnet("a/evil", "4" * 40, ws_host="evil.example"), magnet("a/evil2", "5" * 40, tracker="udp%3A%2F%2Fx.example%3A1"),
        "not a magnet"]) + "\n")
    (d / "removed-2026-11-16.txt").write_text("3" * 40 + "\n" + "1" * 40 + "\n")
    (d / "2026-11-16.txt").write_text(magnet("a/one", "6" * 40, rev="b" * 40) + "\n")
    found = parse_shards(d)
    # a/one's old torrent is removed in the build that lists its new one: the new one stays.
    assert set(found) == {"a/one", "a/two"} and found["a/one"].infohash == "6" * 40
    assert found["a/two"] == Entry(magnet("a/two", "2" * 40), "2" * 40, REV, "2026-11-09")
    (d / "removed-2026-11-16.txt").write_text("3" * 40 + "\n")
    found = parse_shards(d)
    assert found["a/one"].infohash == "6" * 40 and found["a/one"].revision == "b" * 40 and "a/gone" not in found
    (d / "removed-2026-11-16.txt").write_text("9" + "3" * 40 + "\n")     # one stray character
    assert parse_shards(d) is None
    assert entry_of(magnet("a/x", "7" * 40, rev="short"), "d") is None


def index_torrent(tmp_path, shards: dict[str, str]) -> tuple[Path, bytes]:
    root = tmp_path / "src" / "magnet-index"
    (root / "shards").mkdir(parents=True)
    (root / "LICENSE").write_text("CC0\n")
    for name, text in shards.items():
        (root / "shards" / name).write_text(text)
    files = sorted((p.relative_to(root).as_posix(), p.read_bytes()) for p in root.rglob("*") if p.is_file())
    raw = make_torrent(files, "magnet-index")
    return root, raw


def test_check_build_refuses_anything_but_shards(tmp_path):
    _, raw = index_torrent(tmp_path, {"2026-11-09.txt": "x\n"})
    check_build(parse_torrent(raw))
    with pytest.raises(magnetindex.IndexError_, match="other than"):
        check_build(parse_torrent(make_torrent([("shards/2026-11-09.txt", b"x"), ("run.sh", b"x")], "magnet-index")))
    with pytest.raises(magnetindex.IndexError_, match="not the magnet index"):
        check_build(parse_torrent(make_torrent([("LICENSE", b"x")], "something")))


class IndexPF(FakePF):
    def __init__(self, repos, index_raw=None):
        super().__init__(repos)
        self.index_raw = index_raw
        self.down = False

    def torrent(self, repo, quant=None):
        if self.down:
            raise FetchError("https://pirateface.co/api/torrents", None, "connection refused")
        if repo == magnetindex.INDEX_REPO:
            return {"infohash": parse_torrent(self.index_raw).infohash_v1}
        return super().torrent(repo, quant)

    def torrent_file(self, ih):
        if self.down:
            raise FetchError("https://pirateface.co/api/torrents", None, "connection refused")
        if self.index_raw and ih == parse_torrent(self.index_raw).infohash_v1:
            return self.index_raw
        return super().torrent_file(ih)


def test_the_node_keeps_the_index_and_lists_from_it_while_pirate_face_is_down(tmp_path):
    logs: list[str] = []
    repo = FakeRepo("a/b", make_repo(2))
    src, raw = index_torrent(tmp_path, {"2026-11-09.txt": magnet(repo.repo, repo.infohash, repo.revision) + "\n"})
    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    eng = Engine(cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False)
    pf = IndexPF([repo], raw)
    eng.index.sync(pf)
    shutil.copytree(src, eng.index.files / "magnet-index")      # as if the swarm had sent it
    assert drive(eng, lambda: eng.index.entries), logs
    assert any("keeping and seeding build 2026-11-09 (1 models)" in m for m in logs), logs
    assert eng.status_doc()["magnet_index"] == {"enabled": True, "build": "2026-11-09", "models": 1, "seeding": True}
    eng.index.sync(pf)                                          # at most daily
    assert eng.index.rec["synced"] > 0

    # Pirate Face goes down: the listing comes from the copy, the .torrent from the swarm.
    pf.down = True
    asked = []
    eng.index.torrent = lambda m: asked.append(m) or repo.raw
    c = build_candidate(repo.repo, None, neutral_feed(), None, pf, FakeHF([repo]), cfg, index=eng.index)
    assert isinstance(c, TorrentCand), c
    assert c.index_build == "2026-11-09" and c.infohash == repo.infohash and c.verify_source == "huggingface"
    assert asked and pf.remembered[repo.infohash] == repo.raw
    # A pin the index disagrees with can't be checked against Pirate Face: hold on, try later.
    entry = FeedModel("a/b", {"capability": 90}, infohash="f" * 40)
    r = build_candidate(repo.repo, None, neutral_feed(), entry, pf, FakeHF([repo]), cfg, index=eng.index)
    assert isinstance(r, Rejection) and r.transient
    # A model the copy doesn't list: still just "Pirate Face is down".
    other = FakeRepo("c/d", make_repo(1))
    r = build_candidate(other.repo, None, neutral_feed(), None, pf, FakeHF([other]), cfg, index=eng.index)
    assert isinstance(r, Rejection) and r.transient and "lookup failed" in r.reason

    # After a restart the copy is there before any sync.
    eng.shutdown()
    eng2 = Engine(cfg, State(tmp_path / "state"), log=logs.append, listen_host="127.0.0.1", dht=False)
    assert eng2.index.listing("a/b")["infohash"] == repo.infohash
    eng2.cfg.keep_magnet_index = False
    assert eng2.index.listing("a/b") is None
    eng2.shutdown()


def test_metadata_comes_from_the_swarm_checked_against_the_infohash(tmp_path):
    repo = FakeRepo("a/b", make_repo(2))
    src = repo.write(tmp_path / "A")
    ses = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                      "enable_upnp": False, "enable_natpmp": False, "allow_multiple_connections_per_ip": True})
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(lt.bdecode(repo.raw))
    atp.save_path = str(src)
    atp.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
    ses.add_torrent(atp)
    settings = {"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                "enable_upnp": False, "enable_natpmp": False, "allow_multiple_connections_per_ip": True}
    raw = metadata_from_swarm(repo.magnet(), settings, tmp_path / "scratch",
                              connect=[("127.0.0.1", ses.listen_port())], seconds=20)
    meta = parse_torrent(raw)
    assert meta.infohash_v1 == repo.infohash and meta.trackers == ["udp://tracker.pirateface.co:6969/announce"]
    assert not (tmp_path / "scratch").exists()
    with pytest.raises(magnetindex.Unavailable):
        metadata_from_swarm(FakeRepo("c/d", make_repo(1)).magnet(), settings, tmp_path / "s2", seconds=1)


# ------------------------------------------------------------------ rescue requests


def test_post_json_keeps_to_the_allowlist():
    with pytest.raises(UnsafeURL):
        netsafe.post_json("https://evil.example/api/rescue", {"repoId": "a/b"})
    with pytest.raises(UnsafeURL):
        netsafe.post_json("http://pirateface.co/api/rescue", {"repoId": "a/b"})


def test_request_rescue_sends_only_the_name(monkeypatch):
    sent = []

    def post(url, body, **kw):
        sent.append((url, body))
        return netsafe.Response(url, 200, {}, b'{"status": "queued"}')

    monkeypatch.setattr(netsafe, "post_json", post)
    assert sources.PirateFace(("pirateface.co",)).request_rescue("a/b") == {"status": "queued"}
    assert sent == [("https://pirateface.co/api/rescue", {"repoId": "a/b"})]
    with pytest.raises(ValueError):
        sources.PirateFace(("pirateface.co",)).request_rescue("../x")


def test_flotilla_add_offers_a_rescue_request(tmp_path, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr(sources.PirateFace, "torrent", lambda self, repo, quant=None: None)
    monkeypatch.setattr(sources.PirateFace, "request_rescue", lambda self, repo: asked.append(repo) or {"status": "queued"})
    run = ["--config", str(tmp_path / "config.toml"), "--state", str(tmp_path / "state"), "add"]
    assert cli.main([*run, "a/b"]) == 0                       # no terminal: never sends without being asked
    assert not asked and "flotilla add --rescue a/b" in capsys.readouterr().out
    assert cli.main([*run, "--rescue", "a/b"]) == 0
    assert asked == ["a/b"] and "Asked Pirate Face to add a/b (queued)" in capsys.readouterr().out


def test_dashboard_rescue_asks_for_listed_models_only(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(sources.PirateFace, "request_rescue", lambda self, repo: asked.append(repo) or {})
    cfg = Config()
    cfg.folder, cfg.listen_port, cfg.local_feed = tmp_path / "seeds", 0, tmp_path / "my-priorities.json"
    eng = Engine(cfg, State(tmp_path / "state"), log=lambda m: None, listen_host="127.0.0.1", dht=False)
    from flotilla.feed import add_pick

    add_pick(cfg.local_feed, "a/b", 100.0)
    with pytest.raises(ValueError, match="isn't on your list"):
        eng.submit("rescue", {"repo": "x/y"})
    with pytest.raises(ValueError):
        eng.submit("rescue", {"repo": "../etc"})
    assert eng.submit("rescue", {"repo": "a/b"}) == "asking"
    end = time.monotonic() + 5
    while time.monotonic() < end and "a/b" not in eng.state.data.get("rescue_asked", {}):
        time.sleep(0.05)
    assert asked == ["a/b"] and eng.state.data["rescue_asked"]["a/b"] > 0
    eng.shutdown()
