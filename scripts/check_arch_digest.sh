#!/usr/bin/env bash
# Compare scripts/archlinux-image with the current archlinux:base-devel index
# digest on Docker Hub's official library/archlinux repository.
#
# Prints one of:
#   status=current
#   status=outdated
# plus pinned=<image> and latest=<image> lines (also appended to
# $GITHUB_OUTPUT when set). Exits non-zero if either value is malformed or
# the registry cannot be read. Never writes to the repository.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
image_re='^archlinux:base-devel@sha256:[0-9a-f]{64}$'
registry="https://registry-1.docker.io/v2/library/archlinux/manifests/base-devel"
auth="https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/archlinux:pull"

pinned="$(<"$repo/scripts/archlinux-image")"
if [[ ! "$pinned" =~ $image_re ]]; then
  echo "scripts/archlinux-image must be archlinux:base-devel@sha256:<digest>" >&2
  exit 1
fi

# Anonymous pull token for the public repository; never printed.
token="$(curl -fsS --max-time 30 "$auth" | jq -r '.token // empty')"
if [[ -z "$token" ]]; then
  echo "could not get an anonymous Docker Hub token" >&2
  exit 1
fi
digest="$(
  curl -fsS --max-time 30 -I \
    -H "Authorization: Bearer $token" \
    -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json" \
    "$registry" |
    tr -d '\r' | awk 'tolower($1) == "docker-content-digest:" { print $2 }'
)"
unset token
latest="archlinux:base-devel@${digest}"
if [[ ! "$latest" =~ $image_re ]]; then
  echo "Docker Hub returned a malformed digest" >&2
  exit 1
fi

if [[ "$latest" == "$pinned" ]]; then
  status=current
else
  status=outdated
fi
for line in "status=$status" "pinned=$pinned" "latest=$latest"; do
  echo "$line"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "$line" >> "$GITHUB_OUTPUT"
  fi
done
