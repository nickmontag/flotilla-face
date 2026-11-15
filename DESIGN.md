# Flotilla Face design

## What exists today (Pirate Face, Nov 2026)

These facts shaped the design. All of them were checked against the live service.

- **One torrent per repo** (or per quant): `zai-org/GLM-5.2` is a single 1.5 TB torrent with 290 files,
  282 of them `model-000NN-of-00282.safetensors` shards of about 5.3 GB each.
- **Two torrent formats**, with pieces from 1 MiB to 64 MiB (16 MiB is the most common).
  - *v1 only, no pad files.* Pieces straddle file boundaries, so fetching one shard alone also pulls
    at most two boundary pieces that it shares with its neighbours. With 16 MiB pieces that's ≤ 32 MiB,
    under 1% overhead per shard, and libtorrent keeps the neighbour bytes in a `.parts` file.
  - *Hybrid v1 + v2.* Pad files start every file on a piece boundary, so a shard has no boundary
    overhead. These .torrent files leave out the `piece layers` BEP 52 expects, which costs memory
    (see [Memory](#memory)).
- Trackers: `udp://` and `http://tracker.pirateface.co:6969/announce`. Web seed (BEP 19):
  `https://pirateface.co/api/ws/{repo}/{revision}/`, which serves the files from Hugging Face.
- `GET /api/torrents?repo=` returns the magnet, infohash, pinned revision, seeder and leecher counts, a
  `hf` flag and a per-file SHA-256 manifest. `GET /api/torrents/{infohash}.torrent` returns the
  metainfo.
- `GET /api/models` (trending catalogue with `seed`, `leech`, `hfBacked`, `license`), `/api/rescue`
  (rescue queue) and `/api/licenses` (with `acceptFirst` for licenses whose terms must be accepted).
- Hugging Face's `GET /api/models/{repo}/tree/{revision}?recursive=true` independently gives size plus
  LFS SHA-256 for big files, and git blob SHA-1 for small ones. On GLM-5.2 all 283 LFS hashes matched
  Pirate Face's manifest.

So shard-level seeding needs **no changes on Pirate Face's side**. Selecting a subset of files in the
existing per-repo torrent is enough.

## Components

```
            signed feed (minisign)               Pirate Face API            Hugging Face API
                    │                                  │                          │
                    ▼                                  ▼                          ▼
   feed.py ── verify sig, expiry, rollback   sources.py (netsafe.fetch: HTTPS + host allowlist)
                    │                                  │                          │
                    └──────────────┬───────────────────┴──────────────────────────┘
                                   ▼
                     candidates.py — trust chain → TorrentCand[]
                                   │
                                   ▼
                engine.probe — throwaway libtorrent session reads peer bitfields
                                   │  per-file availability
                                   ▼
                     planner.py — pure function → Plan (files per torrent)
                                   │
                                   ▼
          engine.Engine — main libtorrent session: file priorities, rate limits,
                          verify on completion, resume data, replan every 6 h
```

## Trust model

The threats: someone in the middle (or a compromised mirror) gets a seeder to fetch unrelated or
malicious files, inflates or deflates priorities, replays an old feed, or makes a client fill its disk.

| Layer | Check | Defends against |
|---|---|---|
| HTTP | HTTPS only, certifi CA bundle, host allowlist (`pirateface.co`, `huggingface.co`), manual redirects re-checked per hop, no userinfo, no odd ports, byte caps on every response | MITM, open redirects, memory exhaustion |
| Feed | Ed25519 minisign signature against pinned keys; `expires`; monotonic `version` stored locally | Forged priorities, freeze/replay, rollback |
| Feed pins | Optional `infohash` and `revision` per model; a listing that disagrees is rejected | A compromised index swapping the torrent for a pinned model |
| Hash ledger | Signed, append-only per-file hashes per repo@revision, captured from HF; a torrent file it doesn't list, a size or hash HF contradicts, or (HF gone) a revision it never recorded refuses the torrent | A compromised Pirate Face swapping files and manifest together after HF has removed the model |
| Magnet | Tracker hosts must be `tracker.pirateface.co`; web seeds `https` on the allowlist; any param other than `xt dn tr ws xl` (e.g. `x.pe`, `as`, `xs`) rejects the magnet | Steering the client to foreign trackers, seeds or payloads |
| .torrent | SHA-1 of the exact info-dict bytes must equal the listed infohash; its own `announce`, `announce-list` and `url-list` pass the same allowlist; BEP 17 `httpseeds` refused; path components sanitised (no `..`, `/`, `\`, `:`, NUL) | Swapped metainfo, path traversal |
| Content | Every torrent file must exist in the HF tree at the pinned revision with the same size. HF's hash is the expected hash. If Pirate Face's SHA-256 disagrees, HF `main` is checked: same file there → accept, flag for reporting; different or unreadable → refuse as ambiguous | Torrents that smuggle in extra files; Pirate Face packaging a newer upload under an old revision label |
| Bytes | libtorrent piece hashes, then a file-level SHA-256 (LFS) or git blob SHA-1 against HF after the write cache is flushed; one failure blocks the infohash and deletes its data | Torrents that don't match the model |
| Policy | Code files and weight formats that can run code when loaded (pickle, Keras HDF5/.keras, NumPy .npz/.npy) are not selected by default; nothing downloaded is ever executed; licenses must be on the user's allowlist | Turning seeders into a malware distribution network |
| Disk | Budget = `min(budget, held + free − reserve)` | Filling the user's drive |

**Gated repos.** On a gated repo Hugging Face hides each LFS file's SHA-256 from anonymous clients
(the API returns asterisks) but still lists the git ID of the file's LFS pointer. The pointer is a
three-line text file that contains the SHA-256 and size, so Flotilla rebuilds it from Pirate Face's
SHA-256 and checks that it hashes to the ID Hugging Face lists. A match makes Pirate Face's hash as good
as Hugging Face's; a mismatch refuses the torrent.

If Hugging Face no longer has the repo (the case that matters most for preservation), the hash ledger's
record is the expected hash (`verify_source = "ledger"`), and Pirate Face's manifest is only
cross-checked. With no ledger record, Pirate Face's SHA-256 manifest is the only hash source. Such
candidates are labelled `verify_source = "pirateface-only"`. Editors can pin their infohash in the
signed feed so they still have a second, independent anchor. Pirate Face's hash is also the fallback for
a file that Hugging Face lists without publishing a hash.

## Hash ledger

`flotilla/data/ledger/` (`ledger.py`). One entry file per repo@revision lists every file's path, size
and hash (LFS SHA-256, git blob SHA-1, or on a gated repo the LFS pointer's git ID when no SHA-256 can
be confirmed). `index.json` maps each repo@revision to its entry file and the file's SHA-256, and is
signed with the feed editors' keys. Each entry has an OpenTimestamps proof (`.ots`) beside it.

- **Why one file per entry.** A file that is rewritten each time a model is added can only prove the
  date of its latest version. Entries are written once and never edited, so each proof keeps the date
  the hashes were first recorded. `ledger-capture` refuses to overwrite one, and `editor sign` refuses
  an index whose entries no longer match their digests.
- **Capture.** From HF's tree at the pinned revision. If HF hides an LFS SHA-256 (gated), Pirate Face's
  hash is recorded only if it reproduces HF's LFS pointer; otherwise the pointer's ID is, which still
  pins the content. A model HF has already removed is recorded from Pirate Face's manifest and labelled
  `"source": "pirateface"`: weaker evidence, but it can no longer change.
- **Loading.** The bundled copy, then the published copy from GitHub (or the last one that verified),
  in that order. Every copy that verifies adds entries; an entry file must hash to the index's digest.
- **Append-only.** `ledger-seen.json` in the state folder remembers the digest of every entry this node
  accepted, and accepted entries are cached by digest. A later index listing a different digest for a
  known repo@revision is a conflict: logged, shown by `flotilla ledger`, and the first entry stays. An
  index that stops listing an entry changes nothing, since the cached copy is used. So a stolen editor
  key can add models but can't rewrite ones nodes already hold records for. `flotilla ledger --accept`
  is the deliberate, per-node override.
- **Re-verification.** When a refresh changes a held file's expected hash (a ledger record arrives for
  a model only Pirate Face vouched for), the file goes back to unverified. If its bytes don't match,
  the usual rule applies: the torrent is blocked and its data deleted.
- **Timestamps.** Nodes don't check them: that would need a Bitcoin node or a block explorer at run
  time. They're for audits and disputes: when two records disagree, the one with the earlier Bitcoin
  date was made first, and only a record made before a takedown or a hack can be trusted to describe
  what was there.

**Why disagreements aren't fatal.** Pirate Face's manifest is a convenience field, not what peers
actually serve. Peers serve whatever the torrent's piece hashes commit to, and the finished file is
always graded against Hugging Face. A wrong Pirate Face manifest therefore can't make a node seed
bytes that don't match Hugging Face:

- if the swarm holds the genuine file, it verifies and seeds;
- if the swarm holds anything else, the check fails, the data is deleted and the infohash blocked.

Refusing outright would only save one download, at the cost of dropping a possibly genuine model over
a bookkeeping error. The HF `main` comparison covers the one ambiguous case: Pirate Face packaging a
newer upload under an old revision label.

Trees at a commit hash are cached forever (they're immutable). The `main` tree is never cached.

Resume data is never trusted for trackers or web seeds; they're always taken from the verified
`.torrent`.

## Probing: how we learn per-shard availability

Tracker counts only say how many peers have *everything*. A partial seeder announces as a leecher. To
see shards we read peer bitfields.

A session that wants nothing never learns anything: libtorrent (and most clients) drop "upload to
upload" connections before or right after the bitfield. The probe therefore uses a separate
short-lived session:

- it wants one file, the torrent's smallest, at priority 1, with a 4 KB/s per-torrent download limit
  and its own scratch folder that is deleted afterwards. One wanted file is enough to stay
  interested, and peers' bitfields cover every file whatever the probe wants. Wanting every file of
  a hybrid torrent made libtorrent set up v2 hash state for all of them: a probe of a few dozen
  500 GB+ models ran past 3 GB of memory;
- it samples `get_peer_info()` about once a second for 20 s, then every 5 s, and remembers each
  distinct peer's last bitfield (seeds are recorded as "has all");
- its peer id prefix (`-FP0100-`) is ignored by other probes. The node's main session uses
  `-FL0100-<node id>` so a node's own probe skips only itself, not other Flotilla seeders.

`availability(file) = max(observed seeds, tracker seeders − self) + partial peers holding every piece
of the file`.

This was tested on 127.0.0.1, with an earlier probe that wanted every file. An all-zero-priority probe
saw nothing; the throttled probe saw a full seeder (70/70 pieces) and a partial seeder (23/70), and
only about 1.5 KB of payload came down. The throttle also slows the handshake by roughly 2 s per peer,
which is irrelevant inside a 90 s window.

## Planner

Units are single weight files, or whole models smaller than `whole_model_under_gb`. Small metadata files
(config, tokenizer, LICENSE, README, index JSON; each ≤ 64 MB) always ride along with any shard of their
torrent. That keeps every partial seed useful and license-compliant.

```
rate(unit)  = priority × (scarcity(a, T) × risk + throughput)          # value per GB
scarcity    = 1/(1+a)            if a < T     (a = availability, T = target replicas)
            = 0.1/(1+a)          otherwise
risk        = 2.5  HF original gone   | 1.5 unknown | 1.0 HF still serves it
throughput  = 0.5 × min(1, leechers/(seeders+1)) × min(1, upload / 100 Mbit/s)
value       = rate × size_GB × (1.25 if whole model)  × (hold_bonus if already held)
density     = value / (size + boundary-piece overhead)
key         = density^spread × (−1 / ln u),  u = H(node_id ‖ infohash ‖ path) ∈ (0,1)
```

Units are taken in descending `key` order while they fit the budget and the swarm limit. Files held for
less than `min_hold_days` are taken first, unconditionally.

Why each piece is there:

- **Per-GB value.** Size neither helps nor hurts. A GB of a priority-0.9 model is worth 0.9 whether
  the model is 8 GB or 1.5 TB. Editors steer through priority, and the swarm steers through scarcity.
  An earlier per-unit (not per-GB) scoring let one 104 GB model crowd out every small model; the
  per-GB version spreads correctly.
- **Weighted rendezvous hashing.** `−w/ln u` makes the top pick land on unit *i* with probability
  `w_i / Σw`. Across many independent nodes, coverage therefore lands in proportion to need, and two
  volunteers starting at once don't both grab the same rarest shard. The draw is a pure function of the
  node id, so replanning on the same data gives the same answer (no churn). `spread` (default 3) sets
  how greedy a single node is: a 2× better unit wins about 89% of pairwise draws, 3× about 96%.
- **Feedback.** As nodes fetch, the next probe sees their bitfields, so scarcity falls and later
  nodes move on to other shards. Replanning is the only control loop; no coordinator is needed.
- **Upload speed** changes *what* you hold. A slow-upload node gets almost no throughput term, so it
  preserves rare shards. A fast-upload node also takes shards of busy swarms where its bandwidth
  matters.

Tested properties (`tests/test_candidates_planner.py`):

- the budget is respected and metadata is bundled;
- scarce shards beat common ones;
- lost-upstream models beat HF-backed ones;
- priority steers;
- 4 nodes × 20 shards out of 200 cover ≥ 55 distinct shards (≈69 expected), with stable draws;
- whole small models are taken whole;
- held files are sticky;
- the swarm limit holds.

## Engine

- **Main session.** Torrents are not auto-managed, so libtorrent's queue never pauses them. Rate-based
  choker, `anti_leech` seed choking (it favours peers that are just starting or nearly done, which
  spreads rare pieces), global up/down limits from the user's numbers, and UPnP/NAT-PMP only if
  enabled.
- **File priorities.** Selected files that nobody else has get priority 7 so they're fetched first.
  Other selected files get 4, and the rest 0.
- **Completion.** `file_completed_alert` triggers `flush_cache()`, and `cache_flushed_alert` triggers
  hashing in a worker thread. (Hashing straight away raced the disk writer: a file could look short. One
  delayed retry stays as a backstop.)
- **Dropping some files of a kept torrent.** Remove the torrent, delete those files, re-add without
  resume data (a recheck of what remains). This is rare, because held files are sticky.
- **Restart.** The verified `.torrent`, resume data (with the info dict) and per-file expected hashes
  are stored under the state dir. Seeding resumes before any network lookup. Anything mid-verification
  is re-queued.
- **Network failures** during a refresh never drop data: a held torrent whose listing is unreachable is
  kept as is (`Rejection.transient`).

## Dashboard

`flotilla run` serves one static page and a small JSON API on `127.0.0.1:7421`
(`[dashboard] port`). Anything on localhost is reachable from any browser tab, so:

- by default it listens on loopback only;
- the `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`, which defeats DNS rebinding;
- every POST needs a per-process token that's embedded only in the page, plus a JSON body, plus a
  same-origin `Origin` header if one is sent. Other sites can't read the page, so they can't get the
  token;
- there are no CORS headers, and a strict CSP is set.

Requests never touch libtorrent from the web thread. `Engine.submit()` validates them, they wait in a
queue, and the main loop applies them. Settings changes take effect immediately (rate limits) and are
written back to `config.toml`. A space change triggers a replan.

### On the network, for a node in a VPN container

A node inside a VPN container (gluetun and the like) should send its torrents through the VPN but keep
its dashboard on the home network. So the dashboard can run as a second process outside the
container, `flotilla dashboard --serve --listen 0.0.0.0`, that shares only the node's state folder
(`remote.py`):

- the engine writes what the dashboard shows to `dashboard-state.json` every 2 s, while a dashboard
  has asked for it in the last 30 s;
- the dashboard reads traffic history straight from `history.db`;
- actions go into `inbox/`, one small JSON file each. The engine passes them through the same
  `Engine.submit()` checks as its own dashboard's requests and writes the answer to `outbox/`.

The dashboard process opens no connection to the node, the VPN or the internet. Anyone who can write
to the state folder can already change the config, so the folder is the trust boundary.

Listening on anything other than loopback changes the checks, because the loopback ones no longer mean
anything:

- every request needs the dashboard's **access key**: 32 random bytes, made once and kept in
  `dashboard.key` (mode 0600) in the state folder. Opening `/?key=…` once sets it as an `HttpOnly`,
  `SameSite=Strict` cookie and redirects to drop it from the address bar. It is compared in constant
  time;
- any `Host` is accepted, since the node's address on the network isn't known in advance. A DNS
  rebinding site never gets the cookie, so it gets a 401;
- POSTs still need the per-process token, a JSON body and, if `Origin` is sent, one that matches the
  request's own `Host`.

The page and API are served over plain HTTP, so the key is only as private as the network it crosses.
It's meant for a home network, not the internet.

## Traffic history

`history.py`. Once a minute the engine adds up the bytes each torrent moved, from libtorrent's own
counters, not from samples of the current speed, so nothing between samples is lost. They go into
`history.db` (SQLite, WAL) in the state folder at two resolutions:

- per minute, kept for 3 days;
- per hour, kept for good: about 9,000 rows, or 1 MB, a year per model.

The node's own row also counts the seconds it was online and seeding, so time the computer was asleep
shows as a gap, not as zero traffic. Nothing in it leaves the machine.

## Memory

Seeding takes a few hundred MB. Downloading takes more: libtorrent keeps a v2 hash tree of 4-8 MB per
GB for every file it is downloading.

Pirate Face's hybrid torrents add a second cost. Their .torrent files leave out BEP 52's piece layers,
so libtorrent asks v2 peers for them, for every file a peer has, wanted or not, and holds each answer
in a full-size tree until the torrent is unloaded. A node in a 500 GB model's swarm grew by gigabytes
for shards it never downloads.

- **Compacting.** When a torrent is loaded, the trees in its resume data for files the node doesn't
  download are cut down to their piece layer (`_compact_trees`): about 2 KB per GB, and libtorrent
  doesn't ask for them again. A tree without the whole layer is emptied instead. Wanted files keep
  theirs.
- **The budget.** `[network] memory_gb`; by default a quarter of the machine's memory, or 70% of a
  container's limit, and never under 512 MB. A thread of its own checks the process's private memory,
  so a long refresh can't hold the check up. Process memory can only be read on Linux, so elsewhere
  the budget does nothing.
  - Above 70% of the budget, the hybrid torrent likely holding the most peer-supplied trees is
    reloaded, which compacts it: one torrent every 30 s, the same one again while its reloads keep
    freeing memory.
  - Over the budget, the least important model that would start new files starts none. Files it has
    started finish, which frees most of their memory, and everything it holds keeps seeding.
  - Under 70% again, the most important held-back model resumes, at most one every two minutes so
    it doesn't flap.

## Points ledger

Pirate Face's seeding points aren't live yet. Their published formula: 1 point per 3 seed-hours for a
10 GB torrent, scaled by √(GB/10), added hourly. Flotilla records, per torrent, seed-hours,
GB-hours of verified data, and a points estimate. It only counts time when the session isn't paused
and the torrent is in a seeding or finished state, and caps gaps at 10 minutes so sleep or crashes
don't inflate it. When Proof of Seed ships, this is the evidence a submitter will use, including for
partial (shard) holdings.

## Worth Rescuing sync

`worthrescuing.py`, an editor tool. Pirate Face's Worth Rescuing page has no API, so
`flotilla editor sync-worth-rescuing` reads the data embedded in its HTML: the historic shortlist and
the capability ranking. It writes entries into the priority list for the editor to review and sign.

- **Nodes never read the page.** Every model a node carries is still in the signed list, pinned to a
  commit, so a change to the page (or to how it's built) can't steer nodes.
- **It only touches its own entries**, marked `"via": "pirateface/worth-rescuing"`: it adds them,
  moves their pins and removes them when a model leaves the page. A model listed for another reason
  is left alone.
- **Official releases rank below at-risk models.** They get a `boost` of 0.45, under half the weight
  of the lowest at-risk model. The planner divides a file's value by 1 + its copies, so an official
  release loses to any at-risk file with one copy and fills in once those have two.

## Known limitations / next steps

1. **Credit for partial seeders.** Pirate Face counts only full seeders. Partial seeds announce with
   `left > 0`. Options on their side:
   - scrape-like per-file coverage from peers;
   - libtorrent's `distributed_copies`;
   - a tiny opt-in "holdings" ping keyed by infohash and file index.

   Any of these would also let nodes skip the probe.
2. **Pirate Face rebuilds** with a new infohash look like a new torrent. Files that are byte-identical
   could be adopted in place (same save path, recheck) instead of re-downloaded.
3. **Pure BitTorrent v2** torrents (no v1 piece layer) are skipped; hybrid ones are identified by
   their v1 infohash. v2's per-file Merkle roots would make shard seeding cleaner: no boundary pieces,
   and per-file hashes inside the torrent itself.
4. **Gated licenses** (`acceptFirst`) are never auto-picked. Supporting them would need the user's
   Pirate Face key and an explicit, recorded acceptance per license, as Pirate Face's own agent skill
   describes.
5. **Probe cost scales with candidates** (default cap 40): about 40 swarms × 90 s × ≤ 4 KB/s. Fine
   for now. With a coverage endpoint (item 1) the probe could be skipped entirely.
6. **Feed key management.** One key list compiled into the client. A threshold of 2-of-N editor
   signatures would be a cheap upgrade, and would let a second editor confirm each ledger record against
   Hugging Face independently.
