"""A Hugging Face account for gated models (flotilla.hfauth): a read-only token, kept in its own
file, checked with Hugging Face, and sent only for models Hugging Face won't serve without one."""

import argparse
import json
import stat
import time
import urllib.error
import urllib.parse

import pytest

lt = pytest.importorskip("libtorrent")

from flotilla import cli, hfauth, hfpieces, netsafe
from flotilla.hfauth import Account, Connection, TokenError
from flotilla.hfpieces import GATED_RECHECK

from .test_hfpieces import (
    PIECE,
    FakeHF,
    Hdrs,
    Resp,
    held_engine,
    make_stuck,
    pf_torrent,
)

TOKEN = "hf_" + "a" * 34
OTHER = "hf_" + "b" * 34


@pytest.fixture(autouse=True)
def no_env_token(monkeypatch):
    monkeypatch.delenv("FLOTILLA_HF_TOKEN_FILE", raising=False)
    yield
    netsafe.pin("")


def whoami(role="read", fine=None, type_="user"):
    tok = {"displayName": "flotilla", "role": role}
    if fine is not None:
        tok["fineGrained"] = fine
    return {"type": type_, "name": "someone", "email": "x@example.com", "auth": {"type": "access_token", "accessToken": tok}}


# ---------------------------------------------------------------- the token file


def test_token_file_is_private_and_holds_only_a_token(tmp_path):
    path = tmp_path / "cfg" / "hf-token"
    hfauth.save_token(path, "  " + TOKEN + "\n")
    assert path.read_text() == TOKEN + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and not hfauth.readable_by_others(path)
    assert hfauth.read_token(path) == TOKEN
    for junk in ("not-a-token", TOKEN + "\r\nX-Evil: 1", "hf_short", ""):
        with pytest.raises(TokenError):
            hfauth.clean_token(junk)
    path.write_text("garbage")
    with pytest.raises(TokenError):
        hfauth.read_token(path)
    assert hfauth.remove_token(path) and not path.exists() and not hfauth.remove_token(path)
    assert hfauth.read_token(path) is None and hfauth.read_token(None) is None


def test_token_lives_next_to_the_config_unless_a_file_is_named(tmp_path, monkeypatch):
    assert hfauth.token_path(tmp_path / "config.toml") == tmp_path / "hf-token"
    assert hfauth.token_path(None) is None                          # no config file: no token either
    monkeypatch.setenv("FLOTILLA_HF_TOKEN_FILE", str(tmp_path / "secret"))
    assert hfauth.token_path(tmp_path / "config.toml") == tmp_path / "secret"


# ---------------------------------------------------------------- read-only tokens only


@pytest.mark.parametrize("doc, problem", [
    (whoami("read"), None),
    (whoami("fineGrained", {"global": ["discussion.read"], "scoped": [
        {"entity": {"type": "model", "name": "org/m"}, "permissions": ["repo.content.read"]}]}), None),
    (whoami("write"), "Write token"),
    (whoami("fineGrained", {"global": ["inference.serverless.write"], "scoped": []}), "inference.serverless.write"),
    (whoami("fineGrained", {"scoped": [{"entity": {"name": "me"}, "permissions": ["repo.content.read", "repo.write"]}]}),
     "repo.write"),
    (whoami("god"), "god token"),
    ({"auth": {}}, "didn't say"),
])
def test_only_read_only_tokens_pass(doc, problem):
    got = hfauth.read_only_problem(doc["auth"])
    assert got is None if problem is None else problem in got


class WhoamiOpener:
    def __init__(self, status=200, doc=None):
        self.status, self.doc, self.seen = status, doc, []

    def open(self, req, timeout):
        self.seen.append((req.full_url, req.get_header("Authorization")))
        if self.status != 200:
            raise urllib.error.HTTPError(req.full_url, self.status, "no", Hdrs(), None)
        return Resp(200, {"Content-Type": "application/json"}, json.dumps(self.doc).encode())


