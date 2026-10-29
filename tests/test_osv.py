from __future__ import annotations

from warehouse.osv import (
    _affected_rows,
    _alias_type,
    _range_intervals,
    _severity,
)


def record(
    name: str = "express",
    ecosystem: str = "npm",
    ranges: list[dict] | None = None,
    versions: list[str] | None = None,
) -> dict:
    affected: dict = {"package": {"name": name, "ecosystem": ecosystem}}
    if ranges is not None:
        affected["ranges"] = ranges
    if versions is not None:
        affected["versions"] = versions
    return {"id": "GHSA-test", "affected": [affected]}


def test_range_intervals_handles_multiple_disjoint_fixes() -> None:
    events = [
        {"introduced": "0"},
        {"fixed": "1.2.0"},
        {"introduced": "2.0.0"},
        {"fixed": "2.0.3"},
    ]

    assert _range_intervals(events) == [
        ("0", "1.2.0", None),
        ("2.0.0", "2.0.3", None),
    ]


def test_range_intervals_keeps_open_ended_range() -> None:
    assert _range_intervals([{"introduced": "3.1.0"}]) == [("3.1.0", None, None)]


def test_range_intervals_supports_last_affected_and_limit() -> None:
    assert _range_intervals(
        [{"introduced": "1.0.0"}, {"last_affected": "1.4.2"}, {"introduced": "2.0.0"}, {"limit": "2.1.0"}]
    ) == [("1.0.0", None, "1.4.2"), ("2.0.0", "2.1.0", None)]


def test_range_intervals_ignores_unusable_events() -> None:
    assert _range_intervals([]) == []
    assert _range_intervals([{"foo": "bar"}]) == []


def test_affected_rows_requires_matching_ecosystem_and_name() -> None:
    assert _affected_rows(record(ecosystem="PyPI"), "npm", "express") == []
    assert _affected_rows(record(name="other"), "npm", "express") == []


def test_affected_rows_falls_back_to_explicit_version_list() -> None:
    rows = _affected_rows(record(versions=["1.0.0", "1.0.1"]), "npm", "express")

    assert len(rows) == 1
    assert rows[0]["range_type"] == "VERSIONS"
    assert rows[0]["versions"] == ["1.0.0", "1.0.1"]


def test_affected_rows_expands_each_range_into_intervals() -> None:
    payload = record(
        ranges=[
            {
                "type": "SEMVER",
                "events": [{"introduced": "0"}, {"fixed": "1.0.0"}],
            },
            {
                "type": "SEMVER",
                "events": [{"introduced": "2.0.0"}, {"fixed": "2.1.0"}],
            },
        ]
    )

    rows = _affected_rows(payload, "npm", "express")

    assert [(row["introduced"], row["fixed"]) for row in rows] == [
        ("0", "1.0.0"),
        ("2.0.0", "2.1.0"),
    ]
    assert all(row["range_type"] == "SEMVER" for row in rows)


def test_affected_rows_keeps_versions_alongside_ranges() -> None:
    rows = _affected_rows(
        record(
            ranges=[
                {"type": "ECOSYSTEM", "events": [{"introduced": "1.0.0"}]}
            ],
            versions=["1.0.0", "1.0.1"],
        ),
        "npm",
        "express",
    )

    assert len(rows) == 1
    assert rows[0]["versions"] == ["1.0.0", "1.0.1"]


def test_severity_prefers_a_cvss_vector_and_uppercases_the_label() -> None:
    severity, vector = _severity(
        {
            "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N"}],
            "database_specific": {"severity": "high"},
        }
    )

    assert (severity, vector) == ("HIGH", "CVSS:3.1/AV:N")


def test_severity_is_optional() -> None:
    assert _severity({}) == (None, None)


def test_alias_type_distinguishes_cve_and_ghsa() -> None:
    assert _alias_type("CVE-2024-1234") == "cve"
    assert _alias_type("GHSA-xxxx-yyyy-zzzz") == "ghsa"
    assert _alias_type("OSV-2024-1") == "alias"
