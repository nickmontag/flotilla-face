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
  `https://pirateface.co/api/ws/{repo}/{revision}/`, which redirects (through plain HTTP to a bare IP)
  to the file on Hugging Face at that commit. Flotilla drops it: see
  [Pieces from Hugging Face](#pieces-from-hugging-face).
- `GET /api/torrents?repo=` returns the magnet, infohash, pinned revision, seeder and leecher counts, a
  `hf` flag and a per-file SHA-256 manifest. `GET /api/torrents/{infohash}.torrent` returns the
  metainfo.
- `GET /api/feeds/new-torrents.xml` (RSS: the newest torrents with at most one seeder, the feed Pirate
  Face's own seeding agents follow), `/api/rescue` (rescue queue) and `/api/licenses` (with
  `acceptFirst` for licenses whose terms must be accepted). The old `/api/models` catalogue now returns
  a web page, so Flotilla no longer reads it.
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
                          verify on completion, resume data, replan every 6 h,
                          reorient (swap held pieces) on the user's schedule
                                   │  files no peer is sending
                                   ▼
          hfpieces.py — missing pieces from HF (netsafe.fetch_range), checked against
                        the torrent's piece hashes, handed to libtorrent (add_piece);
                        hfauth.py — the user's read-only HF token, for gated models only
```

## Trust model

The threats: someone in the middle (or a compromised mirror) gets a seeder to fetch unrelated or
malicious files, inflates or deflates priorities, replays an old feed, or makes a client fill its disk.

| Layer | Check | Defends against |
|---|---|---|
| HTTP | HTTPS only, certifi CA bundle, host allowlist (`pirateface.co`, `huggingface.co`; `hf.co` added for pieces), manual redirects re-checked per hop, no userinfo, no odd ports, byte caps on every response, the same network pin as torrent traffic (fails closed while it's down) | MITM, open redirects, memory exhaustion, leaking the real IP |
| Feed | Ed25519 minisign signature against pinned keys; `expires`; monotonic `version` stored locally | Forged priorities, freeze/replay, rollback |
| Feed pins | Optional `infohash` and `revision` per model; a listing that disagrees is rejected | A compromised index swapping the torrent for a pinned model |
| Hash ledger | Signed, append-only per-file hashes per repo@revision, captured from HF; a torrent file it doesn't list, a size or hash HF contradicts, or (HF gone) a revision it never recorded refuses the torrent | A compromised Pirate Face swapping files and manifest together after HF has removed the model |
| Magnet | Tracker hosts must be `tracker.pirateface.co`; web seeds `https` on the allowlist; any param other than `xt dn tr ws xl` (e.g. `x.pe`, `as`, `xs`) rejects the magnet | Steering the client to foreign trackers, seeds or payloads |
| .torrent | SHA-1 of the exact info-dict bytes must equal the listed infohash; its own `announce`, `announce-list` and `url-list` pass the same allowlist; BEP 17 `httpseeds` refused; path components sanitised (no `..`, `/`, `\`, `:`, NUL) | Swapped metainfo, path traversal |
| Content | Every torrent file must exist in the HF tree at the pinned revision with the same size. HF's hash is the expected hash. If Pirate Face's SHA-256 disagrees, HF `main` is checked: same file there → accept, flag for reporting; different or unreadable → refuse as ambiguous | Torrents that smuggle in extra files; Pirate Face packaging a newer upload under an old revision label |
| Bytes | libtorrent piece hashes, then a file-level SHA-256 (LFS) or git blob SHA-1 against HF after the write cache is flushed; one failure blocks the infohash and deletes its data | Torrents that don't match the model |
| HF pieces | Only for files peers aren't sending, of models HF has at the pinned commit; the reply must be exactly the byte range asked for; each piece's SHA-1 must equal the torrent's own v1 piece hash before `add_piece`; two mismatches stop HF for that file | HF, its CDN or someone in the middle serving bytes that don't match the torrent; one bad piece making libtorrent discard a whole v2 file |
| Magnet index | A build must hash to the listed infohash, hold only the license and dated shards, be small, and pass the tracker allowlist; shard lines must pass the magnet allowlist and name a 40-hex commit; an unreadable removals file disables the copy; metadata from the swarm must match the infohash; Hugging Face / ledger checks unchanged | A forged or tampered index steering nodes to other torrents, or bringing back a removed one |
| Replaced pins | Followed only when Pirate Face's record of the pin names the listed torrent as its successor, and Hugging Face or the hash ledger vouches for every file | A compromised index swapping a pinned model's torrent while nothing else anchors it |
| Pirate Face account | Only a key or seeding tracker address the user connected (`pirateface-key`, 0600, or `FLOTILLA_PF_KEY_FILE`, which the dashboard never writes; never `PIRATEFACE_KEY` or `pf`'s saved key); sent only to `pirateface.co` (`auth` is host-exact, never a redirect target) for `/api/trackers`; the seeding tracker must be `:6970/<token>/announce` on the tracker allowlist, and only its host is ever logged or shown | The key or the tracker token leaking; an answer steering announces to a foreign tracker |
| Per-file torrents | Pure v2, exactly one file whose pieces root and size match a verified file of ours, infohash as listed; trackers filtered to the allowlist, web seeds dropped; the added piece layer must hash up to the root; seeded in upload mode from our own copy | A listing pointing our upload at other bytes; foreign trackers or seeds; a bad piece layer misleading peers |
| HF account | Only a token the user connected (`hf-token`, 0600, or `FLOTILLA_HF_TOKEN_FILE`; never `HF_TOKEN`); whoami must show a read-only token (role `read`, or fine-grained with only `*.read` permissions) of a user; sent only after an anonymous request is refused, only to `huggingface.co` (never a redirect target); 401 on a download stops using it | A leaked token file changing the user's account or spending on it; the account being tied to public models it isn't needed for; the token reaching the CDN |
| Policy | Code files and weight formats that can run code when loaded (pickle, Keras HDF5/.keras, NumPy .npz/.npy) are not selected by default; nothing downloaded is ever executed; licenses must be on the user's allowlist | Turning seeders into a malware distribution network |
| Disk | Budget = `min(budget, held + free − reserve)` | Filling the user's drive |
| Updates | `flotilla update` installs only a release whose `release.json` carries the project key's minisign signature, with the trusted comment `flotilla-release <version>` (so a priority-list signature can't stand in); the wheel must have the SHA-256 it names; Docker images are pulled by the digest it names; only ever a higher version. The key stays offline: CI builds a draft release, the maintainer signs it. Nodes only check daily and say so; nothing installs unasked | A hijacked GitHub account or CI pushing code to nodes; a moved image tag; replaying an old release to downgrade |

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

Resume data is never trusted for trackers or web seeds: trackers are always taken from the verified
`.torrent`, and web seeds are dropped.

## Probing: how we learn per-shard availability

Tracker counts only say how many peers have *everything*. A partial seeder announces as a leecher. To
see shards we read peer bitfields. The same look counts real downloaders, for demand (see the
planner's notes below).

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

The probe drops web seeds too. With Pirate Face's web seed in, libtorrent showed it as a peer holding
every file, and a model with no seeder at all looked like it had one.

This was tested on 127.0.0.1, with an earlier probe that wanted every file. An all-zero-priority probe
saw nothing; the throttled probe saw a full seeder (70/70 pieces) and a partial seeder (23/70), and
only about 1.5 KB of payload came down. The throttle also slows the handshake by roughly 2 s per peer,
which is irrelevant inside a 90 s window.

## Planner

Units are single weight files, or whole models smaller than `whole_model_under_gb`. Small metadata files
(config, tokenizer, LICENSE, README, index JSON; each ≤ 64 MB) always ride along with any shard of their
torrent. That keeps every partial seed useful and license-compliant.

```
rate(unit)  = priority × (w_risk × scarcity(a, T) × risk + w_demand × demand × uplink)   # value per GB
scarcity    = 1/(1+a)            if a < T     (a = availability, T = target replicas)
            = 0.1/(1+a)          otherwise
            × 1.4 on a reachable node when a ≤ 1
risk        = 2.5  HF original gone   | 1.5 unknown | 1.0 HF still serves it
rate        = 0 for a file under a tenth here with no full copy seen, in a torrent HF wouldn't serve (24 h)
demand      = min(1, log2(1+d) / log2(33)) × d / (d + seeders)
              d = downloaders the probe saw (Flotilla nodes left out), or tracker leechers / 2
uplink      = 0.25 + 0.75 × min(1, upload / 100 Mbit/s)
(w_risk, w_demand) = at-risk (1, 0.5) | balanced (1, 1.5) | in-demand (0.25, 3)    # policy.focus
value       = rate × size_GB × (1.25 if whole model)  × (hold_bonus if already held)
density     = value / (size + boundary-piece overhead)
key         = density^spread × (−1 / ln u),  u = H(node_id ‖ infohash ‖ path) ∈ (0,1)
```

A plan is built in this order, each step taking units while they fit the budget and the swarm limit:

1. files picked less than `min_hold_days` ago that are coming in (a tenth or more has arrived),
   unconditionally;
2. space the user set aside for a model: what's held of it, then its other units by `key`;
3. everything else that's held, the user's own models first, then the highest `density` first (so when
   the space shrinks, the least valuable goes);
4. free space: unheld units in descending `key` order;
5. swaps, up to `churn_bytes` (see [Reorienting](#reorienting)).

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
  nodes move on to other shards. Replanning and reorienting are the only control loops; no coordinator
  is needed.
- **Upload speed** changes *what* you hold. A slow-upload node gets a quarter of the demand term, so it
  leans toward rare shards. A fast-upload node also takes shards of busy swarms where its bandwidth
  matters.
- **Demand is people, not leechers.** Pirate Face's tracker counts anyone without a full copy as a
  leecher, so every Flotilla node holding a few shards looked like demand, and more of them with every
  install. The probe counts downloaders itself: peers whose latest state is neither seed nor
  upload-only, leaving out Flotilla peer ids (`-FL`, `-FP`). It only sees peers it can connect to, which
  on a node behind NAT is roughly the demand it could serve. The count is log-scaled (one new
  downloader matters more at 1 than at 40) and shared with the seeders already there.
- **Focus** is the user's choice of `w_risk` and `w_demand`. In-demand keeps a little rarity as a
  tie-break, and the hold bonus and churn limit stop a change of focus from replacing everything held
  at once.

Tested properties (`tests/test_candidates_planner.py`):

- the budget is respected and metadata is bundled;
- scarce shards beat common ones;
- lost-upstream models beat HF-backed ones;
- priority steers;
- 4 nodes × 20 shards out of 200 cover ≥ 55 distinct shards (≈69 expected), with stable draws;
- whole small models are taken whole;
- held files are sticky;
- the swarm limit holds;
- with no churn allowed, what's held stays; with some, the lowest-value held units go first, no more
  than allowed, and never a model the user chose.

### Reorienting

A node used to be free to drop any held file at every 6-hourly replan, held back only by `hold_bonus`
and `min_hold_days`. Now the user sets how often it happens and how much moves: `[reorient] every`
(`daily`, `weekly`, `monthly`, `never`) and `churn_gb`.

- **The swap step.** Unheld units are tried in `key` order (the node's own weighted draw, so nodes
  that reorient on the same day don't all fetch the same shard). For each, held units are taken as
  victims in ascending `density` until it fits. A victim must be worth less than the unit coming in:
  its `density` includes `hold_bonus`, so a swap has to be 1.5× better by default. The bytes dropped
  count against `churn_bytes`, and a swap that would go past it is skipped, never trimmed: the limit is
  a hard one. `Plan.churn_short` records the smallest swap that was skipped for that reason, so the
  dashboard can say that the churn size is smaller than one piece.
- **Never victims.** Files picked less than `min_hold_days` ago once a tenth of them has arrived (a
  file nobody is sending holds no space for a week), and models the user chose by hand
  (`Feed.manual`: models in their own list file, and models with space set aside). Reordering the list
  is not choosing a model: it changes priorities, which the next reorientation follows.
- **The user's own models don't wait.** An unheld unit of a `manual` model may displace victims at any
  replan, and doesn't count against `churn_bytes`. Otherwise a model added to a full node would wait a
  month.
- **The schedule.** `state.json` keeps `reorient.last`. A replan passes `churn_bytes = churn_gb` when
  `last + period` has passed or the user pressed Reorient now, and 0 otherwise. A new node's first
  plan sets `last`, so its first reorientation comes one period after it starts. The look at the swarm
  that follows is brought forward to the moment one falls due.
- **Said ahead of time.** After each plan the engine runs the planner once more with the full churn
  size and keeps the result (`reorient_preview`): the units that would go out and come in, by model.
  The planner is a pure function, so the preview is what the reorientation will do if the swarm looks
  the same then. `reorient.result` in `state.json` keeps what the last one did.
- **What still happens between reorientations.** Free space is filled; a smaller budget drops the
  least valuable held units; a model that leaves the candidates (skipped, denied, blocked, licence no
  longer allowed) is dropped. None of that is churn.

## Engine

- **Main session.** Torrents are not auto-managed, so libtorrent's queue never pauses them. Rate-based
  choker, `anti_leech` seed choking (it favours peers that are just starting or nearly done, which
  spreads rare pieces), global up/down limits from the user's numbers, and UPnP/NAT-PMP only if
  enabled. A node bound to a VPN runs a `vpn-port` thread instead (vpnport.py): it finds the port the
  VPN forwards (gluetun's control server, NAT-PMP to the tunnel's gateway, `piactl`, a file), moves the
  listen socket there with `apply_settings`, sets `announce_port` when the VPN maps a different public
  port, and re-announces so the swarm learns it at once.
- **Reachability.** The once-a-minute peer scan counts connections peers started (from internet
  addresses, and only after a block traded: LAN peers and DHT crawlers don't count) and ones this node dialed. No inbound peer for `REACHABLE_FOR`
  (3 days) and no forwarded port: the node is unreachable, applies `DIAL_OUT` (faster, more outgoing
  connections, uTP on for BEP 55 hole punching, seed-to-seed connections kept so a seed connected to
  unreachable leechers can pass them on by PEX and relay the hole punch) and force-reannounces every 15 min, within the
  tracker's own minimum interval. A reachable node plans with `Params.reachable`, which weights
  scarcity ×1.4 for files with at most one other copy (below `hold_bonus`, so a change in reachability
  alone doesn't swap held files).
- **Housekeeping cadence.** Every 10 s the main loop settles upload totals and seed-hours. Torrent
  status is read with only the accurate byte counters (`STATUS_FLAGS`), and one read per torrent is
  shared for 2 s by everything that asks. `state.json` is written every 5 minutes, at once after a plan,
  a dashboard action or a file verdict, and on shutdown; `status.json` every minute. The dashboard page
  polls every 3 s only while its tab is visible, and its snapshot is shared for 1.5 s between tabs.
- **File priorities.** Selected files that nobody else has get priority 7 so they're fetched first.
  Other selected files get 4, and the rest 0.
- **Completion.** `file_completed_alert` triggers `flush_cache()`, and `cache_flushed_alert` triggers
  hashing in a worker thread. (Hashing straight away raced the disk writer: a file could look short. One
  delayed retry stays as a backstop.)
- **Dropping some files of a kept torrent.** Save its resume data, remove the torrent, delete those
  files, clear their pieces in the resume data (with any piece a kept file shares, fetched again) and
  re-add. Nothing that remains is re-hashed; without usable resume data it falls back to a full
  recheck. This happens at a reorientation, or when the space shrinks.
- **Restart.** The verified `.torrent`, resume data (with the info dict) and per-file expected hashes
  are stored under the state dir. Seeding resumes before any network lookup. Anything mid-verification
  is re-queued.
- **Network failures** during a refresh never drop data: a held torrent whose listing is unreachable is
  kept as is (`Rejection.transient`).

## Pieces from Hugging Face

`hfpieces.py`, on its own thread. Off until the user turns it on (`hf_fallback`: asked in `flotilla init`,
default no; a switch in the dashboard's settings, read live). It replaces Pirate Face's web seed, which
libtorrent can't keep using:

- The web seed redirects through Hugging Face to a signed CDN link that expires after an hour.
- libtorrent's `web_peer_connection::handle_redirect` adds the redirect target as a new web seed for
  that one file, and clears the file from the original web seed's `have_files`, so the original is
  never asked for it again. When the signed link expires, every request gets a 403, `handle_error`
  clears the file there too, and nothing serves the file until the torrent is added again.
- On a fresh node (Nov 2026) every file still downloading from the web seed stopped at the same
  minute, 61 minutes in.

So `_atp` drops `url-list` and `httpseeds` before libtorrent sees the torrent (they're outside the
info dict, so the infohash is unchanged), resume data gets no web seeds, and Flotilla does the job
itself, only where it's needed:

1. **Stuck.** Every 30 s it records each wanted file's progress. A file is stuck when peers sent it
   less than 32 KB/s over the last 10 minutes (bytes from Hugging Face don't count) and libtorrent
   wasn't using more than 80% of the download limit (a full line isn't the swarm's fault). Paused
   torrents, files at priority 0 (held back for memory), a disk below its reserve and a paused session
   don't count.
2. **Eligible.** The holding's `verify_source` is Hugging Face (its tree at the pinned commit matched
   the torrent), the revision is a full commit hash, and the torrent has v1 piece hashes.
3. **Fetch.** Missing pieces of the most important stuck file first, rarest first (by
   `piece_availability()`, pieces in libtorrent's download queue last), one piece at a time. Each piece
   maps to file ranges (`map_block`); pad files are zeros, and the rest are byte-range requests to
   `huggingface.co/{repo}/resolve/{commit}/{path}` through `netsafe.fetch_range`. The signed link it
   redirects to is reused until 5 minutes before its `Expires`, and a 4xx from the CDN gets a new one.
4. **Check.** SHA-1 of the piece must equal `hash_for_piece()` before `add_piece()`. This matters more
   than it looks: Pirate Face's v2 torrents have no piece layers, so libtorrent can only check a v2
   file as a whole, and a bad last piece makes it fail every piece of the file (measured on 2.1.1).
   libtorrent then checks the piece again and seeds it. The file-level SHA-256 check against HF runs as
   for any other file.
5. **Limits.** Only the bandwidth libtorrent leaves free under the download limit (never below
   256 KB/s), and `hf_gb_per_day` a UTC day, counted in `state.json`. A 404/410 from `huggingface.co`,
   or "Access to this resource is disabled", stops for that model for the run; a 429, a 5xx or a
   network error backs off for `Retry-After`, or 1 minute doubling to 15. A 401/403 is a gated or
   private model: see 7.
6. **Accounting.** `add_piece` bytes aren't in libtorrent's download counters, so the history adds
   them, the holding keeps `hf_bytes`, and the dashboard shows today's total, the rate and the files
   being fetched.
7. **Gated models** (`hfauth.py`). Every request goes without a token first. When `huggingface.co`
   answers 401/403, the token the user connected with `flotilla hf connect` is checked once with
   `/api/whoami-v2` (the first time it's needed, so HF doesn't learn the account runs Flotilla until a
   gated model needs it) and the request is repeated with it; from then on that repo's `huggingface.co`
   requests carry it (`fetch_range` never sends it to a redirect target). A token whoami says can do
   more than read (role `write`, or a fine-grained permission not ending in `.read`) or that isn't a
   user's is never used. With the token, 403 `GatedRepo` means the account isn't let in yet, and a
   401 without `GatedRepo`/`RepoNotFound` means the token was revoked: it's dropped until the file
   changes. Either way the model is skipped and asked about again in an hour (`GATED_RECHECK`), and
   listed in the dashboard and `status.json` (`hf_gated`) with a link to request access. The token
   file is re-read when it changes, so connecting, replacing or disconnecting (also from the
   dashboard: `hf_disconnect`) needs no restart and retries gated models at once.

## Dashboard

`flotilla run` serves one static page and a small JSON API on `127.0.0.1:7421`
(`[dashboard] port`). Anything on localhost is reachable from any browser tab, so:

- by default it listens on loopback only;
- the `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`, which defeats DNS rebinding;
- every POST needs a per-process token that's embedded only in the page, plus a JSON body, plus a
  same-origin `Origin` header if one is sent. Other sites can't read the page, so they can't get the
  token;
- there are no CORS headers, and a strict CSP is set;
- a connection that sends nothing for 30 s is closed, so idle or half-sent requests can't pile up
  threads.

Loopback keeps other machines and other sites out, not other accounts on the same computer: any
local user can open the page, read its token and steer the node. That's fine on a computer one
person uses; on a shared server, run the dashboard with `--listen` and its access key instead, or
not at all.

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
`history.db` (SQLite, WAL) in the state folder at three resolutions:

- per minute, kept for 3 days;
- per hour, kept for 90 days;
- per day after that, for good: 365 rows a year per model. Rolled-up days follow UTC midnight.

An index on `(res, t)` keeps the per-model totals from scanning every row.

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
  so a long refresh can't hold the check up. Private memory is `RssAnon` on Linux and the task's
  `phys_footprint` on macOS (what Activity Monitor shows as Memory); neither counts the files
  libtorrent maps from disk. Where neither can be read, the budget does nothing.
  - Above 70% of the budget, the hybrid torrent likely holding the most peer-supplied trees is
    reloaded, which compacts it: one torrent every 30 s, the same one again while its reloads keep
    freeing memory.
  - Over the budget, the least important model that would start new files starts none. Files it has
    started finish, which frees most of their memory, and everything it holds keeps seeding.
  - Under 70% again, the most important held-back model resumes, at most one every two minutes so
    it doesn't flap.

## Points ledger

Pirate Face's published formula: 1 point per 3 seed-hours for a 10 GB torrent, scaled by √(GB/10).
Flotilla records, per torrent, seed-hours, GB-hours of verified data, and a points estimate. It only
counts time when the session isn't paused and the torrent is in a seeding or finished state, and caps
gaps at 10 minutes so sleep or crashes don't inflate it. It's Flotilla's own count; Pirate Face counts
the real points through each account's seeding tracker (below).

## Seeding credit and per-file swarms

`pfaccount.py`, `perfile.py`, `v2hash.py`.

**Seeding credit.** `/api/trackers`, asked with an account's key, ends with that account's own tracker
(`http://tracker.pirateface.co:6970/<token>/announce`). Torrents that also announce to it count toward the
account's points. With a key connected, the engine adds it, in a tier of its own, to every model and
per-file torrent, and sets `announce_to_all_tiers` so the public trackers keep hearing from it too.
Trackers are outside the info dict, so no infohash changes. The key file holds either the key or the
tracker address itself (copied from the account's settings page): an address is used as it is, since
nothing can check it. A thread re-reads the file every minute, at once when the dashboard saves it, and
asks for a key's tracker every 12 hours; while Pirate Face can't be reached it keeps the tracker it has.
The dashboard's connect request carries the secret, so a separate dashboard hands it over in an inbox
file only this user can read; errors never repeat what was pasted.

**Per-file swarms.** Pirate Face lists a pure v2 torrent per file of its hybrid torrents
(`/api/files/<pieces root>`), shared by every upload with the same bytes. A node holding some shards is a
leecher in the model's swarm but a complete seeder in each shard's own. A worker thread, for each
verified file of 1 MB or more in a hybrid torrent: looks up its swarm (one lookup a second; "not listed"
is remembered for a day), fetches the torrent and checks it (pure v2, exactly one file with this pieces
root and size, the infohash the listing named), keeps only allowlisted trackers, drops web seeds, and
adds the piece layer Pirate Face leaves out. The layer comes from the leaf hashes libtorrent saved in the
model's resume data (libtorrent keeps a downloaded or checked file's block hashes, not its piece
layer; they're folded up per piece, for whatever piece length the per-file torrent uses), or from hashing the file when those aren't there and it wasn't verified in the
last two hours. Either way it must hash up to the pieces root. The main loop adds the result in seed mode
and upload mode with the file renamed to the model's own copy, so it's never downloaded, copied or
written; libtorrent checks each piece against the tree the first time a peer asks for it. Per-file
torrents are taken out before `_remove` or `_evict_files` deletes their file, and when the file stops
being verified. Their uploads count as the model's in the history, the monthly cap and the upload-peer
count. Their torrents (with piece layers) are kept under `state/per-file/`, so a restart doesn't look them
up or hash them again.

## Replaced torrents

`/api/torrents/<infohash>` gives Pirate Face's record of a torrent, `supersededBy` included. A signed pin
that the listing no longer matches is followed to its successor only if that record (followed for up to
three hops) leads to the listed infohash, and only if Hugging Face or the hash ledger vouches for every
file: the pin was an anchor independent of Pirate Face, and they take its place. Separately, whenever a
held model's listing moves to another torrent of the same repo (a repack or a new revision), the engine
moves each verified file whose path, size and expected hash the new candidate shares into the new
torrent's folder before planning (`_carry_over`). The new torrent finds them when libtorrent checks it,
and they're verified against the new candidate's hashes like any download; the old torrent and anything
left of it are then removed.

## Magnet index

`magnetindex.py`. The index (`pirateface/magnet-index`) is a listing like any other: once a day the
refresh asks for it, and a new build's .torrent must hash to the listed infohash, hold only `LICENSE` and
`shards/[removed-]YYYY-MM-DD.txt`, stay under 256 MB and pass the tracker allowlist. It's seeded from
`state/index/files`; a new build is added over the old one's folder (it only downloads new shards) and
the old one is let go once the new one is complete. Shards are read like Pirate Face's CLI reads them:
dates in order, each date's removals applied after its shard, the last line for a repo winning, and a
removals file that isn't all 40-hex lines makes the whole copy answer nothing. A line counts only if it
passes `validate_magnet` and has exactly one web seed naming `api/ws/<owner>/<name>/<40-hex commit>`.
When `/api/torrents` can't be reached, `build_candidate` takes the listing from the copy (no Pirate Face
manifest) and the .torrent from the swarm: BEP 9 metadata in a short-lived session with the probe's
settings, the magnet's trackers added back, checked against the infohash, then cached. Every later check
runs unchanged; a pin the copy disagrees with is a transient rejection, since the replacement can't be
asked about.

## Rescue requests

`flotilla add` (asking first, or `--rescue`) and the dashboard's button send `POST /api/rescue
{"repoId": ...}`, as Pirate Face's CLI does: the model's name only, no account. `netsafe.post_json` uses
the same host allowlist and network pin as `fetch` and never follows a redirect.

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

## The fallback list

`fallback.py`, an editor tool for when the hand-picked models are well seeded. Once they have a few
copies each, a node's spare space should go to the models most at risk of takedown that people
actually use: the refusal-removed builds (abliterated, heretic, obliterated, uncensored) of strong
models. `flotilla editor sync-fallback`:

1. **Searches Hugging Face** for those terms, most downloaded in the last month first. Only language
   and vision-language models count: image, video and audio generators and LoRA adapters are left out.
2. **Groups each build with its quantisations**, through Hugging Face's "quantized" links and by name
   within one publisher, and ranks the groups by their combined downloads. Hugging Face has thousands
   of these, mostly re-quantisations of a few weights.
3. **Keeps at most 2 builds per base model**, so one popular base doesn't take every place.
4. **Picks one copy per group** that Pirate Face has a .torrent for, preferring the full-precision
   release (anyone can re-quantise from it), and pins it to Pirate Face's commit. The license must be
   allowed by default, and so must the base model's license when the build relabels a custom one.
5. **Writes at most 40 entries** (`--max`), marked `"via": "huggingface/refusal-removed"`, and removes
   its own entries that dropped out. Entries on the list for another reason are left alone.

**Where they rank.** Each entry's capability is its base model's score on the list (60 if the base
isn't scored), with cyber 40 and takedown risk 80 (70 when the name says only "uncensored", which may
mean fine-tuning). The boost runs from 0.30 at 1k downloads a month to 0.40 at 1M. That puts them at a
priority of about 0.2-0.3: around the official releases, about half the lowest hand-picked model
(0.56), and above Pirate Face's unranked new torrents (0.15). The planner divides a file's value by
1 + its copies, so these fill in once the hand-picked models have about two copies each, and take
over completely once those reach their target (where their value drops to a tenth).

**Why it's built by an editor and signed.** Nodes never search Hugging Face. Every model a node carries
is still reviewed and pinned to a commit, every node sees the same pool (so the planner's per-node draw
spreads copies across the fleet instead of every node picking the same models), and a thousand nodes
don't each query Hugging Face and Pirate Face for hundreds of models.

## Unlisted models

With `policy.include_unlisted` on (the default), a node also looks at Pirate Face's rescued models and
its new torrents that need seeders, least-seeded first, at priority 0.15. Everything on the list and
everything held is always looked at; `policy.max_candidates` (default 20) caps only these extras,
since each costs Pirate Face lookups on every refresh. (Until November 2026 the cap counted the list
too, so a list longer than 40 models crowded these out entirely.)

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
5. **Probe cost scales with candidates** (the list plus up to 20 unlisted): about 40 swarms × 90 s × ≤ 4 KB/s. Fine
   for now. With a coverage endpoint (item 1) the probe could be skipped entirely.
6. **Feed key management.** One key list compiled into the client. A threshold of 2-of-N editor
   signatures would be a cheap upgrade, and would let a second editor confirm each ledger record against
   Hugging Face independently.
