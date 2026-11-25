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
| `release.py` | Signed releases: what `flotilla update` installs and the update notice checks for |
| `netinfo.py` | Read-only network facts: is traffic going through a VPN? |
| `vpnport.py` | The port a VPN forwards to this node, so peers can connect in |
| `containers.py` | VPN containers such as gluetun on this machine, and running Flotilla inside one |
| `access.py` | Who can reach the dashboard: this machine, the home network, the tailnet or anyone |
| `worthrescuing.py` | Editor tool: bring the list in line with Pirate Face's Worth Rescuing page |
| `fallback.py` | Editor tool: the fallback list of popular refusal-removed models on Hugging Face |
| `catalog.py`, `sweep.py` | Spare space once the list is covered: the catalog list of popular models, then a sweep of Pirate Face's least-seeded ones |
| `cli.py` | Commands |
| `ui.py` | Terminal styling for interactive setup |

`DESIGN.md` explains the trust model and the planner maths. Read it before changing either.

Two scripts help with work the tests don't cover:

- `scripts/demo_dashboard.py` runs the real engine and dashboard against a fake local swarm, with no
  internet, for UI work: `.venv/bin/python scripts/demo_dashboard.py`, then open http://127.0.0.1:7499.
- `scripts/deploy-dev.sh <ssh host>` tries this working tree on a Docker host's Flotilla without
  making a release; `flotilla update` on the host puts the signed release back.

## Ground rules

- **Security-relevant changes need a test.** Anything touching `netsafe`, `signing`, `feed`, `ledger`,
  `magnet`, `torrentmeta`, `candidates`, `hfauth`, `pfaccount`, `perfile` or `magnetindex` should come with a test showing the attack it blocks.
- **Never execute downloaded content**, and don't add a code path that could.
- **No new runtime dependencies** without a good reason. Easy install is a feature.
- **Keep the planner pure.** No I/O in `planner.py`, so it stays testable.
- Pull requests run the suite on macOS (Apple Silicon) and Linux (x86_64 and arm64), Python 3.11–3.13.

## The priority list

The published list is `flotilla/data/priorities.json`, signed with the project key
(`flotilla/data/priorities.json.minisig`). A test checks that the shipped list verifies with the key in
`BUILTIN_EDITOR_KEYS`. README.md explains what the list is for and how nodes use it.

```bash
flotilla editor sign flotilla/data/priorities.json
```

Edit the file, bump `version`, then sign it. The signing key is `~/.config/flotilla/editor.key` on the
maintainer's machine. Back it up and never commit it: `*.key` is git-ignored, but check `git status`
anyway. Its public half is in `BUILTIN_EDITOR_KEYS` in `flotilla/config.py`.

