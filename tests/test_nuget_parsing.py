from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from scrapy.http import HtmlResponse

from warehouse.spiders.nuget import (
    package_name_from_url,
    parse_compact_int,
    parse_dependencies_page,
    parse_package_listing,
    parse_package_page,
    parse_version_table,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str, url: str) -> HtmlResponse:
    body = (FIXTURES / name).read_bytes()
    return HtmlResponse(url=url, body=body, encoding="utf-8")


def test_parse_package_listing_reads_names_and_downloads() -> None:
    packages = parse_package_listing(
        fixture(
            "nuget_search.html",
            "https://www.nuget.org/packages?page=1&sortBy=totalDownloads-desc",
        )
    )

    assert packages[0] == {
        "name": "Newtonsoft.Json",
        "downloads": 9_651_476_289,
    }
    assert len(packages) == 20


def test_parse_package_page_reads_metadata() -> None:
    parsed = parse_package_page(
        fixture(
            "nuget_newtonsoft.html",
            "https://www.nuget.org/packages/Newtonsoft.Json",
        )
    )

    assert parsed["name"] == "Newtonsoft.Json"
    assert parsed["latest_version"] == "13.0.4"
    assert parsed["description"].startswith("Json.NET is a popular")
    assert parsed["license"] == "MIT"
    assert parsed["homepage_url"] == "https://www.newtonsoft.com/json"
    assert parsed["repository_url"] == "https://github.com/JamesNK/Newtonsoft.Json"
    assert parsed["downloads"] == 9_700_000_000


def test_parse_version_table_reads_the_full_history() -> None:
    entries = parse_version_table(
        fixture(
            "nuget_newtonsoft_versions.html",
            "https://www.nuget.org/packages/Newtonsoft.Json/13.0.4/versiontable",
        )
    )

    assert len(entries) == 55
    assert entries[0]["version"] == "14.0.1-beta2"
    assert entries[0]["is_prerelease"] is True
    assert entries[0]["published_at"] == datetime(
        2026, 9, 21, tzinfo=timezone.utc
    )
    assert entries[-1]["version"] == "3.5.8"
    assert len({entry["version"] for entry in entries}) == len(entries)


def test_parse_version_table_reads_the_initial_page() -> None:
    entries = parse_version_table(
        fixture(
            "nuget_newtonsoft.html",
            "https://www.nuget.org/packages/Newtonsoft.Json",
        )
    )

    assert [entry["version"] for entry in entries][:3] == [
        "14.0.1-beta2",
        "14.0.1-beta1",
        "13.0.4",
    ]


def test_parse_dependencies_page_merges_framework_groups() -> None:
    dependencies = parse_dependencies_page(
        fixture(
            "nuget_logging_1000.html",
            "https://www.nuget.org/packages/Microsoft.Extensions.Logging/10.0.0",
        )
    )

    names = [dependency["name"] for dependency in dependencies]
    assert names == [
        "Microsoft.Bcl.AsyncInterfaces",
        "Microsoft.Extensions.DependencyInjection",
        "Microsoft.Extensions.Logging.Abstractions",
        "Microsoft.Extensions.Options",
        "System.Diagnostics.DiagnosticSource",
        "System.ValueTuple",
    ]
    assert len(names) == len(set(names))
    value_tuple = next(
        dependency
        for dependency in dependencies
        if dependency["name"] == "System.ValueTuple"
    )
    assert value_tuple == {
        "name": "System.ValueTuple",
        "constraint": ">= 4.6.1",
        "dependency_type": "runtime",
        "optional": False,
    }


def test_parse_dependencies_page_reads_package_links() -> None:
    dependencies = parse_dependencies_page(
        fixture(
            "nuget_newtonsoft_1303.html",
            "https://www.nuget.org/packages/Newtonsoft.Json/13.0.3",
        )
    )

    assert {
        "name": "NETStandard.Library",
        "constraint": ">= 1.6.1",
        "dependency_type": "runtime",
        "optional": False,
    } in dependencies


def test_parse_compact_int_expands_gallery_units() -> None:
    assert parse_compact_int("9.7B") == 9_700_000_000
    assert parse_compact_int("297.3M") == 297_300_000
    assert parse_compact_int("1,234") == 1_234


def test_package_name_from_url_handles_version_paths() -> None:
    assert (
        package_name_from_url("https://www.nuget.org/packages/Newtonsoft.Json")
        == "Newtonsoft.Json"
    )
    assert (
        package_name_from_url(
            "https://www.nuget.org/packages/Newtonsoft.Json/13.0.4"
        )
        == "Newtonsoft.Json"
    )
    assert package_name_from_url("https://www.nuget.org/packages?page=2") is None
