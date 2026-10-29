from __future__ import annotations

import pytest

from warehouse.matching import evaluate_ranges
from warehouse.versioning import (
    UnsupportedVersion,
    compare_versions,
    sort_versions,
    version_in_range,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("1.0.0", "1.0.1"),
        ("1.0.0-alpha", "1.0.0-alpha.1"),
        ("1.0.0-alpha.1", "1.0.0-beta"),
        ("1.0.0-rc.1", "1.0.0"),
        ("9.0.0", "10.0.0"),
        ("1.2.3", "1.2.3.1"),
    ],
)
def test_semver_ordering(left: str, right: str) -> None:
    assert compare_versions("npm", left, right) < 0
    assert compare_versions("npm", right, left) > 0


def test_semver_ignores_build_metadata() -> None:
    assert compare_versions("npm", "1.0.0+build.1", "1.0.0") == 0
    assert compare_versions("npm", "v1.2.3", "1.2.3") == 0


def test_nuget_four_part_versions() -> None:
    assert compare_versions("NuGet", "1.2.3.4", "1.2.3") > 0
    assert compare_versions("NuGet", "1.2.3.4", "1.2.4.0") < 0


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("1.0.dev1", "1.0a1"),
        ("1.0a1", "1.0b1"),
        ("1.0b1", "1.0rc1"),
        ("1.0rc1", "1.0"),
        ("1.0", "1.0.post1"),
        ("1.0a1", "1.0a1.post1"),
        ("1.0a1.dev1", "1.0a1"),
    ],
)
def test_pep440_ordering(left: str, right: str) -> None:
    assert compare_versions("PyPI", left, right) < 0


def test_pep440_equivalent_release_forms() -> None:
    assert compare_versions("PyPI", "1.0", "1.0.0") == 0
    assert compare_versions("PyPI", "1.0", "1!0.1") < 0


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("1.0.0.a", "1.0.0"),
        ("1.0.0.rc1", "1.0.0"),
        ("1.0.0", "1.0.1"),
        ("1.0.0.beta2", "1.0.0.beta10"),
    ],
)
def test_rubygems_ordering(left: str, right: str) -> None:
    assert compare_versions("RubyGems", left, right) < 0


def test_rubygems_trailing_zero_segments_are_equal() -> None:
    assert compare_versions("RubyGems", "1.0", "1.0.0") == 0


def test_unsupported_version_is_reported() -> None:
    with pytest.raises(UnsupportedVersion):
        compare_versions("npm", "not-a-version", "1.0.0")


def test_sort_versions_uses_ecosystem_rules() -> None:
    assert sort_versions("npm", ["1.10.0", "1.2.0", "1.2.0-beta"]) == [
        "1.2.0-beta",
        "1.2.0",
        "1.10.0",
    ]


def test_version_in_range_respects_open_and_closed_bounds() -> None:
    assert version_in_range("npm", "1.0.0", "1.0.0", "1.0.1") is True
    assert version_in_range("npm", "1.0.1", "1.0.0", "1.0.1") is False
    assert version_in_range("npm", "0.9.0", "0", "1.0.0") is True
    assert version_in_range("npm", "1.0.0", "1.0.0", None, "1.0.0") is True
    assert version_in_range("npm", "1.0.1", "1.0.0", None, "1.0.0") is False


def range_entry(
    range_type: str = "SEMVER",
    introduced: str | None = "1.0.0",
    fixed: str | None = "2.0.0",
    last_affected: str | None = None,
    versions: list[str] | None = None,
) -> dict:
    return {
        "id": 1,
        "range_type": range_type,
        "introduced": introduced,
        "fixed": fixed,
        "last_affected": last_affected,
        "versions": versions,
    }


def test_evaluate_ranges_marks_matching_version_affected() -> None:
    affected, certainty, evidence = evaluate_ranges(
        "npm", "1.5.0", [range_entry()]
    )

    assert affected is True
    assert certainty == "range"
    assert evidence is not None and evidence["id"] == 1


def test_evaluate_ranges_handles_disjoint_ranges() -> None:
    ranges = [
        range_entry(introduced="1.0.0", fixed="1.5.0"),
        range_entry(introduced="2.0.0", fixed="2.1.0"),
    ]

    assert evaluate_ranges("npm", "1.4.9", ranges)[0] is True
    assert evaluate_ranges("npm", "1.5.0", ranges)[0] is False
    assert evaluate_ranges("npm", "2.0.5", ranges)[0] is True


def test_evaluate_ranges_uses_explicit_version_list() -> None:
    affected, certainty, _ = evaluate_ranges(
        "npm", "1.2.3", [range_entry(versions=["1.2.3", "1.2.4"], fixed=None)]
    )
    assert (affected, certainty) == (True, "exact_list")


def test_evaluate_ranges_reports_unknown_range_formats() -> None:
    affected, certainty, _ = evaluate_ranges(
        "npm", "1.2.3", [range_entry(range_type="GIT", introduced=None, fixed=None)]
    )

    assert affected is False
    assert certainty == "uncertain"


def test_evaluate_ranges_does_not_invent_a_match_for_version_lists() -> None:
    affected, certainty, _ = evaluate_ranges(
        "npm",
        "1.2.3",
        [range_entry(range_type="VERSIONS", introduced=None, fixed=None, versions=["1.2.4"])],
    )
    assert (affected, certainty) == (False, "range")