def test_check_asks_hugging_face_and_refuses_what_isnt_read_only(monkeypatch):
    op = WhoamiOpener(doc=whoami("fineGrained", {"global": [], "scoped": [
        {"entity": {"type": "model", "name": "org/m"}, "permissions": ["repo.content.read"]}]}))
    monkeypatch.setattr(netsafe, "_OPENER", op)
    acct = hfauth.check(TOKEN)
    assert (acct.name, acct.token_name, acct.role, acct.scoped) == ("someone", "flotilla", "fineGrained", ["org/m"])
    assert op.seen == [("https://huggingface.co/api/whoami-v2", f"Bearer {TOKEN}")]
    op.doc = whoami("write")
    with pytest.raises(TokenError, match="read-only tokens"):
        hfauth.check(TOKEN)
    op.doc = whoami(type_="org")
    with pytest.raises(TokenError, match="personal account"):
        hfauth.check(TOKEN)
    op.status = 401
    with pytest.raises(TokenError, match="doesn't accept"):
        hfauth.check(TOKEN)
    op.status = 503
    with pytest.raises(netsafe.FetchError):
        hfauth.check(TOKEN)


def test_fetch_sends_authorization_only_to_the_host_it_names(monkeypatch):
    seen = []

    class Redirect:
        def open(self, req, timeout):
            host = urllib.parse.urlsplit(req.full_url).hostname
            seen.append((host, req.get_header("Authorization")))
            if host == "huggingface.co":
                raise urllib.error.HTTPError(req.full_url, 302, "Found", Hdrs(Location="https://cdn.hf.co/x"), None)
            return Resp(200, {}, b"{}")

    monkeypatch.setattr(netsafe, "_OPENER", Redirect())
    netsafe.fetch("https://huggingface.co/api/x", max_bytes=100, allowed=("huggingface.co", "hf.co"),
                  auth=("huggingface.co", "Bearer s"))
    assert seen == [("huggingface.co", "Bearer s"), ("cdn.hf.co", None)]


# ---------------------------------------------------------------- the node's connection


def test_connection_follows_the_file_and_checks_once(tmp_path, monkeypatch):
    logs, calls = [], []
    monkeypatch.setattr(hfauth, "check", lambda t: calls.append(t) or Account("someone", "flotilla", "read"))
    path = tmp_path / "hf-token"
    c = Connection(path, logs.append)
    assert not c.refresh() and c.state == "none" and c.auth() is None and calls == []
    hfauth.save_token(path, TOKEN)
    g = c.generation
    assert c.refresh() and c.state == "unchecked" and c.generation == g + 1
    assert calls == []                                               # not checked until a gated model needs it
    assert c.auth() == ("huggingface.co", f"Bearer {TOKEN}") and c.auth() and calls == [TOKEN]
    assert c.info()["name"] == "someone" and any("using someone's read-only token" in l for l in logs)
    assert not c.refresh()                                           # unchanged file: nothing happens
    c.rejected()                                                     # revoked since
    assert c.auth() is None and c.state == "refused" and "no longer accepts" in c.info()["why"]
    assert c.disconnect() and not path.exists() and c.state == "none"


def test_a_token_that_cant_be_checked_is_not_used_until_it_can(tmp_path, monkeypatch):
    outcome = [netsafe.FetchError(hfauth.WHOAMI_URL, None, "timed out")]

    def check(t):
        if isinstance(outcome[0], Exception):
            raise outcome[0]
        return outcome[0]

    monkeypatch.setattr(hfauth, "check", check)
    path = tmp_path / "hf-token"
    hfauth.save_token(path, TOKEN)
    c = Connection(path, lambda _: None)
    c.refresh()
    assert c.auth() is None and c.state == "error"
    outcome[0] = Account("someone", "flotilla", "read")
    assert c.auth() is None                                          # not before RECHECK_AFTER_ERROR
    c._retry_at = time.time() - 1
    assert c.auth() is not None and c.state == "ok"
    outcome[0] = TokenError("it's a Write token")
    hfauth.save_token(path, OTHER)
    c.refresh()
    assert c.auth() is None and c.state == "refused" and "Write token" in c.info()["why"]


