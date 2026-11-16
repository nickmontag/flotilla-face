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
# Environment variables:
#   FLOTILLA_REF=...            a tag or commit of this repo to install instead of main, e.g. v0.1.0
#                               (a commit hash pins exactly what you run)
#   FLOTILLA_PACKAGE=...        what to install instead of this repo (a git URL, wheel or folder)
#   FLOTILLA_YES=1              no questions: install only, set up later with `flotilla init`
#   FLOTILLA_NO_MODIFY_PATH=1   don't add ~/.local/bin to your shell profile
set -eu

# Straight from the source on GitHub, as a tarball (no git needed). Flotilla isn't published to
# a package index, so what you install is what you can read in the repo.
REF="${FLOTILLA_REF:-main}"
PACKAGE="${FLOTILLA_PACKAGE:-flotilla-face @ https://github.com/nickmontag/flotilla-face/archive/$REF.tar.gz}"
PYTHON_VERSION="3.13" # libtorrent publishes no wheels for 3.14+ yet

say() { printf '%s\n' "$*"; }
die() { printf 'flotilla install: %s\n' "$*" >&2; exit 1; }
case "$REF" in
  *[!A-Za-z0-9._-]* | "") die "FLOTILLA_REF must be a tag, branch or commit (letters, digits, . _ -)." ;;
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
  say "Installing uv, the Python package manager Flotilla uses (from astral.sh)..."
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
  say "Installing Flotilla ($PACKAGE) with Python $PYTHON_VERSION..."
else
  say "Installing Flotilla from github.com/nickmontag/flotilla-face with Python $PYTHON_VERSION..."
fi
# --refresh-package: running the installer again picks up the newest main, not a cached download.
uv tool install --python "$PYTHON_VERSION" --force --refresh-package flotilla-face "$PACKAGE" \
  || die "couldn't install $PACKAGE."
BIN="$(uv tool dir --bin)/flotilla"
[ -x "$BIN" ] || die "installed, but $BIN is missing."
if [ "${FLOTILLA_NO_MODIFY_PATH:-0}" != 1 ]; then
  uv tool update-shell > /dev/null 2>&1 || true
fi
say "Installed: flotilla $("$BIN" --version)"

# --- 4. Setup ---------------------------------------------------------------
if [ "${FLOTILLA_YES:-0}" = 1 ] || ! (exec < /dev/tty) 2> /dev/null; then
  say ""
  say "Next: run 'flotilla init' to choose how much space to give, then 'flotilla service install'."
  exit 0
fi

say ""
"$BIN" init < /dev/tty
say ""
printf 'Run Flotilla in the background now and every time you log in? [Y/n] '
read -r answer < /dev/tty || answer=n
case "$answer" in
  [nN]*)
    say "OK. Start it whenever you like with: flotilla run"
    ;;
  *)
    linger=""
    if [ "$os" = Linux ]; then
      printf 'Keep seeding while you are logged out (good for servers)? [y/N] '
      read -r keep < /dev/tty || keep=n
      case "$keep" in [yY]*) linger="--linger" ;; esac
    fi
    # shellcheck disable=SC2086
    "$BIN" service install $linger
    ;;
esac
say ""
say "Done. Open a new terminal so 'flotilla' is on your PATH, then: flotilla dashboard"
