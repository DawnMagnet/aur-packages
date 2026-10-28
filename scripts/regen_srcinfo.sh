#!/bin/bash
# Write .SRCINFO for one package from its PKGBUILD.
#
# A fresh container runs makepkg --holdver --printsrcinfo. --holdver keeps
# pkgver() from talking to upstream again; the text comes from the PKGBUILD
# the publish job already validated. The git checkout is not mounted.
set -euo pipefail

pkg="${1:?package name}"
if [[ ! "$pkg" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]; then
  echo "invalid package name: $pkg" >&2
  exit 1
fi

repo="$(cd "$(dirname "$0")/.." && pwd)"
pkgdir="$repo/$pkg"
if [[ ! -f "$pkgdir/PKGBUILD" || -L "$pkgdir/PKGBUILD" ]]; then
  echo "missing PKGBUILD for $pkg" >&2
  exit 1
fi

copy="$(mktemp -d)"
cleanup() {
  local status=$?
  case "$copy" in
    /tmp/*|/var/tmp/*|"${TMPDIR:-/tmp}"/*|"${RUNNER_TEMP:-/tmp}"/*)
      chmod -R u+rwX "$copy" 2>/dev/null || true
      rm -rf "$copy" || true
      ;;
    *)
      echo "refusing to delete unexpected work dir: $copy" >&2
      ;;
  esac
  exit "$status"
}
trap cleanup EXIT

mkdir -p "$copy/pkg"
cp -a "$pkgdir/." "$copy/pkg/"
rm -rf "$copy/pkg/src" "$copy/pkg/pkg"
rm -f "$copy/pkg/.SRCINFO" "$copy/pkg/.update-old-version"

unset GITHUB_TOKEN GH_TOKEN ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_RUNTIME_TOKEN || true

docker pull archlinux:base-devel
docker run --rm \
  -e LANG=C \
  -v "$copy/pkg:/pkg" \
  -w /pkg \
  archlinux:base-devel \
  bash -c 'set -euo pipefail; id builder >/dev/null 2>&1 || useradd -m builder; chown -R builder:builder /pkg; runuser -u builder -- makepkg --holdver --printsrcinfo' \
  > "$copy/SRCINFO"

if ! python3 -c 'import pathlib,sys; data=pathlib.Path(sys.argv[1]).read_bytes(); sys.exit(0 if data and b"\0" not in data else 1)' "$copy/SRCINFO"; then
  echo "makepkg --printsrcinfo produced nothing usable" >&2
  exit 1
fi
if ! grep -F -q -x "pkgbase = ${pkg}" "$copy/SRCINFO"; then
  echo ".SRCINFO pkgbase does not match $pkg" >&2
  exit 1
fi
pkgver="$(awk -F= '/^pkgver=/{print $2; exit}' "$pkgdir/PKGBUILD")"
srcver="$(awk -F' = ' '/^	pkgver = /{print $2; exit}' "$copy/SRCINFO")"
if [[ "$pkgver" != "$srcver" ]]; then
  echo ".SRCINFO pkgver ($srcver) does not match PKGBUILD ($pkgver)" >&2
  exit 1
fi
install -m 644 "$copy/SRCINFO" "$pkgdir/.SRCINFO"
echo "regenerated .SRCINFO for $pkg"
