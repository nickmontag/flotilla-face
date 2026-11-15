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

## Not in scope

- The contents or behaviour of the models themselves. Flotilla only verifies that bytes match what
  was published.
- Takedown or licence requests for a model: send those to Pirate Face (https://pirateface.co/takedown)
  and to the feed's editors. Editors can add any repo or infohash to the feed's `deny` list, which
  makes every node drop it.
- Peers seeing your IP address: that's how BitTorrent works. Use `bind_interface` with a VPN if that
  matters to you.
