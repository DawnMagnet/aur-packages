# aur-packages

PKGBUILDs for [DawnMagnet](https://aur.archlinux.org/packages?K=DawnMagnet&SeB=m)'s AUR packages. GitHub Actions checks upstream every 2 days, test-builds in `archlinux:base-devel`, and publishes from a separate job only when that build succeeds.

| Package | Kind | Upstream | Automation |
| --- | --- | --- | --- |
| `maple-mono-nf-cn` | release, font | [subframe7536/maple-font](https://github.com/subframe7536/maple-font) stable tags, asset `MapleMono-NF-CN-unhinted.zip` | nvchecker + checksums |
| `memprocfs` | release, prebuilt | [ufrisk/MemProcFS](https://github.com/ufrisk/MemProcFS) | static `pkgver` / URLs / checksums |
| `moonbit-bin` | release, prebuilt | [MoonBit CLI](https://www.moonbitlang.com/download/) `latest` channel | `moon version` + pinned checksum |
| `cuda-api-wrappers-git` | VCS | [eyalroz/cuda-api-wrappers](https://github.com/eyalroz/cuda-api-wrappers) | `pkgver()` from `git describe` |
| `reflectcpp-git` | VCS | [getml/reflect-cpp](https://github.com/getml/reflect-cpp) | `pkgver()` from `git describe` |
| `asus-fa401km-acpi-fix` | local | none | imported only, no updater |
| `hearthstone-linux-gui-bin` | release | [DawnMagnet/hearthstone-linux-gui](https://github.com/DawnMagnet/hearthstone-linux-gui) | prepared, **not pushed** |
| `hearthstone-linux-gui-appimage` | release | same | prepared, **not pushed** |

The hearthstone directories are disabled with `.aur-disabled` and are absent from the workflow matrix. They must not be pushed until the AUR packages are adopted.

## What the workflow does

`.github/workflows/update-aur.yml` runs:

- on the schedule `0 0 */2 * *` (00:00 UTC on odd days of the month; GitHub may start the job late)
- on `workflow_dispatch`, with boolean input `force_publish` (default false)
- on a push to `main` that touches a package directory, `nvchecker.toml`, `scripts/`, or the workflow file

`force_publish` exists only on manual runs. When it is true, every matrix package is test-built and published even if the upstream version did not change. Schedule and push runs cannot set it. Dispatch it from `main`.

Each package is its own matrix entry (`fail-fast: false`). Build and publish are separate jobs and share a per-package concurrency group (`aur-update-<package>`, `cancel-in-progress: false`).

### Build job

Permissions are `contents: read`. The job does not receive `AUR_SSH_PRIVATE_KEY`. Checkout uses `persist-credentials: false`, so the job token is not written into `.git/config`.

1. `scripts/update_package.py` compares upstream with the PKGBUILD. `nvchecker.toml` is the version source for GitHub releases. The script also calls `nvchecker` when it is installed (CI installs it) and uses `GITHUB_TOKEN` via a gitignored `nvchecker-keyfile.toml`. That file stays in the checkout root and is not mounted into the container.
2. If the upstream version is strictly newer (`vercmp`), it sets `pkgver`, resets `pkgrel` to `1` (memprocfs uses the asset date as `pkgrel`; see below), and updates source URLs. When nothing changed and this is not a manual `force_publish`, the job uploads a skip marker and does not start Docker.
3. `scripts/test_build.sh` uses two fresh `archlinux:base-devel` containers. The first mounts the package directory read-only and runs only `updpkgsums` and `makepkg --printsrcinfo` (VCS packages also run `makepkg --nobuild --nodeps` so `pkgver()` is filled in). It has no sudoers entry. The host copies the resulting `PKGBUILD` into a directory that is not mounted anywhere else, and rejects the file unless the only changes from the trusted tree are `pkgver`, `pkgrel`, and checksum arrays whose entries are quoted lowercase hashes (or `SKIP` on `-git` packages) with nothing after the closing `)`. The second container is a new `makepkg -s` (then `namcap`, whose findings are ignored) on a throwaway copy. It cannot see the sealed `PKGBUILD`. The build is x86_64 only; aarch64 checksums are still generated for packages that ship them.
4. The artifact is that sealed `PKGBUILD` plus a hash manifest. `.SRCINFO` is not uploaded. The build workspace is discarded.

### Publish job

`needs: build`. A failed build of one package does not cancel the others. This job's permissions are `contents: write` and `actions: read`. It is the only job that runs in the `aur` environment and the only job that is given `AUR_SSH_PRIVATE_KEY`. It checks out `main` again (`persist-credentials: true`, so the commit can be pushed) and does not reuse the build workspace or any script the build could have edited.

The artifact is checked again (regular file, size, UTF-8, strict checksums, no downgrade against the clean tree, and no edits outside `pkgver`, `pkgrel`, checksums, and a single-line `https://` source array). `.SRCINFO` from the build is not accepted. `scripts/regen_srcinfo.sh` runs `makepkg --holdver --printsrcinfo` in a fresh container whose only input is a copy of the validated package, and that output becomes `.SRCINFO`. `scripts/publish.sh` then pushes the package directory to `ssh://aur@aur.archlinux.org/<name>.git` branch `master`, then commits `Update to <pkgver>-<pkgrel>` here as `DawnMagnet <DawnMagnet@users.noreply.github.com>`.

A failing build does not push to the AUR or to this repo. The next run retries from the last commit. Pushes are normal fast-forwards: the script does not force-push and does not rewrite AUR history. Commits back to this repo use `GITHUB_TOKEN`, which does not start another workflow run.

Host keys for `aur.archlinux.org` are pinned in `scripts/aur_known_hosts` and checked against the fingerprints published on the AUR homepage:

- Ed25519 `SHA256:RFzBCUItH9LZS0cKB5UE6ceAYhBD5C8GeOBip8Z11+4`
- ECDSA `SHA256:uTa/0PndEgPZTf76e1DFqXKJEXKsn7m9ivhLQtzGOCI`
- RSA `SHA256:5s5cIyReIfNNVGRFdDbe3hdYiI5OelHGpw2rOUud3Q8`

The publish step also runs `ssh-keyscan` and refuses to connect if the live key does not show those fingerprints.

### Secrets

| Secret | Use |
| --- | --- |
| `AUR_SSH_PRIVATE_KEY` | ed25519 private key whose public key is registered on the DawnMagnet AUR account. Store it on the `aur` environment (below), not as a repository secret, so only the publish job can read it. |
| `GITHUB_TOKEN` | automatic. The build job's token is read-only and is used for the GitHub API. The publish job's token can push to `main`. |

### `aur` environment

The publish job sets `environment: aur`. Create that environment in the repository settings before relying on the branch restriction: deployment branches limited to `main`, and `AUR_SSH_PRIVATE_KEY` added there. If the environment does not exist, GitHub creates it on the first run with no branch rule and no secrets; a repository-level copy of the key still works until it is removed. After one successful publish on this workflow, delete the repository-level secret so other jobs cannot use it.

## Version schemes

### memprocfs

Upstream asset names look like `MemProcFS_files_and_binaries_v5.19.0-linux_x64-20261014.tar.gz`. The PKGBUILD is static: fixed `pkgver`, fixed URLs for `x86_64` and `aarch64`, and real `sha256` sums. It does not call the GitHub API at build time.

- `pkgver` is the embedded version `major.minor.build` (`5.19.0`)
- `pkgrel` is the asset date `YYYYMMDD` (`20261014`)

`vercmp` compares `pkgver` before `pkgrel`, so `5.19.0-20261014` is newer than the previous AUR version `5.16-9.20260104` (pkgver `5.16`, nonstandard pkgrel `9.20260104`). `5.16.14-20260211` is also newer than `5.16-9.20260104`, because `5.16.14 > 5.16`. A later build number bumps `pkgver`. A rebuilt asset with the same build number bumps `pkgrel` through the date.

### moonbit-bin

The install script accepts `.../binaries/<version>/moonbit-linux-x86_64.tar.gz` and `.../cores/core-<version>.tar.gz`, but those dated paths are not actually published (S3 `AccessDenied`). The public channels are `latest` and `nightly`.

The updater downloads `latest`, runs `moon version` with `PATH`, `MOONC_OVERRIDE`, and `MOONRUN_OVERRIDE` pointed at the bundled tools (upstream ships them mode 0644), and records that version (`0.1.20261023` as of this writing; `moonc` reports its own version, currently `v0.10.17`). `updpkgsums` pins the checksum of that tarball and of `core-latest.tar.gz`. Nothing is mirrored.

An already published PKGBUILD breaks when `latest` moves, because the checksum no longer matches. That is unavoidable without versioned URLs. The scheduled run replaces the pin. The build also checks that `moon version` from the tarball that `makepkg` verified still equals `pkgver`.

### VCS packages

`cuda-api-wrappers-git` and `reflectcpp-git` keep `pkgver()` in the PKGBUILD. The updater clones the default branch and applies the same `git describe --long --tags | sed ...` transform, including stripping a leading `v`. The value looks like `0.8.2.r0.gc2f2a2b` or `0.26.2.r10.g587b72ee` (tag, commits since the tag, abbreviated hash). `nvchecker` still records the latest stable tag, which can be behind that git version when there are commits after the tag. `reflectcpp-git` used to publish `pkgver=0` because `.SRCINFO` was generated without running `pkgver()`; the function is what the workflow runs now.

`cuda-api-wrappers-git` depends on `cuda`. The workflow frees disk space before that build. `reflectcpp-git` links pugixml, tomlplusplus, and yaml-cpp, matching the formats the PKGBUILD turns on.

## Add a package

1. Create `<name>/` with a `PKGBUILD`, `.SRCINFO` (`makepkg --printsrcinfo > .SRCINFO`), and any extra sources (`install` scripts, wrappers).
2. Add an entry to `nvchecker.toml`. Use `source = "github"` for tags and releases. Use `source = "cmd"` when the version has to be read from an asset name or a binary, and point it at a small script under `scripts/`.
3. Teach `scripts/update_package.py` how to rewrite that PKGBUILD, and add the name to the allowlist in `scripts/publish.sh`.
4. Add the same matrix row to the build job and the publish job in `.github/workflows/update-aur.yml`, and include the directory in the `push` path filter.
5. Push to `main`. The workflow builds and, on success, the publish job pushes to the AUR.

To stage a package without publishing it, commit the directory with a `.aur-disabled` file and leave it out of the matrix and the publish allowlist. That is how `hearthstone-linux-gui-bin` and `hearthstone-linux-gui-appimage` are stored. For those two, upstream releases already include `hearthstone-linux-gui-<ver>-<rel>-x86_64.pkg.tar.zst` and `hearthstone-linux-gui-<ver>-x86_64.AppImage` plus `SHA256SUMS.txt`.

`asus-fa401km-acpi-fix` has no upstream version. Leave it out of `nvchecker.toml` and the matrix; edit it by hand when the DSDT override changes.
