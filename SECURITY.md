# Security policy

Flotilla downloads large files from strangers and runs unattended, so its trust chain matters.
Please report weaknesses privately.

## Reporting

Use GitHub's **Report a vulnerability** button (Security → Advisories) on this repository. Please
include what an attacker controls, what they can make a Flotilla node do, and steps to reproduce. You'll
get an acknowledgement within a week.

Please don't open a public issue for a vulnerability until a fix is released.

## In scope

- Making a node download or seed bytes that don't match the Hugging Face, hash-ledger or pinned hashes,
  or making `flotilla verify <folder>` pass a folder that doesn't match the ledger.
- Getting a node to talk to hosts outside its allowlist, or to trackers or web seeds not on it.
- Forging, replaying or rolling back the signed priority feed.
- Forging the hash ledger, or getting a node to replace a ledger record it has already accepted.
- Path traversal or writes outside the seed folder.
- Leaking traffic outside `bind_interface` when it's set.
- The local dashboard: reaching it from another origin or machine, CSRF, DNS rebinding.
- Filling the disk past the configured reserve.

## Known limits

- The local dashboard keeps out other machines and other websites, not other accounts on the same
  computer: any local user can open `127.0.0.1:7421` and steer the node. On a shared server, use
  `flotilla dashboard --serve --listen` with its access key, or don't run the dashboard.
- The network dashboard (`--listen`) is plain HTTP. Its access key is only as private as the network
  it crosses, so it's meant for a home network, never the open internet.
- Browsers send a host's cookies to every port on it, so other web apps on the same host (another
  container's web UI on the same server, say) receive the dashboard's cookie and could read it. The
  cookie holds a value derived from the access key, not the key, and that value changes every 30 days
  (the previous one still works, so an active browser is never logged out). Until it changes, though,
  anyone who reads it can use it to open the dashboard. Don't serve the dashboard on a host name shared
  with web apps you don't trust.
- Anyone who can reach the dashboard's port can hold some of its connections open before a key is
  checked. Each request must arrive within 30 seconds and at most 32 connections are open at once, so
  this can't exhaust the node's threads or files, but it can keep the dashboard itself busy.
- The check that turns away visitors from public internet addresses sees the address the connection
  comes from. Where the container runtime rewrites that (Docker Desktop on macOS and Windows, rootless
  Docker or Podman), every visitor seems to come from a private gateway address and passes it, and only
  the access key keeps them out. Publish the dashboard's port on a home-network or loopback address
  only, as setup does.
- `install.sh` installs the newest GitHub release's wheel (or `FLOTILLA_REF` / `FLOTILLA_PACKAGE`, if you
  name one) and never falls back to `main`. That first install trusts GitHub and HTTPS: it doesn't check
  the release's signature. `flotilla update` does, for every later release, before installing it.

## Not in scope

- The contents or behaviour of the models themselves. Flotilla only verifies that bytes match what
  was published.
- Takedown or licence requests for a model: send those to Pirate Face (https://pirateface.co/takedown)
  and to the feed's editors. Editors can add any repo or infohash to the feed's `deny` list, which
  makes every node drop it.
- Peers seeing your IP address: that's how BitTorrent works. Use `bind_interface` with a VPN if that
  matters to you.
