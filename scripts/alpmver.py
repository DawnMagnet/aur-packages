"""Pacman/libalpm version comparison (alpm_pkg_vercmp).

Port of lib/libalpm/version.c from pacman. Used on runners that do not ship
vercmp. The Arch test-build container checks the same ordering with vercmp.
"""

from __future__ import annotations


def _rpmvercmp(a: str, b: str) -> int:
    if a == b:
        return 0

    str1 = list(a)
    str2 = list(b)
    one = two = 0
    ptr1 = ptr2 = 0

    def is_alnum(buf: list[str], idx: int) -> bool:
        return idx < len(buf) and buf[idx].isalnum()

    def is_digit(buf: list[str], idx: int) -> bool:
        return idx < len(buf) and buf[idx].isdigit()

    def is_alpha(buf: list[str], idx: int) -> bool:
        return idx < len(buf) and buf[idx].isalpha()

    while one < len(str1) and two < len(str2):
        while one < len(str1) and not is_alnum(str1, one):
            one += 1
        while two < len(str2) and not is_alnum(str2, two):
            two += 1
        if not (one < len(str1) and two < len(str2)):
            break
        if (one - ptr1) != (two - ptr2):
            return -1 if (one - ptr1) < (two - ptr2) else 1

        ptr1 = one
        ptr2 = two
        if is_digit(str1, ptr1):
            while ptr1 < len(str1) and is_digit(str1, ptr1):
                ptr1 += 1
            while ptr2 < len(str2) and is_digit(str2, ptr2):
                ptr2 += 1
            isnum = True
        else:
            while ptr1 < len(str1) and is_alpha(str1, ptr1):
                ptr1 += 1
            while ptr2 < len(str2) and is_alpha(str2, ptr2):
                ptr2 += 1
            isnum = False

        if one == ptr1:
            return -1
        if two == ptr2:
            return 1 if isnum else -1

        seg1 = "".join(str1[one:ptr1])
        seg2 = "".join(str2[two:ptr2])
        if isnum:
            seg1 = seg1.lstrip("0")
            seg2 = seg2.lstrip("0")
            if len(seg1) > len(seg2):
                return 1
            if len(seg2) > len(seg1):
                return -1
        if seg1 != seg2:
            return -1 if seg1 < seg2 else 1
        one = ptr1
        two = ptr2

    if one >= len(str1) and two >= len(str2):
        return 0

    one_empty = one >= len(str1)
    two_ch = str2[two] if two < len(str2) else ""
    one_ch = str1[one] if one < len(str1) else ""
    if (one_empty and two_ch != "" and not two_ch.isalpha()) or one_ch.isalpha():
        return -1
    return 1


def _parse_evr(evr: str) -> tuple[str, str, str | None]:
    i = 0
    while i < len(evr) and evr[i].isdigit():
        i += 1
    if i < len(evr) and evr[i] == ":":
        epoch = evr[:i] or "0"
        version = evr[i + 1 :]
    else:
        epoch = "0"
        version = evr
    if "-" in version:
        version, release = version.rsplit("-", 1)
    else:
        release = None
    return epoch, version, release


def vercmp(a: str, b: str) -> int:
    """Return >0 if a is newer than b, <0 if older, 0 if equal."""
    if a == b:
        return 0
    if a is None and b is None:  # pragma: no cover
        return 0
    if not a:
        return -1
    if not b:
        return 1
    epoch1, ver1, rel1 = _parse_evr(a)
    epoch2, ver2, rel2 = _parse_evr(b)
    ret = _rpmvercmp(epoch1, epoch2)
    if ret == 0:
        ret = _rpmvercmp(ver1, ver2)
        if ret == 0 and rel1 is not None and rel2 is not None:
            ret = _rpmvercmp(rel1, rel2)
    return ret
