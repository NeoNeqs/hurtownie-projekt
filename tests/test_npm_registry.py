from __future__ import annotations

from warehouse.spiders.npm_registry import (
    RegistryCandidate,
    choose_sample,
    packument_url,
    parse_packument,
    parse_search_payload,
    version_dependencies,
)


def test_parse_search_payload_reads_names_and_monthly_downloads() -> None:
    payload = {
        "objects": [
            {"package": {"name": "react"}, "downloads": {"monthly": 100}},
            {"package": {"name": "vue"}, "downloads": {"monthly": 50}},
            {"package": {}},
        ]
    }

    candidates = parse_search_payload(payload, "popular", 2)

    assert [candidate.name for candidate in candidates] == ["react", "vue"]
    assert candidates[0].downloads == 100
    assert candidates[0].category == "popular"
    assert candidates[0].query_index == 2


def test_version_dependencies_maps_every_dependency_kind() -> None:
    dependencies = version_dependencies(
        {
            "dependencies": {"runtime": "^1"},
            "devDependencies": {"dev": "^2"},
            "peerDependencies": {"peer": "^3"},
            "optionalDependencies": {"optional": "^4"},
            "bundledDependencies": ["bundled"],
        }
    )

    by_name = {dependency["name"]: dependency for dependency in dependencies}
    assert by_name["runtime"]["dependency_type"] == "runtime"
    assert by_name["dev"]["dependency_type"] == "dev"
    assert by_name["peer"]["dependency_type"] == "peer"
    assert by_name["optional"]["dependency_type"] == "optional"
    assert by_name["optional"]["optional"] is True
    assert by_name["bundled"]["dependency_type"] == "bundled"
    assert by_name["bundled"]["constraint"] is None


def test_parse_packument_reads_metadata_and_release_history() -> None:
    payload = {
        "name": "demo",
        "description": "A demo package",
        "homepage": "https://example.test",
        "repository": {"type": "git", "url": "git+https://github.com/x/y.git"},
        "license": {"type": "MIT"},
        "dist-tags": {"latest": "2.0.0"},
        "time": {
            "created": "2020-01-01T00:00:00Z",
            "1.0.0": "2020-01-02T00:00:00Z",
            "2.0.0": "2021-02-03T00:00:00Z",
        },
        "versions": {
            "1.0.0": {"dependencies": {"a": "^1"}},
            "2.0.0": {"dependencies": {"a": "^2"}},
        },
    }

    metadata, entries = parse_packument(payload)

    assert metadata["name"] == "demo"
    assert metadata["latest_version"] == "2.0.0"
    assert metadata["license"] == "MIT"
    assert metadata["repository_url"] == "git+https://github.com/x/y.git"
    assert metadata["first_published_at"].isoformat() == "2020-01-01T00:00:00+00:00"
    assert metadata["latest_published_at"].isoformat() == "2021-02-03T00:00:00+00:00"
    assert [entry["version"] for entry in entries] == ["1.0.0", "2.0.0"]
    assert entries[1]["dependencies"][0]["name"] == "a"


def test_choose_sample_sorts_popular_and_keeps_random_disjoint() -> None:
    candidates = [
        RegistryCandidate("low", "popular", 0, 0, 10),
        RegistryCandidate("high", "popular", 0, 1, 100),
        RegistryCandidate("mid", "popular", 1, 0, 50),
        RegistryCandidate("random-a", "random", 0, 0, None),
        RegistryCandidate("random-b", "random", 1, 0, None),
    ]

    sample = choose_sample(candidates, popular_count=2, random_count=2, seed=1)

    assert [candidate.name for candidate in sample[:2]] == ["high", "mid"]
    assert {candidate.name for candidate in sample[2:]} == {"random-a", "random-b"}
    assert all(candidate.category == "random" for candidate in sample[2:])


def test_packument_url_encodes_scoped_names() -> None:
    assert packument_url("@babel/core") == "https://registry.npmjs.org/@babel%2Fcore"
    assert packument_url("react") == "https://registry.npmjs.org/react"
