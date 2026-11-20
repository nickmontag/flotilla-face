#!/bin/sh
# Flotilla Face installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/nickmontag/flotilla-face/main/install.sh | sh
#
# What it does: checks your system, installs uv (Astral's Python package
# manager) if you don't have it, installs Flotilla with Python 3.13 into its own
# isolated environment, then walks you through setup and offers to run
# Flotilla in the background. Nothing needs sudo.
#
# It installs the newest release. Later, `flotilla update` installs newer ones, checking each
# against the project's signature first.
#
# Environment variables:
#   FLOTILLA_REF=...            a tag, branch or commit of this repo to install instead of the newest
#                               release, e.g. main (a commit hash pins exactly what you run)
#   FLOTILLA_PACKAGE=...        what to install instead of this repo (a git URL, wheel or folder)
#   FLOTILLA_YES=1              no questions: install only, set up later with `flotilla init`
#   FLOTILLA_NO_MODIFY_PATH=1   don't add ~/.local/bin to your shell profile
set -eu

# Straight from GitHub (no package index): the newest release's wheel, or the source of FLOTILLA_REF.
REPO="nickmontag/flotilla-face"
REF="${FLOTILLA_REF:-}"
PYTHON_VERSION="3.13" # libtorrent publishes no wheels for 3.14+ yet

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != dumb ]; then
  B=$(printf '\033[1m') C=$(printf '\033[36m') D=$(printf '\033[2m') G=$(printf '\033[32m') R=$(printf '\033[0m')
else
  B="" C="" D="" G="" R=""
fi
say() { printf '%s\n' "$*"; }
step() { printf '%s\n' "${D}→${R} $*"; }
ok() { printf '%s\n' "${G}✓${R} $*"; }
die() { printf 'flotilla install: %s\n' "$*" >&2; exit 1; }
case "$REF" in
  *[!A-Za-z0-9._-]*) die "FLOTILLA_REF must be a tag, branch or commit (letters, digits, . _ -)." ;;
esac

# --- 1. Is this system supported? ------------------------------------------
os=$(uname -s)
arch=$(uname -m)
case "$os" in
  Darwin)
    ver=$(sw_vers -productVersion)
    major=${ver%%.*}
    if [ "$arch" = arm64 ] && [ "$major" -lt 14 ]; then
      die "Apple Silicon Macs need macOS 14 or newer (this one runs $ver)."
    fi
    if [ "$arch" = x86_64 ] && [ "$major" -lt 15 ]; then
      say "Note: Intel Macs before macOS 15 get libtorrent 2.0, which works fine."
    fi
    ;;
  Linux)
    case "$arch" in
      x86_64 | amd64 | aarch64 | arm64) ;;
      *) die "unsupported CPU: $arch. Flotilla needs x86_64 or arm64 (64-bit Raspberry Pi works)." ;;
    esac
    if (ldd --version 2>&1 || true) | grep -qi musl; then
      die "musl-based Linux (such as Alpine) isn't supported: libtorrent's packages need glibc."
    fi
    ;;
  *) die "unsupported system: $os. Flotilla runs on macOS and Linux." ;;
esac
command -v curl > /dev/null 2>&1 || die "curl is required."

# --- 2. uv ------------------------------------------------------------------
if ! command -v uv > /dev/null 2>&1; then
  for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    if [ -x "$d/uv" ]; then PATH="$d:$PATH"; export PATH; break; fi
  done
fi
if ! command -v uv > /dev/null 2>&1; then
  step "Installing uv, the Python package manager Flotilla uses (from astral.sh)"
  if [ "${FLOTILLA_NO_MODIFY_PATH:-0}" = 1 ]; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi
command -v uv > /dev/null 2>&1 || die "uv didn't install. See https://docs.astral.sh/uv/ and try again."

# --- 3. Flotilla ------------------------------------------------------------
if [ -n "${FLOTILLA_PACKAGE:-}" ]; then
  PACKAGE="$FLOTILLA_PACKAGE"
  what="Flotilla ($PACKAGE)"
elif [ -n "$REF" ]; then
  PACKAGE="flotilla-face @ https://github.com/$REPO/archive/$REF.tar.gz"
  what="Flotilla from github.com/$REPO ($REF)"
else
  # The newest release: GitHub redirects /releases/latest to /releases/tag/vX.Y.Z.
  tag=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest" 2> /dev/null \
    | sed -n 's#.*/releases/tag/##p') || tag=""
  case "$tag" in *[!A-Za-z0-9.]*) tag="" ;; esac
  case "$tag" in
    v[0-9]*.[0-9]*.[0-9]*)
      PACKAGE="flotilla-face @ https://github.com/$REPO/releases/download/$tag/flotilla_face-${tag#v}-py3-none-any.whl"
      what="Flotilla ${tag#v}, the newest release"
      ;;
    *)
      # Never fall back to main: it isn't signed or released, and may be mid-change.
      die "couldn't find the newest release on github.com/$REPO (check your connection and try again; FLOTILLA_REF=main installs main on purpose)"
      ;;
  esac
fi
step "Installing $what with Python $PYTHON_VERSION"
# --refresh-package: running the installer again picks up what's newest, not a cached download.
# uv's own output (every package it installs) is shown only if something goes wrong.
uv_log=$(mktemp)
if ! uv tool install --python "$PYTHON_VERSION" --force --refresh-package flotilla-face "$PACKAGE" > "$uv_log" 2>&1; then
  cat "$uv_log" >&2
  rm -f "$uv_log"
  die "couldn't install $PACKAGE."
fi
rm -f "$uv_log"
BIN="$(uv tool dir --bin)/flotilla"
[ -x "$BIN" ] || die "installed, but $BIN is missing."
if [ "${FLOTILLA_NO_MODIFY_PATH:-0}" != 1 ]; then
  uv tool update-shell > /dev/null 2>&1 || true
fi
ok "Installed flotilla $("$BIN" --version)"

# --- 4. Setup ---------------------------------------------------------------
if [ "${FLOTILLA_YES:-0}" = 1 ] || ! (exec < /dev/tty) 2> /dev/null; then
  say ""
  say "Next: run 'flotilla init' to choose how much space to give, then 'flotilla service install'."
  exit 0
fi

# Setup can move into a VPN container (gluetun) instead of this machine. Then Docker keeps the
# containers running and there's nothing to install here; init says which it did in this file.
setup_result=$(mktemp)
FLOTILLA_SETUP_RESULT="$setup_result" "$BIN" init < /dev/tty
where=$(cat "$setup_result")
rm -f "$setup_result"
if [ "$where" = docker ]; then
  say ""
  ok "Done. Flotilla runs in Docker, which restarts it on its own: nothing to run on this machine."
  say "  The dashboard's address is above; ${B}docker logs flotilla-dashboard${R} shows it again."
  exit 0
fi
say ""
say "${B}Run in the background${R}"
printf '%s' "  ${C}?${R} Run Flotilla now and every time you log in ${D}(Y/n)${R}: "
read -r answer < /dev/tty || answer=n
case "$answer" in
  [nN]*)
    say "  Start it whenever you like with: flotilla run"
    ;;
  *)
    linger=""
    if [ "$os" = Linux ]; then
      printf '%s' "  ${C}?${R} Keep seeding while you're logged out, good for servers ${D}(y/N)${R}: "
      read -r keep < /dev/tty || keep=n
      case "$keep" in [yY]*) linger="--linger" ;; esac
    fi
    # shellcheck disable=SC2086
    "$BIN" service install $linger
    ;;
esac
say ""
ok "Done. Open a new terminal so 'flotilla' is on your PATH, then: ${B}flotilla dashboard${R}"
