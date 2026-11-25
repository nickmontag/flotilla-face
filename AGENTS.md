# Installing Flotilla Face for someone

This is for an agent (or a person scripting it) installing Flotilla Face on someone's computer. It
covers what to ask them, what to check, the commands, and how to tell it worked. README.md explains
what Flotilla does; this file is the procedure.

## 1. Ask the person first

Some of this is theirs to decide, and some of it exposes them. Ask, in plain words, and don't guess:

1. **Do they want to seed?** Flotilla downloads AI models and shares them with strangers over
   BitTorrent: the models most at risk of being taken down, which include uncensored builds and
   models tuned for cybersecurity work. It uses disk space and upload bandwidth around the clock.
2. **Do they use a VPN on this machine?** Every peer in every swarm sees the address Flotilla seeds
   from. Without a VPN that's their home's public IP address, listed as seeding these models. Only
   pass `--seed-from-home-ip` if they say, explicitly, that this is fine.
3. **Where, and how much space?** A folder on a drive with room, and how many GB to give. The default
   is half that drive's free space.
4. **Their upload and download speed**, in megabits per second, and any monthly data cap on their
   internet plan. Defaults are 10 up, 100 down, no cap.
5. **Optional: a Pirate Face account key** (to earn points) and **a Hugging Face read-only token** (for
   gated models). Both are secrets: have them run `flotilla points connect` and `flotilla hf connect`
   themselves, which ask for the value, or give you a file path for `--from-file`. Never put a key on a
   command line, in a log, or in your reply.

## 2. Check the system

| System | Supported | How |
| --- | --- | --- |
| macOS 14+ on Apple Silicon, macOS on Intel | yes | installer, then a launchd service |
| Linux x86_64 or arm64 with glibc and systemd (Ubuntu, Debian, Fedora, Arch, Raspberry Pi OS 64-bit) | yes (CI tests Ubuntu) | installer, then a systemd user service |
| Linux without systemd (some containers, OpenRC, runit) | partly | installer works; run `flotilla run` under their own supervisor, or use Docker |
| Alpine and other musl Linux | no | Docker (see README, "Behind gluetun") |
| Windows | no | WSL2 or a Linux VM, with the VM caveats below |

Run `uname -s; uname -m` and, on Linux, `ldd --version` (musl means no) and `systemctl --user status`
(an error means no systemd user session).

**Servers, VMs and seedboxes are a good place for it**: always on, with a disk of their own (a Proxmox
VM or LXC container, a home server, a rented seedbox or dedicated server). The one rule is that the VPN
has to fail closed somewhere the seeding traffic can't get around. Any of these does:

1. A VPN inside the machine (a WireGuard client, or gluetun in Docker): `init` pins to it.
2. A router or firewall (OPNsense, pfSense) that sends this machine's traffic through a VPN and blocks
   it when the VPN is down. `init` can't see that VPN and says "no VPN found"; `--seed-from-home-ip` is
   right here, once the person confirms the kill switch.
3. A rented seedbox or server, whose address is the provider's, not anyone's home. `--seed-from-home-ip`
   is right here too, once the person confirms the provider allows torrents (seedbox hosts do; many
   VPS providers don't).

Not fine: a VPN app on the computer that hosts the VM (or Windows, under WSL), with nothing at the
network level. The VM can't see it, and when it drops, the VM's traffic leaves through the home
connection. `init` detects a VM or container and says so; don't use `--seed-from-home-ip` to get past
"no VPN found" in that case. Also: an LXC container needs `/dev/net/tun` passed through for a VPN
inside it; Alpine containers won't work (musl); and a VM's disk should have real space behind it (a
pre-allocated image, a passed-through disk or a ZFS dataset), since Flotilla only sees the guest's.

**VPNs.** `flotilla init` pins Flotilla to the VPN it finds, so if the VPN drops, Flotilla goes offline
instead of leaking. It finds full-tunnel VPNs, split-tunnel WireGuard and the common apps (NordLynx,
ProtonVPN, Mullvad, Windscribe). Tailscale and ZeroTier don't count as VPNs. On macOS the pin is the
VPN's address: a VPN that hands out a new address on each reconnect (many OpenVPN setups) leaves
Flotilla offline until `flotilla init --yes` runs again. Tell the person if theirs does.

**gluetun.** If a VPN container such as gluetun runs on the machine, the best setup puts Flotilla
inside its network. That needs the interactive `flotilla init` or the docker commands in README
("Behind gluetun"); `init --yes` doesn't do it and says so.

## 3. Install

```bash
curl -fsSL https://raw.githubusercontent.com/nickmontag/flotilla-face/main/install.sh | FLOTILLA_YES=1 sh
```

`FLOTILLA_YES=1` installs only, with no questions. It installs uv if needed, then the newest signed
release, into the user's own environment. No sudo. The command lands in `~/.local/bin/flotilla`; use
that path if a new shell isn't open yet.

## 4. Set up

```bash
flotilla init --yes --folder ~/flotilla --budget-gb 500 --upload-mbps 40 --download-mbps 300
```

Every answer comes from a flag, else the current settings, else the default `flotilla init` offers.
`flotilla init --help` lists them all. The ones that matter most:

| Flag | Default | |
| --- | --- | --- |
| `--vpn auto\|none\|<interface or address>` | `auto` | `auto` pins to the VPN found |
| `--seed-from-home-ip` | off | only with the person's explicit yes (step 1) |
| `--budget-gb`, `--reserve-gb` | half the free space, 20 | disk to use, disk to always leave free |
| `--upload-mbps`, `--download-mbps`, `--upload-share`, `--monthly-upload-cap-gb` | 10, 100, 0.8, none | |
| `--focus at-risk\|balanced\|in-demand` | `at-risk` | what to seed first |
| `--dashboard local\|lan\|tailnet\|public` | `local` | never `public` unless they ask for it |

Exit codes: **0** saved and ready to start; **3** saved but not to be started (no VPN to pin to, and no
`--seed-from-home-ip`); **2** a flag was wrong, nothing saved. On 3, tell the person why and stop.

## 5. Start it

```bash
flotilla service install            # now and at every login
flotilla service install --linger   # Linux: also while they're logged out (servers)
```

If `service install` fails on Linux, the reason is printed (usually no systemd user session): start it
with `flotilla run` under whatever supervises their programs, or use Docker.

## 6. Check it works

```bash
flotilla status          # running, pinned to the VPN (or not), what it holds and is fetching
flotilla plan            # what this node would seed, with no downloading
flotilla dashboard --url # the dashboard's address
```

If another torrent client already holds Flotilla's port, `flotilla status` shows a `moved` line (fine:
it listens on the next free port) or a `TAKEN` line (the VPN's forwarded port is held by the other
client, so peers can't connect in: tell the person, and give the other client a different port).

The dashboard address includes an access key: give it to the person, not to anyone else. Downloads
start within a few minutes; filling the space takes hours to days.

## Later

- `flotilla update` installs a newer signed release when there is one (it checks the signature).
- `flotilla init --yes ...` again changes settings; anything not passed keeps its current value.
- `flotilla uninstall` stops it; `--delete-data` also removes the seeded files and its state.

## Don'ts

- Don't run any of this as root or with sudo.
- Don't pass `--seed-from-home-ip` without the person's explicit agreement, and never as a workaround
  for "no VPN found".
- Don't set `--dashboard public` unless they ask: anyone on the internet could reach it.
- Don't handle their Pirate Face key or Hugging Face token yourself if they can enter it.