# ---------------------------------------------------------------- gated models on a real libtorrent


class GatedHF(FakeHF):
    """Hugging Face serving a gated model: only to the token in `let_in`."""

    def __init__(self, src, let_in=(), revoked=()):
        super().__init__(src)
        self.let_in, self.revoked, self.auths = set(let_in), set(revoked), []

    def __call__(self, url, start, length, *, allowed, auth=None, on_chunk=None, timeout=60.0):
        host = urllib.parse.urlsplit(url).hostname
        sent = auth[1] if auth and auth[0] == host else None           # what fetch_range would send
        self.auths.append((host, sent))
        if host == "huggingface.co":
            token = sent.removeprefix("Bearer ") if sent else None
            if token in self.revoked:
                raise netsafe.FetchError(url, 401, "HTTP 401",
                                         headers={"X-Error-Message": "Invalid credentials in Authorization header"})
            if token not in self.let_in:
                raise netsafe.FetchError(url, 403 if token else 401, "HTTP", headers={"X-Error-Code": "GatedRepo"})
        return super().__call__(url, start, length, allowed=allowed, on_chunk=on_chunk)


def gated_engine(tmp_path, monkeypatch, fake_check=True):
    src, raw = pf_torrent(tmp_path, {"a.bin": 3 * PIECE})
    logs = []
    eng, h, ih, idx, meta = held_engine(tmp_path, raw, src, ["a.bin"], logs)
    eng.hf.conn = Connection(tmp_path / "hf-token", logs.append)
    if fake_check:
        monkeypatch.setattr(hfauth, "check", lambda t: Account("someone", "flotilla", "read"))
    key = (ih, idx["a.bin"])
    return src, logs, eng, h, ih, key, meta, idx


def have_all(h, meta, idx):
    first, last = meta.files[idx["a.bin"]].piece_range(meta.piece_length)
    return all(h.have_piece(p) for p in range(first, last + 1))


def test_gated_without_an_account_waits_and_asks_again_hourly(tmp_path, monkeypatch):
    src, logs, eng, h, ih, key, meta, idx = gated_engine(tmp_path, monkeypatch)
    fake = GatedHF(src, let_in={TOKEN})
    monkeypatch.setattr(hfpieces.netsafe, "fetch_range", fake)
    make_stuck(eng, key)
    eng.hf.fetch_file(key, time.monotonic() + 60)
    g = eng.hf.gated[ih]
    assert g["kind"] == "account" and g["until"] > time.time() + GATED_RECHECK - 60
    assert fake.auths == [("huggingface.co", None)]
    said = [l for l in logs if "flotilla hf connect" in l]
    assert len(said) == 1 and "asking Hugging Face again every hour" in said[0]
    assert eng.hf.scan(time.time() + 900) == []                       # not asked again within the hour
    assert eng.hf.gated_list() == [{"repo": "org/m", "kind": "account", "url": "https://huggingface.co/org/m",
                                    "until": g["until"]}]
    assert eng.status_doc()["hf_gated"][0]["repo"] == "org/m"
    eng.hf.gated[ih]["until"] = time.time() - 1                      # an hour on
    assert key in eng.hf.scan(time.time() + 1000)
    eng.hf.fetch_file(key, time.monotonic() + 60)
    assert len(fake.auths) == 2 and len([l for l in logs if "flotilla hf connect" in l]) == 1   # said once

    hfauth.save_token(tmp_path / "hf-token", TOKEN)                   # `flotilla hf connect` while it runs
    assert key in eng.hf.scan(time.time() + 1100) and ih not in eng.hf.gated
    eng.hf.fetch_file(key, time.monotonic() + 60)
    assert have_all(h, meta, idx)
    # Anonymous first; once refused, the token went with every request to huggingface.co, never to the CDN.
    assert fake.auths[2] == ("huggingface.co", None) and fake.auths[3] == ("huggingface.co", f"Bearer {TOKEN}")
    assert all(a is None for host, a in fake.auths if host != "huggingface.co")
    assert all(a for host, a in fake.auths[3:] if host == "huggingface.co")
    eng.handles = {}
    eng.shutdown()


