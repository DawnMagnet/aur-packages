#!/usr/bin/env python3
"""Print the moon CLI version embedded in the latest MoonBit tarball."""

from update_package import probe_moonbit

print(probe_moonbit()["pkgver"])
