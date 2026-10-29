"""Ecosystem-aware version comparison for affected-range matching.

The comparators implement the ordering rules that matter for advisory ranges.
They are intentionally small and dependency-free so the matching logic can be
tested independently from scraping and database code.
"""

from __future__ import annotations

import re
from functools import cmp_to_key

SEMVER_ECOSYSTEMS = {"npm", "crates.io", "NuGet", "Packagist", "Hex"}
PEP440_ECOSYSTEMS = {"PyPI"}
RUBYGEMS_ECOSYSTEMS = {"RubyGems"}

_SEMVER_RE = re.compile(
    r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_PEP440_RE = re.compile(
    r"^\s*v?"
    r"(?:(?P<epoch>\d+)!)?"
    r"(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>\d+)?)?"
    r"(?:[-_.]?(?P<post>post|rev|r)[-_.]?(?P<post_n>\d+)?)?"
    r"(?:[-_.]?(?P<dev>dev)[-_.]?(?P<dev_n>\d+)?)?"
    r"(?:\+[0-9A-Za-z.-]+)?\s*$",
    re.IGNORECASE,
)
_GEM_TOKEN_RE = re.compile(r"\d+|[a-zA-Z]+")


class UnsupportedVersion(ValueError):
    pass


def _compare(a: object, b: object) -> int:
    return (a > b) - (a < b)


def _semver_key(version: str) -> tuple:
    match = _SEMVER_RE.match(version.strip())
    if not match:
        raise UnsupportedVersion(f"not a semver version: {version!r}")

    major, minor, patch, revision, prerelease = match.groups()
    core = (
        int(major),
        int(minor or 0),
        int(patch or 0),
        int(revision or 0),
    )

    if prerelease is None:
        pre: tuple = (1,)
    else:
        identifiers: list[tuple[int, object]] = []
        for part in prerelease.split("."):
            if part.isdigit():
                identifiers.append((0, int(part)))
            else:
                identifiers.append((1, part))
        pre = (0, tuple(identifiers))
    return (*core, pre)


def _compare_semver(a: str, b: str) -> int:
    left, right = _semver_key(a), _semver_key(b)
    if left[:4] != right[:4]:
        return _compare(left[:4], right[:4])

    left_pre, right_pre = left[4], right[4]
    if left_pre[0] != right_pre[0]:
        return _compare(left_pre[0], right_pre[0])
    if left_pre[0] == 1:
        return 0

    for left_part, right_part in zip(left_pre[1], right_pre[1]):
        if left_part == right_part:
            continue
        if left_part[0] != right_part[0]:
            return _compare(left_part[0], right_part[0])
        return _compare(left_part[1], right_part[1])
    return _compare(len(left_pre[1]), len(right_pre[1]))


def _pep440_key(version: str) -> tuple:
    match = _PEP440_RE.match(version)
    if not match:
        raise UnsupportedVersion(f"not a PEP 440 version: {version!r}")

    epoch = int(match.group("epoch") or 0)
    release = tuple(int(part) for part in match.group("release").split("."))
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]

    pre_label = (match.group("pre_l") or "").lower()
    pre_number = int(match.group("pre_n") or 0)
    post_number = int(match.group("post_n") or 0) if match.group("post") else None
    dev_number = int(match.group("dev_n") or 0) if match.group("dev") else None

    if pre_label:
        phase = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}[pre_label]
        pre = (0, phase, pre_number)
    elif post_number is None and dev_number is not None:
        # A bare dev release sorts before every pre-release of the same version.
        pre = (-1, 0, 0)
    else:
        pre = (1, 0, 0)

    post = (0, 0) if post_number is None else (1, post_number)
    dev = (2, 0) if dev_number is None else (1, dev_number)
    return (epoch, release, pre, post, dev)


def _compare_pep440(a: str, b: str) -> int:
    left, right = _pep440_key(a), _pep440_key(b)
    for left_part, right_part in zip(left, right):
        if left_part == right_part:
            continue
        if isinstance(left_part, tuple) and isinstance(right_part, tuple):
            return _compare(left_part, right_part)
        if left_part != right_part:
            return _compare(len(left_part), len(right_part)) if isinstance(left_part, tuple) else _compare(left_part, right_part)
    return 0


def _gem_key(version: str) -> tuple:
    tokens: list[tuple[int, object]] = []
    for part in _GEM_TOKEN_RE.findall(version.strip()):
        if part.isdigit():
            tokens.append((1, int(part)))
        else:
            tokens.append((0, part.lower()))
    if not tokens:
        raise UnsupportedVersion(f"not a RubyGems version: {version!r}")
    return tuple(tokens)


def _compare_gem(a: str, b: str) -> int:
    left, right = _gem_key(a), _gem_key(b)
    length = max(len(left), len(right))
    for index in range(length):
        left_part = left[index] if index < len(left) else (1, 0)
        right_part = right[index] if index < len(right) else (1, 0)
        if left_part == right_part:
            continue
        return _compare(left_part, right_part)
    return 0


def compare_versions(ecosystem: str, a: str, b: str) -> int:
    if ecosystem in PEP440_ECOSYSTEMS:
        return _compare_pep440(a, b)
    if ecosystem in RUBYGEMS_ECOSYSTEMS:
        return _compare_gem(a, b)
    return _compare_semver(a, b)


def sort_versions(ecosystem: str, versions: list[str]) -> list[str]:
    return sorted(versions, key=cmp_to_key(lambda a, b: compare_versions(ecosystem, a, b)))


def version_in_range(
    ecosystem: str,
    version: str,
    introduced: str | None,
    fixed: str | None = None,
    last_affected: str | None = None,
) -> bool | None:
    """Return whether a version falls in [introduced, fixed) or <= last_affected.

    None means the comparison could not be made, which callers must report as
    uncertainty instead of treating it as "not affected".
    """
    try:
        if introduced and introduced != "0" and compare_versions(ecosystem, version, introduced) < 0:
            return False
        if fixed:
            return compare_versions(ecosystem, version, fixed) < 0
        if last_affected:
            return compare_versions(ecosystem, version, last_affected) <= 0
        return True
    except UnsupportedVersion:
        return None
