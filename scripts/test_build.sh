#!/bin/bash
# Test-build a package in the official archlinux:base-devel image.
set -euo pipefail

pkg="${1:?package name}"
repo="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -f "$repo/$pkg/.aur-disabled" ]]; then
  echo "refusing to build disabled package $pkg" >&2
  exit 1
fi

docker pull archlinux:base-devel
docker run --rm \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  -e LANG=C \
  -v "$repo:/work" \
  -w /work \
  archlinux:base-devel \
  bash /work/scripts/build_in_arch.sh "$pkg" /work
