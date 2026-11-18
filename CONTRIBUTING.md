# Contributing to Flotilla Face

Thanks for helping keep open-weight models alive.

## Set up

```bash
git clone https://github.com/nickmontag/flotilla-face && cd flotilla-face
uv venv -p 3.13 && uv pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Python 3.11–3.13. libtorrent has no wheels for 3.14+ yet, so pin 3.13 if your default is newer.

The test suite never touches the internet. `tests/test_swarm.py` runs real libtorrent sessions on
127.0.0.1 (a full seeder, a Flotilla node, an observer) and takes about 25 seconds.

## Where things live

| Module | What it does |
|---|---|
| `config.py` | The config file (TOML), local state, the memory budget |
| `netsafe.py` | The only HTTP client: HTTPS, host allowlist, redirect re-checks, byte caps, the network pin, byte-range fetches |
| `signing.py`, `feed.py` | minisign-compatible signatures; the signed priority feed (expiry, rollback, pins) |
| `ledger.py` | The signed, append-only hash ledger: loading, capture, folder checks |
| `sources.py` | Read-only Pirate Face and Hugging Face API clients |
| `magnet.py`, `torrentmeta.py` | Strict magnet and .torrent parsing, infohash recomputation |
| `candidates.py` | The trust chain: listing → magnet → .torrent → Hugging Face file match → hashes |
| `planner.py` | Pure function: which shards this node should hold |
| `engine.py` | libtorrent: probe swarms, apply plans, verify, seed, disk guard |
| `hfpieces.py` | Pieces no peer is sending, fetched from Hugging Face and checked against the torrent |
| `hfauth.py` | The Hugging Face account for gated models: the token file, the read-only check, when the token is sent |
| `pfaccount.py` | The Pirate Face account for points: the key file, the account's seeding tracker |
| `magnetindex.py` | Pirate Face's magnet index: keeping and seeding it, reading its shards, listings from it while Pirate Face is down |
| `perfile.py`, `v2hash.py` | Per-file swarms: looking up and checking each shard's own torrent, supplying its BitTorrent v2 piece layer |
| `history.py` | Upload and download history (SQLite) |
| `dashboard.py` | Local web dashboard and its JSON API |
| `remote.py` | The dashboard as a separate process, through the shared state folder |
| `service.py` | launchd / systemd user service install |
| `netinfo.py` | Read-only network facts: is traffic going through a VPN? |
| `worthrescuing.py` | Editor tool: bring the list in line with Pirate Face's Worth Rescuing page |
| `fallback.py` | Editor tool: the fallback list of popular refusal-removed models on Hugging Face |
| `cli.py` | Commands |

`DESIGN.md` explains the trust model and the planner maths. Read it before changing either.

## Ground rules

- **Security-relevant changes need a test.** Anything touching `netsafe`, `signing`, `feed`, `ledger`,
  `magnet`, `torrentmeta`, `candidates`, `hfauth`, `pfaccount`, `perfile` or `magnetindex` should come with a test showing the attack it blocks.
- **Never execute downloaded content**, and don't add a code path that could.
- **No new runtime dependencies** without a good reason. Easy install is a feature.
- **Keep the planner pure.** No I/O in `planner.py`, so it stays testable.
- Pull requests run the suite on macOS (Apple Silicon) and Linux (x86_64 and arm64), Python 3.11–3.13.

## The priority list

The published list is `flotilla/data/priorities.json`, signed with the project key
(`flotilla/data/priorities.json.minisig`). To change it, edit it, bump `version`, run
`flotilla editor sign flotilla/data/priorities.json` on the maintainer's machine, and commit everything
it changed: the list, its `.minisig` and `flotilla/data/ledger/` (signing also records newly pinned
models in the hash ledger). A test checks that the shipped list verifies with the key in
`BUILTIN_EDITOR_KEYS`. Never commit a secret key: `*.key` is ignored, but check `git status` anyway.

## Releasing (maintainers)

Installs follow releases, not `main`, so `main` can be mid-change. A release:

1. Set the version in `flotilla/__init__.py` (`pyproject.toml` reads it from there) and push to `main`.
2. Tag it with the release notes as the tag's message, and push the tag:

   ```bash
   git tag -a v0.3.0 -m "What changed, for people updating"
   git push origin v0.3.0
   ```

   `.github/workflows/release.yml` runs the tests, builds the wheel and source archive and the Docker
   image (amd64 and arm64, pushed to `ghcr.io/nickmontag/flotilla-face`), and makes a **draft**
   release holding them and the image's digest. Nobody sees a draft.
3. Sign and publish it, on the machine with the project key:

   ```bash
   flotilla editor release v0.3.0            # --dry-run to sign without publishing
   ```

   It downloads the draft's files, checks the wheel is that version, writes `release.json` (the
   files' SHA-256s and the image digest), signs it with `~/.config/flotilla/editor.key`, checks the
   signature against the key built into Flotilla, uploads it and publishes the release.

From then on `flotilla update` offers it, and nodes say a new release is out within a day. The key
never goes to GitHub: CI builds, only you sign.

