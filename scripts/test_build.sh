#!/bin/bash
# Test-build one package in archlinux:base-devel.
#
# Container 1 only refreshes checksums (updpkgsums + makepkg --printsrcinfo).
# The host copies that PKGBUILD into a directory that is never mounted again
# and rejects anything that is not a pkgver, pkgrel, or strict checksum edit.
# Container 2 is a fresh makepkg -s. It sees a throwaway copy, not the seal.
# .SRCINFO is not an output; the publish job regenerates it.
set -euo pipefail

pkg="${1:?package name}"
if [[ ! "$pkg" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]; then
  echo "invalid package name: $pkg" >&2
  exit 1
fi

repo="$(cd "$(dirname "$0")/.." && pwd)"
pkgdir="$repo/$pkg"
script="$repo/scripts/build_in_arch.sh"

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

tmp_root="$(mktemp -d)"
seal="$tmp_root/seal"
work_seal="$tmp_root/seal-work"
work_build="$tmp_root/build-copy"
mkdir -p "$seal" "$work_seal" "$work_build"

cleanup() {
  local status=$?
  case "$tmp_root" in
    /tmp/*|/var/tmp/*|"${TMPDIR:-/tmp}"/*|"${RUNNER_TEMP:-/tmp}"/*)
      chmod -R u+rwX "$tmp_root" 2>/dev/null || true
      rm -rf "$tmp_root" 2>/dev/null || sudo rm -rf "$tmp_root" || true
      ;;
    *)
      echo "refusing to delete unexpected work dir: $tmp_root" >&2
      ;;
  esac
  exit "$status"
}
trap cleanup EXIT

unset GITHUB_TOKEN GH_TOKEN ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_RUNTIME_TOKEN || true

image="$(<"$repo/scripts/archlinux-image")"
if [[ ! "$image" =~ ^archlinux:base-devel@sha256:[0-9a-f]{64}$ ]]; then
  echo "scripts/archlinux-image must be archlinux:base-devel@sha256:<digest>" >&2
  exit 1
fi
docker pull "$image"
docker run --rm \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  -e LANG=C \
  -v "$pkgdir:/pkg:ro" \
  -v "$script:/build_in_arch.sh:ro" \
  -v "$work_seal:/work" \
  "$image" \
  bash /build_in_arch.sh seal "$pkg" /pkg /work

if [[ -L "$work_seal/result/PKGBUILD" || ! -f "$work_seal/result/PKGBUILD" ]]; then
  echo "seal container did not produce a regular PKGBUILD" >&2
  exit 1
fi
install -m 644 "$work_seal/result/PKGBUILD" "$seal/PKGBUILD"
# Drop the first container's work directory before the test build starts.
chmod -R u+rwX "$work_seal" 2>/dev/null || true
rm -rf "$work_seal" 2>/dev/null || sudo rm -rf "$work_seal"

check_args=(
  --package "$pkg"
  --baseline "$pkgdir/PKGBUILD"
  --candidate "$seal/PKGBUILD"
)
if [[ -f "$pkgdir/.update-old-version" ]]; then
  check_args+=(--old-version-file "$pkgdir/.update-old-version")
fi
python3 "$repo/scripts/publish_artifact.py" check-pkgbuild "${check_args[@]}"

mkdir -p "$work_build/pkg"
cp -a "$pkgdir/." "$work_build/pkg/"
rm -rf "$work_build/pkg/src" "$work_build/pkg/pkg"
rm -f "$work_build/pkg/.SRCINFO" "$work_build/pkg/.update-old-version"
install -m 644 "$seal/PKGBUILD" "$work_build/pkg/PKGBUILD"
case "$seal" in
  "$work_build"|"$work_build"/*)
    echo "refusing to mount the sealed PKGBUILD into the test build" >&2
    exit 1
    ;;
esac

# Second container: throwaway package copy only. $seal is not a mount.
docker run --rm \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  -e LANG=C \
  -v "$script:/build_in_arch.sh:ro" \
  -v "$work_build/pkg:/pkg" \
  "$image" \
  bash /build_in_arch.sh build "$pkg" /pkg

out="${AUR_BUILD_OUT:-}"
if [[ -z "$out" ]]; then
  out="$(mktemp -d)"
  printf 'build outputs: %s\n' "$out"
fi
mkdir -p "$out"
install -m 644 "$seal/PKGBUILD" "$out/PKGBUILD"
printf 'build ok: %s\n' "$pkg"
