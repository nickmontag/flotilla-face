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
| `pfaccount.py` | The Pirate Face account for points: the key file (a key or the tracker address), the account's seeding tracker |
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

## If the project key is lost or stolen

One key signs everything nodes trust: the priority list, the hash ledger's index, and each release's
`release.json`. Its public half is the only entry in `BUILTIN_EDITOR_KEYS` (`flotilla/config.py`, key
ID 468DA6DE310D390E). Its secret half is `~/.config/flotilla/editor.key` on the maintainer's machine,
unencrypted (mode 0600). There is no second key, nothing here records a backup copy, and there's no
way to tell installed nodes that a key has gone bad.

### If it's lost

Nothing stops at once. Nodes keep seeding, and keep the list and ledger they have. Then:

- **No new list or ledger records.** A signed list expires 90 days after it's issued. Nodes that
  fetched it then fall back to neutral priorities (every model equal), because the copy bundled with
  their install is an older version, which rollback protection refuses.
- **No signed releases.** Nothing can be released until there's a new key, and existing installs
  check releases against the old one: once a release is signed with any other key, their
  `flotilla update` refuses it and their Docker setup can't start.

What the maintainer would do:

1. `flotilla editor keygen`, and replace the old key in `BUILTIN_EDITOR_KEYS` with the new one.
2. Sign a new list (version bumped) and the ledger index with it, and release a version signed with
   it.
3. Tell users, in the release notes, the README and a pinned issue, that existing installs must run
   the installer again (`curl ... | sh`), since `flotilla update` can't vouch for the new key. The
   installer trusts GitHub over HTTPS, not the key, so it still works.

### If it's stolen

The key alone reaches no one: nodes fetch the list and ledger from the repo's `main` branch and
releases from its GitHub releases. Someone who has the key **and** can push to the repo or publish
releases (the maintainer's GitHub account, a leaked token) can:

- **Publish a release.** Every node's `flotilla update` would offer it, and Docker setup would run its
  image. Nothing installs unasked, but anyone who says yes runs the thief's code. This is the serious
  case.
- **Publish a list** steering nodes to other models. Each still has to pass the trust chain (a Pirate
  Face torrent under an allowed license, its hashes matching Hugging Face or the ledger), and nothing
  downloaded is ever run. Its `version` could be set very high, after which nodes refuse any list
  numbered lower (rollback protection).
- **Add ledger records** for models nodes don't know yet, with hashes of their choosing. That matters
  for models Hugging Face no longer hosts, where the ledger is the authority. Records nodes already
  hold can't be rewritten: each node keeps the first one it accepted.

What the maintainer would do:

1. Say so at once (a pinned issue, a README notice, a GitHub security advisory): don't accept
   `flotilla update` until told, and which versions are good.
2. Lock the GitHub account down (password, 2FA, tokens) and delete any release or commit the thief
   published.
3. Make a new key and release a version that trusts only it, signed with it. Existing nodes still
   trust the stolen key and can't check the new one, so, as with a lost key, everyone reinstalls with
   the installer. The next list's `version` goes above any the thief used.

Until a node reinstalls it trusts the stolen key, and nothing can tell it otherwise.

### Options, for the maintainer to decide

Anything that changes what nodes trust only protects nodes running a release that includes it, so
the earlier it ships, the more nodes it covers.

| Option | Covers | Costs |
|---|---|---|
| **An encrypted backup** of `editor.key`, offline and away from the machine (a password manager, a USB drive in a drawer). Optionally encrypt the key on disk too. | Loss. No code change. | Nothing against theft: a copy is one more place to steal it from. |
| **A second built-in key**, kept offline and apart from the first. The code already accepts any key in `BUILTIN_EDITOR_KEYS`. | Loss: sign with the other key and nodes carry on, with no reinstall. | Nothing against theft, and it doubles what can be stolen: either key alone signs anything. Revoking one needs a rule nodes follow, which the other key's thief could use too. |
| **A recovery key that can only replace keys**: the second key may sign a key change and nothing else. Nodes keep the trusted key list in their state and accept a change signed by it. | Loss and theft of the everyday key, without reinstalls, as long as the recovery key stays offline and unused. | New, security-critical code (a signed key list with its own rollback protection) that must ship before it's needed. A stolen recovery key is as bad as today. |
| **Separate keys by job**: one for releases, used a few times a year and kept offline; one for the list and ledger, used often. | Theft of the everyday key can no longer ship code, only a list or ledger records. | A small code change (releases check their own key list) and one more key to keep and back up. Loss of either still needs one of the options above. |
| **Threshold signing**, 2 of N keys held by different people or devices. Already in DESIGN.md's future work. | Theft of any one key does nothing; with N = 3, loss of one is fine too. A second editor could also check each ledger record against Hugging Face independently. | Needs at least one more trusted key holder. Every release and list update waits on two signatures. More code: several signatures per file, counting distinct key IDs. |

These combine: separate keys by job, each with a recovery key, and an encrypted backup of each, is
a common setup.

