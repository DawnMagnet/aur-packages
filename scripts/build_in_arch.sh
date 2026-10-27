#!/bin/bash
# Build one package as a non-root user inside archlinux:base-devel.
# Usage: build_in_arch.sh <package> <repo>
set -euo pipefail

pkg="${1:?package name}"
repo="${2:-/work}"
dest="$repo/$pkg"

if [[ ! -f "$dest/PKGBUILD" ]]; then
  echo "missing $dest/PKGBUILD" >&2
  exit 1
fi
if [[ -f "$dest/.aur-disabled" ]]; then
  echo "refusing to build disabled package $pkg" >&2
  exit 1
fi

export LANG=C
unset LC_ALL || true

cleanup() {
  local status=$?
  rm -rf "$dest/src" "$dest/pkg"
  rm -f "$dest"/*.pkg.tar* "$dest"/*.pkg.tar*.sig "$dest"/*.log
  # makepkg drops downloaded sources next to the PKGBUILD. Drop anything that
  # is not part of the git tree so publish does not commit tarballs.
  if git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$repo" clean -fdx -- "$pkg" || true
  fi
  if [[ -n "${HOST_UID:-}" ]]; then
    chown -R "${HOST_UID}:${HOST_GID:-$HOST_UID}" "$dest" || true
  fi
  exit "$status"
}
trap cleanup EXIT

if ! pacman -Sy --noconfirm archlinux-keyring; then
  rm -rf /etc/pacman.d/gnupg
  pacman-key --init
  pacman-key --populate archlinux
  pacman -Sy --noconfirm archlinux-keyring
fi
pacman -Syu --noconfirm --needed base-devel git sudo pacman-contrib unzip
pacman -S --noconfirm --needed namcap || true

if ! id builder >/dev/null 2>&1; then
  useradd -m builder
fi
echo "builder ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/builder
chmod 440 /etc/sudoers.d/builder
git config --system --add safe.directory "*" || true

if grep -q '^#MAKEFLAGS=' /etc/makepkg.conf; then
  sed -i "s|^#MAKEFLAGS=.*|MAKEFLAGS=\"-j$(nproc)\"|" /etc/makepkg.conf
fi

chown -R builder:builder "$dest"

sudo -u builder bash <<EOF
set -euo pipefail
cd "$dest"
updpkgsums
if [[ "$pkg" == *-git ]]; then
  # pkgver() only needs the git checkout. Dependencies are installed by the
  # full makepkg -s below; checking them here aborts before that install.
  makepkg --nobuild --nodeps --noconfirm
fi
makepkg --printsrcinfo > .SRCINFO
EOF

old_file="$dest/.update-old-version"
if [[ -f "$old_file" ]]; then
  old="$(tr -d '[:space:]' < "$old_file")"
  newv="$(awk -F= '/^pkgver=/{print $2; exit}' "$dest/PKGBUILD")"
  newr="$(awk -F= '/^pkgrel=/{print $2; exit}' "$dest/PKGBUILD")"
  new="${newv}-${newr}"
  cmp="$(vercmp "$new" "$old")"
  echo "vercmp $new $old -> $cmp"
  if [[ "$cmp" -le 0 ]]; then
    echo "refusing to publish $pkg: $new is not newer than $old" >&2
    exit 1
  fi
fi

src_ver="$(awk -F' = ' '/^	pkgver = /{print $2; exit}' "$dest/.SRCINFO")"
pkg_ver="$(awk -F= '/^pkgver=/{print $2; exit}' "$dest/PKGBUILD")"
if [[ "$src_ver" != "$pkg_ver" ]]; then
  echo ".SRCINFO pkgver ($src_ver) does not match PKGBUILD ($pkg_ver)" >&2
  exit 1
fi

sudo -u builder bash <<EOF
set -euo pipefail
cd "$dest"
makepkg -s --noconfirm --needed
EOF

if [[ "$pkg" == "moonbit-bin" ]]; then
  # moon version resolves moonc and moonrun via PATH. Upstream ships them mode 0644.
  find "$dest/src/bin" -type f -exec chmod a+x {} +
  moon="$dest/src/bin/moon"
  got="$(
    MOONC_OVERRIDE="$dest/src/bin/moonc" \
    MOONRUN_OVERRIDE="$dest/src/bin/moonrun" \
    PATH="$dest/src/bin:$PATH" \
    "$moon" version | awk 'NR==1 {print $2}'
  )"
  exp="$(awk -F= '/^pkgver=/{print $2; exit}' "$dest/PKGBUILD")"
  echo "moon version $got, pkgver $exp"
  if [[ "$got" != "$exp" ]]; then
    echo "moon version $got does not match pkgver $exp" >&2
    exit 1
  fi
fi

if command -v namcap >/dev/null 2>&1; then
  namcap "$dest"/*.pkg.tar* || true
fi

if [[ "$pkg" != *-git ]] && grep -Eq 'sums(_[A-Za-z0-9_]+)?=\(.*SKIP' "$dest/PKGBUILD"; then
  echo "refusing to keep SKIP checksums for $pkg" >&2
  exit 1
fi

rm -f "$old_file"
echo "build ok: $pkg"
