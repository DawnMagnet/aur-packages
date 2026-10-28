#!/bin/bash
# Test-build a package in the official archlinux:base-devel image.
#
# The package directory is mounted read-only at /pkg. Writable build state
# lives in a temporary directory mounted at /work. The git checkout, other
# packages, and scripts/publish.sh are not mounted. PKGBUILD and .SRCINFO
# are copied to $AUR_BUILD_OUT (a new temp directory when that is unset).
set -euo pipefail

pkg="${1:?package name}"
if [[ ! "$pkg" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]; then
  echo "invalid package name: $pkg" >&2
  exit 1
fi

repo="$(cd "$(dirname "$0")/.." && pwd)"
pkgdir="$repo/$pkg"

if [[ -f "$pkgdir/.aur-disabled" ]]; then
  echo "refusing to build disabled package $pkg" >&2
  exit 1
fi
if [[ ! -f "$pkgdir/PKGBUILD" || -L "$pkgdir/PKGBUILD" ]]; then
  echo "missing PKGBUILD for $pkg" >&2
  exit 1
fi
if [[ -L "$pkgdir/.SRCINFO" ]]; then
  echo "refusing to build $pkg: .SRCINFO is a symlink" >&2
  exit 1
fi

work="$(mktemp -d)"
cleanup() {
  local status=$?
  case "$work" in
    /tmp/*|/var/tmp/*|"${TMPDIR:-/tmp}"/*|"${RUNNER_TEMP:-/tmp}"/*)
      chmod -R u+rwX "$work" 2>/dev/null || true
      rm -rf "$work" || true
      ;;
    *)
      echo "refusing to delete unexpected work dir: $work" >&2
      ;;
  esac
  exit "$status"
}
trap cleanup EXIT

# docker run does not inherit the environment. Drop the tokens anyway so a
# later edit cannot pass them into the build with -e.
unset GITHUB_TOKEN GH_TOKEN ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_RUNTIME_TOKEN || true

docker pull archlinux:base-devel
docker run --rm \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  -e LANG=C \
  -v "$pkgdir:/pkg:ro" \
  -v "$repo/scripts/build_in_arch.sh:/build_in_arch.sh:ro" \
  -v "$work:/work" \
  archlinux:base-devel \
  bash /build_in_arch.sh "$pkg" /pkg /work

if [[ -L "$work/result/PKGBUILD" || -L "$work/result/.SRCINFO" ]]; then
  echo "build result is a symlink; refusing" >&2
  exit 1
fi
if [[ ! -f "$work/result/PKGBUILD" || ! -f "$work/result/.SRCINFO" ]]; then
  echo "build did not produce PKGBUILD and .SRCINFO" >&2
  exit 1
fi

out="${AUR_BUILD_OUT:-}"
if [[ -z "$out" ]]; then
  out="$(mktemp -d)"
  printf 'build outputs: %s\n' "$out"
fi
mkdir -p "$out"
install -m 644 "$work/result/PKGBUILD" "$out/PKGBUILD"
install -m 644 "$work/result/.SRCINFO" "$out/.SRCINFO"
printf 'build ok: %s\n' "$pkg"