def test_public_models_never_get_the_token(tmp_path, monkeypatch):
    src, _logs, eng, h, _ih, key, meta, idx = gated_engine(tmp_path, monkeypatch)
    hfauth.save_token(tmp_path / "hf-token", TOKEN)
    fake = GatedHF(src, let_in={None, TOKEN})                         # public: anyone may fetch it
    monkeypatch.setattr(hfpieces.netsafe, "fetch_range", fake)
    make_stuck(eng, key)
    eng.hf.fetch_file(key, time.monotonic() + 60)
    assert have_all(h, meta, idx)
    assert all(a is None for _, a in fake.auths) and eng.hf.conn.state == "unchecked"   # never even checked
    eng.handles = {}
    eng.shutdown()


def test_an_account_not_let_in_is_told_where_to_ask(tmp_path, monkeypatch):
    src, logs, eng, h, ih, key, meta, idx = gated_engine(tmp_path, monkeypatch)
    hfauth.save_token(tmp_path / "hf-token", TOKEN)
    fake = GatedHF(src, let_in={OTHER})
    monkeypatch.setattr(hfpieces.netsafe, "fetch_range", fake)
    make_stuck(eng, key)
    eng.hf.fetch_file(key, time.monotonic() + 60)
    assert eng.hf.gated[ih]["kind"] == "access"
    assert any("hasn't let someone's account in (request access at https://huggingface.co/org/m)" in l for l in logs)
    fake.let_in.add(TOKEN)                                            # the author lets us in
    eng.hf.gated[ih]["until"] = time.time() - 1                      # and the hourly check comes round
    assert key in eng.hf.scan(time.time() + 1000)
    eng.hf.fetch_file(key, time.monotonic() + 60)
    assert have_all(h, meta, idx) and ih not in eng.hf.gated
    assert any("serves it to us now" in l for l in logs)
    eng.handles = {}
    eng.shutdown()


def test_a_revoked_token_stops_being_used(tmp_path, monkeypatch):
    src, logs, eng, _h, ih, key, _meta, _idx = gated_engine(tmp_path, monkeypatch)
    hfauth.save_token(tmp_path / "hf-token", TOKEN)
    fake = GatedHF(src, revoked={TOKEN})
    monkeypatch.setattr(hfpieces.netsafe, "fetch_range", fake)
    make_stuck(eng, key)
    eng.hf.fetch_file(key, time.monotonic() + 60)
    assert eng.hf.conn.state == "refused" and eng.hf.gated[ih]["kind"] == "account"
    assert any("no longer accepts the connected token" in l for l in logs)
    eng.hf.gated[ih]["until"] = 0
    eng.hf.fetch_file(key, time.monotonic() + 60)
    assert [a for _, a in fake.auths].count(f"Bearer {TOKEN}") == 1   # not sent again
    eng.handles = {}
    eng.shutdown()


def test_disconnect_from_the_dashboard(tmp_path, monkeypatch):
    _src, logs, eng, _h, _ih, _key, _meta, _idx = gated_engine(tmp_path, monkeypatch)
    hfauth.save_token(tmp_path / "hf-token", TOKEN)
    eng.hf.scan(time.time())
    assert eng.snapshot()["hf"]["account"]["state"] == "unchecked"
    assert eng.submit("hf_disconnect", {"anything": 1}) == "queued"
    eng.process_commands()
    assert not (tmp_path / "hf-token").exists() and eng.snapshot()["hf"]["account"]["state"] == "none"
    assert any("account token disconnected from the dashboard" in l for l in logs)
    eng.handles = {}
    eng.shutdown()


# ---------------------------------------------------------------- the command line


def run_cli(tmp_path, *argv):
    return cli.main(["--config", str(tmp_path / "config.toml"), "--state", str(tmp_path / "state"), *argv])


