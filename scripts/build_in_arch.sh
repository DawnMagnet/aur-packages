#!/bin/bash
# Build one package as a non-root user inside archlinux:base-devel.
# Usage: build_in_arch.sh <package> <readonly-source> <writable-work>
#
# <readonly-source> must be a read-only mount of the single package directory.
# Downloads, src/, and pkg/ stay under <writable-work>. PKGBUILD and .SRCINFO
# are sealed into <writable-work>/result before makepkg -s runs.
set -euo pipefail

pkg="${1:?package name}"
src="${2:?readonly package directory}"
work="${3:?writable work directory}"

if [[ ! "$pkg" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]; then
  echo "invalid package name: $pkg" >&2
  exit 1
fi
src="${src%/}"
work="${work%/}"
if [[ "$src" != /* || "$work" != /* ]]; then
  echo "source and work paths must be absolute" >&2
  exit 1
fi
if [[ ! -d "$src" || ! -f "$src/PKGBUILD" ]]; then
  echo "missing $src/PKGBUILD" >&2
  exit 1
fi
if [[ -f "$src/.aur-disabled" ]]; then
  echo "refusing to build disabled package $pkg" >&2
  exit 1
fi
if [[ -n "${HOST_UID:-}" && ! "${HOST_UID}" =~ ^[0-9]+$ ]]; then
  echo "bad HOST_UID" >&2
  exit 1
fi
if [[ -n "${HOST_GID:-}" && ! "${HOST_GID}" =~ ^[0-9]+$ ]]; then
  echo "bad HOST_GID" >&2
  exit 1
fi

# Field 5 is the mount point and field 6 is its options. "ro" must be its own
# option: errors=remount-ro on a read-write superblock does not count.
mount_opts="$(awk -v mp="$src" '$5==mp {print $6; exit}' /proc/self/mountinfo)"
case ",${mount_opts}," in
  *,ro,*) ;;
  *)
    echo "refusing to build: $src is not a read-only mount (options: ${mount_opts:-missing})" >&2
    exit 1
    ;;
esac
if [[ ! -d "$work" || ! -w "$work" ]]; then
  echo "work directory is not writable: $work" >&2
  exit 1
fi

dest="$work/build/$pkg"
result="$work/result"
rm -rf "$dest"
mkdir -p "$dest" "$result"
chmod 755 "$result"

cleanup() {
  local status=$?
  if [[ -n "${HOST_UID:-}" ]]; then
    local gid="${HOST_GID:-$HOST_UID}"
    chown -R "${HOST_UID}:${gid}" "$work" || true
  fi
  exit "$status"
}
trap cleanup EXIT

export LANG=C
unset LC_ALL || true

if ! pacman -Sy --noconfirm archlinux-keyring; then
  rm -rf /etc/pacman.d/gnupg
  pacman-key --init
  pacman-key --populate archlinux
  pacman -Sy --noconfirm archlinux-keyring
fi
pacman -Syu --noconfirm --needed base-devel git sudo pacman-contrib unzip diffutils
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

cp -a "$src/." "$dest/"
rm -rf "$dest/src" "$dest/pkg"
find "$dest" -maxdepth 1 -type f \( -name '*.pkg.tar*' -o -name '*.log' \) -delete
chown -R builder:builder "$dest"

sudo -u builder env DEST="$dest" PKG="$pkg" bash -c '
set -euo pipefail
cd "$DEST"
updpkgsums
if [[ "$PKG" == *-git ]]; then
  # pkgver() only needs the git checkout. Dependencies are installed by the
  # full makepkg -s below; checking them here aborts before that install.
  makepkg --nobuild --nodeps --noconfirm
fi
makepkg --printsrcinfo > .SRCINFO
'

old_file="$dest/.update-old-version"
if [[ -f "$old_file" ]]; then
  old="$(tr -d '[:space:]' < "$old_file")"
  newv="$(awk -F= '/^pkgver=/{print $2; exit}' "$dest/PKGBUILD")"
  newr="$(awk -F= '/^pkgrel=/{print $2; exit}' "$dest/PKGBUILD")"
  new="${newv}-${newr}"
  cmp_result="$(vercmp "$new" "$old")"
  echo "vercmp $new $old -> $cmp_result"
  if [[ "$cmp_result" -le 0 ]]; then
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

if [[ "$pkg" != *-git ]] && grep -Eq 'sums(_[A-Za-z0-9_]+)?=\(.*SKIP' "$dest/PKGBUILD"; then
  echo "refusing to keep SKIP checksums for $pkg" >&2
  exit 1
fi

# Seal the files that are allowed to leave the container before the upstream
# build() / package() functions run. makepkg -s can still rewrite the copy in
# $dest; the host publishes $result only when the two still match.
install -m 644 "$dest/PKGBUILD" "$result/PKGBUILD"
install -m 644 "$dest/.SRCINFO" "$result/.SRCINFO"

sudo -u builder env DEST="$dest" bash -c '
set -euo pipefail
cd "$DEST"
makepkg -s --noconfirm --needed
'

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
  exp="$(awk -F= '/^pkgver=/{print $2; exit}' "$result/PKGBUILD")"
  echo "moon version $got, pkgver $exp"
  if [[ "$got" != "$exp" ]]; then
    echo "moon version $got does not match pkgver $exp" >&2
    exit 1
  fi
fi

if command -v namcap >/dev/null 2>&1; then
  namcap "$dest"/*.pkg.tar* || true
fi

if ! cmp -s "$dest/PKGBUILD" "$result/PKGBUILD" || ! cmp -s "$dest/.SRCINFO" "$result/.SRCINFO"; then
  echo "build modified PKGBUILD or .SRCINFO after they were sealed; refusing to publish" >&2
  exit 1
fi

rm -f "$old_file"
echo "build ok: $pkg"
