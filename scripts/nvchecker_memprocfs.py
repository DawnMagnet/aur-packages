#!/usr/bin/env python3
"""Print the memprocfs version nvchecker should track: <pkgver>.<pkgrel>."""

from update_package import probe_memprocfs

info = probe_memprocfs()
print(f"{info['pkgver']}.{info['pkgrel']}")