def test_hf_connect_checks_then_saves(tmp_path, monkeypatch, capsys):
    good = tmp_path / "token.txt"
    good.write_text(TOKEN + "\n")
    monkeypatch.setattr(hfauth, "check", lambda t: (_ for _ in ()).throw(TokenError("it's a Write token")))
    assert run_cli(tmp_path, "hf", "connect", "--from-file", str(good)) == 1
    assert not (tmp_path / "hf-token").exists() and "Write token" in capsys.readouterr().out

    monkeypatch.setattr(hfauth, "check", lambda t: Account("someone", "flotilla", "read"))
    assert run_cli(tmp_path, "hf", "connect", "--from-file", str(good)) == 0
    assert hfauth.read_token(tmp_path / "hf-token") == TOKEN
    assert stat.S_IMODE((tmp_path / "hf-token").stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "Connected: someone's token 'flotilla'" in out and TOKEN not in out

    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "status.json").write_text(json.dumps({"hf_gated": [{"repo": "org/m", "kind": "access"}]}))
    assert run_cli(tmp_path, "hf", "status") == 0
    out = capsys.readouterr().out
    assert "Connected as someone" in out and "https://huggingface.co/org/m (request access there)" in out
    assert TOKEN not in out

    assert run_cli(tmp_path, "hf", "disconnect") == 0
    assert not (tmp_path / "hf-token").exists() and "Disconnected" in capsys.readouterr().out


def test_hf_connect_asks_without_echo(tmp_path, monkeypatch, capsys):
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": TOKEN)
    monkeypatch.setattr(hfauth, "check", lambda t: Account("someone", "flotilla", "read"))
    assert run_cli(tmp_path, "hf", "connect") == 0
    out = capsys.readouterr().out
    assert "type Read" in out and "username" in out and TOKEN not in out
    assert hfauth.read_token(tmp_path / "hf-token") == TOKEN


def test_setup_offers_an_account_only_with_hugging_face_on(tmp_path, monkeypatch):
    import getpass

    from flotilla import netinfo, sources

    monkeypatch.setattr(netinfo, "internet_route", lambda: None)

    class NoPF:
        def __init__(self, *a, **kw):
            pass

        def licenses(self):
            raise OSError("offline")

    monkeypatch.setattr(sources, "PirateFace", NoPF)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": TOKEN)
    monkeypatch.setattr(hfauth, "check", lambda t: Account("someone", "flotilla", "read"))
    seen = []

    def answers(hf, connect):
        def ask(prompt=""):
            prompt = prompt.lstrip().removeprefix("? ")
            seen.append(prompt)
            if prompt.startswith("Folder"):
                return str(tmp_path / "seeds")
            if prompt.startswith("Fetch pieces no peer has"):
                return hf
            if prompt.startswith("Connect a Hugging Face account"):
                return connect
            return ""
        return ask

    cfg = tmp_path / "config.toml"
    monkeypatch.setattr("builtins.input", answers("", "y"))
    assert cli.cmd_init(argparse.Namespace(config=str(cfg))) == 0
    assert not any(p.startswith("Connect a Hugging Face account") for p in seen)
    assert not (tmp_path / "hf-token").exists()

    seen.clear()
    monkeypatch.setattr("builtins.input", answers("y", ""))           # on, but no account (the default)
    assert cli.cmd_init(argparse.Namespace(config=str(cfg))) == 0
    asked = [p for p in seen if p.startswith("Connect a Hugging Face account")]
    assert asked and asked[0].endswith("(y/N): ") and not (tmp_path / "hf-token").exists()

    monkeypatch.setattr("builtins.input", answers("y", "y"))
    assert cli.cmd_init(argparse.Namespace(config=str(cfg))) == 0
    assert hfauth.read_token(tmp_path / "hf-token") == TOKEN

    seen.clear()
    monkeypatch.setattr("builtins.input", answers("y", ""))           # already connected: keep it?
    assert cli.cmd_init(argparse.Namespace(config=str(cfg))) == 0
    assert any(p.startswith("A Hugging Face account token is connected") for p in seen)
    assert (tmp_path / "hf-token").exists()