Signing the list also keeps the [hash ledger](README.md#the-hash-ledger) up to date, so there's no separate
routine to remember. It records every newly pinned model from Hugging Face, re-signs the ledger if
anything changed, timestamps the new records, and completes any proofs from earlier runs whose
Bitcoin transaction has since confirmed. Then commit everything it changed: the list and its
`.minisig`, plus `flotilla/data/ledger/`. `--no-ledger` signs the list alone.

Per model you can set:

- `scores` (0–100 per axis);
- `target_replicas`, its minimum target: how many full copies the swarm should hold. Set it from
  the model's takedown risk with the rule below;
- `spare`: `true` to have nodes look at the model only once nearly everything else on the list has
  its copies (or while they hold it). Every
  model on the list costs each node a Pirate Face lookup at every refresh, so models meant only for
  a big fleet's spare space shouldn't cost anything until then;
- a `boost` for emergencies;
- a `note`;
- `revision` / `infohash` pins;
- `license`, when a publisher's label is wrong (a derivative of a custom license relabelled MIT, say);
- `origin`: `base`, `method` (`original`, `refusal-removal`, `fine-tune`, `fine-tune+refusal-removal` or
  `unknown`), `data` (`public` or `undisclosed`, for fine-tunes) and `repack_of`.

Put a repo or infohash in `deny` to make every node drop it, for example to honour a takedown.

**Minimum targets.** A model loses its last copy not when one seeder leaves (Flotilla copies it again
within a day) but when a takedown wave removes many seeders at once: a crackdown in one country, a VPN
provider blocking torrents, a run of legal notices. So a model's minimum target is the fewest full
copies for at least one to survive such a wave 99% of the time, assuming the wave removes a share of
its seeders that grows steeply with its takedown risk: 70% × (risk/100)^1.5. Never fewer than 2.

| Takedown risk | Share a wave removes | Minimum target |
| --- | --- | --- |
| 20 | 6% | 2 |
| 30–40 | 12–18% | 3 |
| 52–58 | 26–31% | 4 |
| 68 | 39% | 5 |
| 72–75 | 43–45% | 6 |
| 80 | 50% | 7 |
| 84 | 54% | 8 |
| 88–90 | 58–60% | 9 |
| 92 | 62% | 10 |
| 95 | 65% | 11 |

`flotilla/feed.py` (`target_for_risk`) holds the rule; the sync tools use it, and a test checks the
published list follows it. The 99% and the 70% are judgment calls, one line each to change. Copies past
7 wait until nearly every model on the list has 2, so no model on the list is down to its last copy
while another gets its eighth.

**Follow Pirate Face's Worth Rescuing page.** Pirate Face keeps a page of models worth preserving: a
historic shortlist and the top open models on Artificial Analysis's Intelligence Index. To bring the
list in line with it:

```bash
flotilla editor sync-worth-rescuing flotilla/data/priorities.json --dry-run   # see what would change
flotilla editor sync-worth-rescuing flotilla/data/priorities.json
flotilla editor sign flotilla/data/priorities.json
```

It adds each model on the page that has a Pirate Face torrent and a license Flotilla seeds by default,
pinned to the commit Pirate Face serves. It updates pins that moved and removes models that left the
page. It only touches entries it added (marked `"via": "pirateface/worth-rescuing"`); a model already on
the list for another reason is left alone. Official releases carry a `boost` of 0.45, under half the
weight of the lowest at-risk model. Since the planner halves a file's value once someone else holds a
copy, a node spends space on official releases only after the at-risk models' files have a copy or two. The command also reports any model whose license differs from the custom
license of the model it's built on. Nodes never read the page themselves: they only ever follow the
signed list.

**Fill in with popular refusal-removed models.** Once the hand-picked models are well seeded, nodes
need something else worth carrying. To add the most-downloaded abliterated, heretic, obliterated and
uncensored builds on Hugging Face that Pirate Face has torrents for:

```bash
flotilla editor sync-fallback flotilla/data/priorities.json --dry-run   # see what would change
flotilla editor sync-fallback flotilla/data/priorities.json
flotilla editor sign flotilla/data/priorities.json
```

It groups each build with its quantisations, picks one copy per group (the full-precision release
when Pirate Face has it), keeps at most two per base model and at most 40 in all (`--max`). The license
must be allowed by default, and so must the base model's when a build relabels it. Entries are marked
`"via": "huggingface/refusal-removed"` and rank at a priority of about 0.2-0.3, below every
hand-picked model, so nodes turn to them once the hand-picked models have a couple of copies each.
DESIGN.md explains the ranking. For a bigger fleet, raise the caps: `--max` (entries), `--per-base`
(builds of one base model, default 2) and `--search-limit` (Hugging Face results per search term,
default 200, at most 1000). `--spare-after N` marks every entry past the top N `spare` (see above), so
a long fallback list costs nodes nothing until the rest is covered.

**Widen the list for a big fleet.** Once everything on the list has its target number of copies, more
space would only add a 20th or 40th copy of the same models. To add Hugging Face's most downloaded
language and vision-language models that Pirate Face's magnet index lists:

```bash
flotilla editor sync-catalog flotilla/data/priorities.json --index <shards folder> --dry-run
flotilla editor sync-catalog flotilla/data/priorities.json --index <shards folder>
flotilla editor sign flotilla/data/priorities.json
```

`--index` is a magnet index build's `shards` folder; a node keeps one at
`<state>/index/files/magnet-index/shards`, the default on a machine that runs one. Refusal-removed
builds are left to the fallback list. It keeps one model per base (`--per-base`) and at most 100 in all
(`--max`), checked like the fallback list. Entries are marked `"via": "pirateface/catalog"`, have a
target of 2 copies, rank at 0.16-0.19 (above Pirate Face's unranked new torrents, below everything
else on the list) and are `spare`: nodes don't look at them until the rest of the list has its copies.
Then each gets a couple of copies before any gets a third.

## The hash ledger

README.md explains what the ledger is and how to check a download against it.

Recording happens when you sign the priority list (see above). A model has to be recorded while Hugging Face
still has it, so sign and push soon after adding one. A model added after Hugging Face removed it can
only be recorded from Pirate Face's manifest, and is labelled that way.

Timestamping needs the OpenTimestamps client, `uv tool install opentimestamps-client`. Without it,
signing still records and signs, and tells you nothing was timestamped. Stamping sends only each
record's SHA-256 to the public OpenTimestamps calendars. Records are never edited: recording only adds,
and signing refuses a ledger whose records have changed.

The steps can also be run on their own:

```bash
flotilla editor ledger-capture flotilla/data/priorities.json   # record new pinned models (--dry-run to preview)
flotilla editor sign flotilla/data/ledger/index.json           # sign the ledger alone
flotilla editor stamp flotilla/data/ledger/index.json          # timestamp new records, complete ready proofs
flotilla editor stamp --upgrade flotilla/data/ledger/index.json   # only complete proofs
```

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

