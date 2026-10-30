from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from scrapy.http import HtmlResponse

from warehouse.spiders.crates import (
    crate_name_from_url,
    parse_crate_listing,
    parse_dependencies_page,
    parse_package_page,
    parse_versions_page,
)

FIXTURES = Path(__file__).parent / "fixtures"
TWO_HOURS = timezone(timedelta(hours=2))


def fixture(name: str, url: str) -> HtmlResponse:
    body = (FIXTURES / name).read_bytes()
    return HtmlResponse(url=url, body=body, encoding="utf-8")


def test_parse_crate_listing_reads_names_and_downloads() -> None:
    crates = parse_crate_listing(
        fixture(
            "crates_list.html",
            "https://crates.io/crates?sort=downloads&page=1",
        )
    )

    assert crates[0]["name"] == "hashbrown"
    assert crates[0]["downloads"] == 2_901_475_088
    assert len(crates) == 50
    assert len({crate["name"] for crate in crates}) == len(crates)


def test_parse_package_page_reads_metadata() -> None:
    parsed = parse_package_page(
        fixture("crates_serde.html", "https://crates.io/crates/serde")
    )

    assert parsed["name"] == "serde"
    assert parsed["latest_version"] == "1.0.229"
    assert (
        parsed["description"]
        == "A generic serialization/deserialization framework"
    )
    assert parsed["license"] == "MIT OR Apache-2.0"
    assert parsed["homepage_url"] == "https://serde.rs"
    assert parsed["repository_url"] == "https://github.com/serde-rs/serde"
    assert parsed["downloads"] == 1_615_476_642
    assert parsed["latest_published_at"] == datetime(
        2026, 7, 19, 1, 5, 13, tzinfo=TWO_HOURS
    )


def test_parse_versions_page_reads_dates_and_licenses() -> None:
    entries, total = parse_versions_page(
        fixture(
            "crates_serde_versions.html",
            "https://crates.io/crates/serde/versions",
        )
    )

    assert total == 316
    assert len(entries) == 100
    assert entries[0] == {
        "version": "1.0.229",
        "published_at": datetime(2026, 7, 19, 1, 5, 13, tzinfo=TWO_HOURS),
        "license": "MIT OR Apache-2.0",
    }
    assert entries[-1]["version"] == "1.0.131"
    assert len({entry["version"] for entry in entries}) == len(entries)


def test_parse_dependencies_page_keeps_types_and_optionality() -> None:
    dependencies = parse_dependencies_page(
        fixture(
            "crates_serde_100_dependencies.html",
            "https://crates.io/crates/serde/1.0.0/dependencies",
        )
    )

    assert dependencies == [
        {
            "name": "serde_derive",
            "constraint": "^1.0",
            "dependency_type": "runtime",
            "optional": True,
        },
        {
            "name": "serde_derive",
            "constraint": "^1.0",
            "dependency_type": "dev",
            "optional": False,
        },
    ]


def test_crate_name_from_url_handles_nested_paths() -> None:
    assert crate_name_from_url("https://crates.io/crates/serde") == "serde"
    assert (
        crate_name_from_url("https://crates.io/crates/serde/1.0.0/dependencies")
        == "serde"
    )
    assert crate_name_from_url("https://crates.io/crates?sort=downloads") is None
