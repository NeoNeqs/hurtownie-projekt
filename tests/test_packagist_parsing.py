from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from scrapy.http import HtmlResponse

from warehouse.spiders.packagist import (
    package_name_from_url,
    parse_date,
    parse_package_list,
    parse_package_page,
    parse_popular_page,
    parse_version_list,
    parse_version_panel,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str, url: str) -> HtmlResponse:
    body = (FIXTURES / name).read_bytes()
    return HtmlResponse(url=url, body=body, encoding="utf-8")


def test_parse_package_page_reads_metadata_and_installs() -> None:
    parsed = parse_package_page(
        fixture(
            "packagist_laravel.html",
            "https://packagist.org/packages/laravel/framework",
        )
    )

    assert parsed["name"] == "laravel/framework"
    assert parsed["description"] == "The Laravel Framework."
    assert parsed["license"] == "MIT"
    assert parsed["repository_url"] == "https://github.com/laravel/framework"
    assert parsed["homepage_url"] == "https://laravel.com"
    assert parsed["downloads"] == 607_425_881
    assert parsed["latest_published_at"] == datetime(
        2026, 10, 27, 14, 5, tzinfo=timezone.utc
    )


def test_parse_version_list_marks_dev_and_current_versions() -> None:
    entries = parse_version_list(
        fixture(
            "packagist_laravel.html",
            "https://packagist.org/packages/laravel/framework",
        )
    )

    assert [entry["version"] for entry in entries[:3]] == [
        "13.x-dev",
        "v13.39.0",
        "v13.38.0",
    ]
    assert entries[0]["is_dev"] is True
    assert entries[1]["is_current"] is True
    assert entries[1]["panel_url"] == "https://packagist.org/versions/10931412.json"
    assert all(entry["published_at"] is None for entry in entries)


def test_dev_branches_are_not_treated_as_releases() -> None:
    body = b"""
    <html><body><ul class="versions">
      <li class="details-toggler version" data-load-more="/versions/1.json">
        <a class="version-number">dev-main</a>
      </li>
      <li class="details-toggler version open" data-load-more="/versions/2.json">
        <a class="version-number">v1.0.0</a>
      </li>
    </ul></body></html>
    """
    response = HtmlResponse(
        url="https://packagist.org/packages/a/b", body=body, encoding="utf-8"
    )

    entries = parse_version_list(response)

    assert {entry["version"]: entry["is_dev"] for entry in entries} == {
        "dev-main": True,
        "v1.0.0": False,
    }


def test_parse_popular_page_reads_names_and_downloads() -> None:
    packages = parse_popular_page(
        fixture("packagist_popular.html", "https://packagist.org/explore/popular")
    )

    assert [package["name"] for package in packages] == [
        "symfony/deprecation-contracts",
        "symfony/polyfill-mbstring",
    ]
    assert packages[0]["downloads"] == 989_559_807


def test_parse_version_panel_reads_date_and_dependencies() -> None:
    payload = (FIXTURES / "packagist_version_panel.json").read_text(encoding="utf-8")
    panel = parse_version_panel(payload)

    assert panel["published_at"] == datetime(
        2026, 10, 29, 13, 59, tzinfo=timezone.utc
    )
    runtime = [d for d in panel["dependencies"] if d["dependency_type"] == "runtime"]
    dev = [d for d in panel["dependencies"] if d["dependency_type"] == "dev"]
    assert len(runtime) == 40
    assert len(dev) == 26
    assert {d["name"] for d in runtime} >= {"brick/math", "nesbot/carbon"}
    assert all("/" in d["name"] for d in panel["dependencies"])
    assert next(d for d in runtime if d["name"] == "brick/math")["constraint"].startswith(
        "^0.14.2"
    )


def test_parse_package_list_filters_branch_packages() -> None:
    payload = '{"packageNames": ["a/b", "vendor/c", "0.1.x-dev/x", "bad"]}'

    assert parse_package_list(payload) == ["a/b", "vendor/c"]


def test_parse_date_requires_packagist_format() -> None:
    assert parse_date("2026-10-29 13:59 UTC") == datetime(
        2026, 10, 29, 13, 59, tzinfo=timezone.utc
    )
    assert parse_date("Oct 29, 2026") is None


def test_package_name_from_url_handles_package_paths() -> None:
    assert (
        package_name_from_url("https://packagist.org/packages/laravel/framework")
        == "laravel/framework"
    )
    assert (
        package_name_from_url("https://packagist.org/packages/laravel/framework/stats")
        == "laravel/framework/stats"
    )
    assert package_name_from_url("https://packagist.org/explore/popular") is None
