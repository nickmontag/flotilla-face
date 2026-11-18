#!/bin/sh
# Try this working tree on a Docker host's Flotilla without making a release.
#
# Builds a wheel here, builds an image from it on the host (tagged flotilla-face:dev-<commit>), and
# recreates the node and dashboard containers on it with their settings and files, the way
# `flotilla update` does. The version number stays the last release's; the image tag says which
# commit it is ("-dirty" when there were uncommitted changes). `flotilla update` on the host puts the
# signed release back.
#
#   scripts/deploy-dev.sh media-server
#   SSH_OPTS="-o HostName=10.0.0.206" scripts/deploy-dev.sh media-server
set -eu
host=${1:?usage: scripts/deploy-dev.sh <ssh host>}
cd "$(dirname "$0")/.."

rev=$(git rev-parse --short HEAD)
git diff --quiet HEAD || rev="$rev-dirty"
tag="flotilla-face:dev-$rev"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

uv build --wheel --out-dir "$tmp/dist" >/dev/null
wheel=$(basename "$tmp"/dist/*.whl)
cp Dockerfile .dockerignore "$tmp/"

# shellcheck disable=SC2086
remote() { ssh ${SSH_OPTS:-} "$host" "$@"; }
dir=$(remote mktemp -d)
COPYFILE_DISABLE=1 tar --no-xattrs -C "$tmp" -czf - Dockerfile .dockerignore dist | remote tar -C "$dir" -xzf -
echo "building $tag on $host"

remote sh -s -- "$dir" "$wheel" "$tag" "$rev" <<'REMOTE'
set -e
dir=$1 wheel=$2 tag=$3 rev=$4
trap 'rm -rf "$dir"' EXIT
docker build -q --build-arg "WHEEL=$wheel" --label "flotilla.dev=$rev" -t "$tag" "$dir" >/dev/null
# The host's own Flotilla recreates the containers: the same code path as `flotilla update`.
cli=$(command -v flotilla || echo "$HOME/.local/bin/flotilla")
py=$(head -1 "$cli" | cut -c3-)
TAG=$tag "$py" - <<'PY'
import os
from flotilla import containers as c
tag = os.environ["TAG"]
found = c.installed()
mine = [n for n in (c.NODE, c.DASHBOARD) if n in found and found[n].ours]
if not mine:
    raise SystemExit("no Flotilla containers here: set Flotilla up with `flotilla init` first")
for name in mine:
    c.replace(found[name], tag)
    print(f"✓ {name} now runs {tag}")
PY
REMOTE
