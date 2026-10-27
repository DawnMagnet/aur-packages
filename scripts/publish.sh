#!/bin/bash
# Publish a successfully built package to the AUR, then commit it here.
# Never force-pushes and never deletes AUR history.
set -euo pipefail

pkg="${1:?package name}"
repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"

case "$pkg" in
  maple-mono-nf-cn|memprocfs|moonbit-bin|cuda-api-wrappers-git|reflectcpp-git) ;;
  *)
    echo "refusing to publish $pkg; it is not in the AUR allowlist" >&2
    exit 1
    ;;
esac

if [[ -f "$pkg/.aur-disabled" ]]; then
  echo "refusing to publish disabled package $pkg" >&2
  exit 1
fi

if [[ ! -f "$pkg/PKGBUILD" || ! -f "$pkg/.SRCINFO" ]]; then
  echo "missing PKGBUILD or .SRCINFO for $pkg" >&2
  exit 1
fi

if [[ "$pkg" != *-git ]] && grep -Eq 'sums(_[A-Za-z0-9_]+)?=\(.*SKIP' "$pkg/PKGBUILD"; then
  echo "refusing to publish $pkg with SKIP checksums" >&2
  exit 1
fi

ver="$(awk -F= '/^pkgver=/{v=$2} /^pkgrel=/{r=$2} END{print v "-" r}' "$pkg/PKGBUILD")"
echo "publishing $pkg $ver"

install -d -m 700 "$HOME/.ssh"
python3 - <<'PY'
import base64, os, pathlib, sys
raw = os.environ.get("AUR_SSH_PRIVATE_KEY", "")
if not raw.strip():
    sys.exit("AUR_SSH_PRIVATE_KEY is empty")
raw = raw.replace("\r\n", "\n").replace("\r", "\n")
if "PRIVATE KEY" not in raw:
    try:
        raw = base64.b64decode(raw, validate=True).decode()
    except Exception as exc:
        sys.exit(f"AUR_SSH_PRIVATE_KEY is not a PEM private key or base64 PEM: {exc}")
if not raw.endswith("\n"):
    raw += "\n"
path = pathlib.Path.home() / ".ssh" / "aur_ed25519"
path.write_text(raw)
path.chmod(0o600)
PY

echo "AUR key fingerprint:"
ssh-keygen -lf "$HOME/.ssh/aur_ed25519"

expected=(
  "SHA256:RFzBCUItH9LZS0cKB5UE6ceAYhBD5C8GeOBip8Z11+4"
  "SHA256:uTa/0PndEgPZTf76e1DFqXKJEXKsn7m9ivhLQtzGOCI"
  "SHA256:5s5cIyReIfNNVGRFdDbe3hdYiI5OelHGpw2rOUud3Q8"
)
pinned="$repo/scripts/aur_known_hosts"
pinned_fps="$(ssh-keygen -lf "$pinned")"
echo "pinned known_hosts:"
echo "$pinned_fps"
for fp in "${expected[@]}"; do
  if ! grep -F -q "$fp" <<<"$pinned_fps"; then
    echo "pinned known_hosts is missing published fingerprint $fp" >&2
    exit 1
  fi
done

scan="$(mktemp)"
if ssh-keyscan -T 20 -t ed25519,ecdsa,rsa aur.archlinux.org > "$scan" 2>"$scan.err"; then
  scan_fps="$(ssh-keygen -lf "$scan")"
  echo "ssh-keyscan:"
  echo "$scan_fps"
  for fp in "${expected[@]}"; do
    if ! grep -F -q "$fp" <<<"$scan_fps"; then
      echo "ssh-keyscan fingerprint mismatch; expected $fp" >&2
      cat "$scan.err" >&2 || true
      exit 1
    fi
  done
  install -m 644 "$scan" "$HOME/.ssh/known_hosts_aur"
else
  echo "ssh-keyscan failed; using the pinned known_hosts file" >&2
  cat "$scan.err" >&2 || true
  install -m 644 "$pinned" "$HOME/.ssh/known_hosts_aur"
fi

export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/aur_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$HOME/.ssh/known_hosts_aur -o ConnectTimeout=20"

push_aur() {
  local attempt stage
  for attempt in 1 2 3 4; do
    stage="$(mktemp -d)"
    if ! git clone --depth 1 "ssh://aur@aur.archlinux.org/${pkg}.git" "$stage"; then
      echo "AUR clone failed (attempt $attempt)" >&2
      rm -rf "$stage"
      sleep $((attempt * 3))
      continue
    fi
    # Replace package files. Do not --delete: extra AUR files stay in history
    # until a normal commit removes them, and we never rewrite history.
    rsync -a \
      --exclude src \
      --exclude pkg \
      --exclude '.update-old-version' \
      --exclude '.aur-disabled' \
      --exclude '*.pkg.tar*' \
      --exclude '*.log' \
      "$repo/$pkg/" "$stage/"
    git -C "$stage" add -A
    if git -C "$stage" diff --cached --quiet; then
      echo "AUR already has $pkg $ver"
      rm -rf "$stage"
      return 0
    fi
    git -C "$stage" -c user.name="DawnMagnet" -c user.email="DawnMagnet@users.noreply.github.com" \
      commit -m "Update to $ver"
    if git -C "$stage" push origin HEAD:master; then
      rm -rf "$stage"
      return 0
    fi
    echo "AUR push rejected (attempt $attempt); retrying without force" >&2
    rm -rf "$stage"
    sleep $((attempt * 3))
  done
  echo "failed to push $pkg to the AUR" >&2
  return 1
}

push_aur

git config user.name "DawnMagnet"
git config user.email "DawnMagnet@users.noreply.github.com"
git add -- "$pkg"
if git diff --cached --quiet; then
  echo "no local commit needed for $pkg"
else
  git commit -m "Update to $ver"
  for attempt in 1 2 3 4 5; do
    if git push origin HEAD:main; then
      break
    fi
    echo "github push rejected (attempt $attempt); rebasing" >&2
    git pull --rebase origin main
    sleep $((attempt * 2))
    if [[ "$attempt" -eq 5 ]]; then
      echo "failed to push $pkg to GitHub" >&2
      exit 1
    fi
  done
fi

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  echo "Published **${pkg}** ${ver} to the AUR and this repo." >> "$GITHUB_STEP_SUMMARY"
fi
echo "published $pkg $ver"
