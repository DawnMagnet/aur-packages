#!/bin/bash
# Build helper that runs inside archlinux:base-devel.
#
#   build_in_arch.sh seal  <package> <readonly-source> <writable-work>
#       updpkgsums and makepkg --printsrcinfo only. No sudo. The PKGBUILD is
#       copied to <writable-work>/result for the host to take before any
#       test build. This container must not be the one that runs build().
#
#   build_in_arch.sh build <package> <writable-package-copy>
#       makepkg -s only. The caller mounts a throwaway copy, never the sealed
#       PKGBUILD directory. Nothing from this container is published.
set -euo pipefail

mode="${1:?seal or build}"
pkg="${2:?package name}"
src="${3:?package directory}"

if [[ "$mode" != "seal" && "$mode" != "build" ]]; then
  echo "usage: build_in_arch.sh seal|build <package> <source> [work]" >&2
  exit 1
fi
if [[ ! "$pkg" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]; then
  echo "invalid package name: $pkg" >&2
  exit 1
fi
src="${src%/}"
if [[ "$src" != /* || ! -d "$src" || ! -f "$src/PKGBUILD" ]]; then
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

export LANG=C
unset LC_ALL || true

prepare_root() {
  if ! pacman -Sy --noconfirm archlinux-keyring; then
    rm -rf /etc/pacman.d/gnupg
    pacman-key --init
    pacman-key --populate archlinux
    pacman -Sy --noconfirm archlinux-keyring
  fi
  if [[ "$mode" == "seal" ]]; then
    pacman -Syu --noconfirm --needed base-devel git pacman-contrib
  else
    pacman -Syu --noconfirm --needed base-devel git sudo unzip
    pacman -S --noconfirm --needed namcap || true
  fi
  if ! id builder >/dev/null 2>&1; then
    useradd -m builder
  fi
  git config --system --add safe.directory "*" || true
  if grep -q '^#MAKEFLAGS=' /etc/makepkg.conf; then
    sed -i "s|^#MAKEFLAGS=.*|MAKEFLAGS=\"-j$(nproc)\"|" /etc/makepkg.conf
  fi
}

chown_host() {
  local path="$1"
  if [[ -n "${HOST_UID:-}" && -e "$path" ]]; then
    local gid="${HOST_GID:-$HOST_UID}"
    chown -R "${HOST_UID}:${gid}" "$path" || true
  fi
}

run_seal() {
  local work="${1:?writable work directory}"
  work="${work%/}"
  if [[ "$work" != /* || ! -d "$work" || ! -w "$work" ]]; then
    echo "work directory is not writable: $work" >&2
    exit 1
  fi
  local mount_opts
  mount_opts="$(awk -v mp="$src" '$5==mp {print $6; exit}' /proc/self/mountinfo)"
  case ",${mount_opts}," in
    *,ro,*) ;;
    *)
      echo "refusing to seal: $src is not a read-only mount (options: ${mount_opts:-missing})" >&2
      exit 1
      ;;
  esac

  local dest="$work/build/$pkg"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp -a "$src/." "$dest/"
  rm -rf "$dest/src" "$dest/pkg"
  find "$dest" -maxdepth 1 -type f \( -name '*.pkg.tar*' -o -name '*.log' \) -delete
  chown -R builder:builder "$dest"

  # No sudoers entry. updpkgsums and printsrcinfo do not need root, and
  # build() is not run in this container. The single quotes are intentional:
  # DEST and PKG are environment variables of the inner shell.
  # shellcheck disable=SC2016
  runuser -u builder -- env DEST="$dest" PKG="$pkg" bash -c '
set -euo pipefail
cd "$DEST"
updpkgsums
if [[ "$PKG" == *-git ]]; then
  makepkg --nobuild --nodeps --noconfirm
fi
makepkg --printsrcinfo > .SRCINFO
'

  local src_ver pkg_ver
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

  mkdir -p "$work/result"
  install -m 644 "$dest/PKGBUILD" "$work/result/PKGBUILD"
  # Hand the whole work tree back so the host can delete it. The sealed
  # PKGBUILD is copied out before the test-build container exists.
  chown_host "$work"
  echo "sealed $pkg"
}

run_build() {
  local dest="$src"
  if [[ ! -w "$dest/PKGBUILD" ]]; then
    echo "test build directory is not writable: $dest" >&2
    exit 1
  fi
  echo "builder ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/builder
  chmod 440 /etc/sudoers.d/builder
  chown -R builder:builder "$dest"
  # shellcheck disable=SC2016
  runuser -u builder -- env DEST="$dest" bash -c '
set -euo pipefail
cd "$DEST"
makepkg -s --noconfirm --needed
'
  if [[ "$pkg" == "moonbit-bin" ]]; then
    find "$dest/src/bin" -type f -exec chmod a+x {} +
    local moon got exp
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
  chown_host "$dest"
  echo "build ok: $pkg"
}

prepare_root
if [[ "$mode" == "seal" ]]; then
  run_seal "${4:?writable work directory}"
else
  run_build
fi
