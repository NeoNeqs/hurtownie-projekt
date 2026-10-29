from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from scrapy.http import HtmlResponse

from warehouse.spiders.hex import (
    package_name_from_url,
    parse_date,
    parse_dependencies_page,
    parse_package_listing,
    parse_package_page,
    parse_versions_page,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str, url: str) -> HtmlResponse:
    body = (FIXTURES / name).read_bytes()
    return HtmlResponse(url=url, body=body, encoding="utf-8")


def test_parse_package_page_reads_metadata_and_links() -> None:
    parsed = parse_package_page(
        fixture("hex_phoenix.html", "https://hex.pm/packages/phoenix")
    )

    assert parsed["name"] == "phoenix"
    assert parsed["latest_version"] == "1.8.16"
    assert parsed["description"] == "Peace of mind from prototype to production"
    assert (
        parsed["repository_url"] == "https://github.com/phoenixframework/phoenix"
    )
    assert parsed["homepage_url"] is None


def test_parse_versions_page_reads_versions_and_dates() -> None:
    entries, total = parse_versions_page(
        fixture(
            "hex_phoenix_versions.html",
            "https://hex.pm/packages/phoenix/versions",
        )
    )

    assert total == 183
    assert entries[0]["version"] == "1.8.16"
    assert entries[0]["published_at"] == datetime(
        2026, 10, 20, 10, 33, tzinfo=timezone.utc
    )
    versions = [entry["version"] for entry in entries]
    assert len(versions) == len(set(versions))
    assert "1.8.15" in versions


def test_parse_versions_page_ignores_version_picker_links() -> None:
    body = """
    <html><body>
      <details class="package-version-picker">
        <summary><span class="font-mono">2.0.0</span></summary>
        <a href="/packages/x/1.0.0">1.0.0</a>
      </details>
      <div class="flex items-start justify-between gap-4 px-4 py-4">
        <div class="min-w-0 flex-1">
          <a href="/packages/x/2.0.0">2.0.0</a>
          <p title="Jan 02, 2026, 09:00">January 2, 2026</p>
        </div>
      </div>
    </body></html>
    """
    response = HtmlResponse(
        url="https://hex.pm/packages/x/versions",
        body=body.encode("utf-8"),
        encoding="utf-8",
    )

    entries, _ = parse_versions_page(response)

    assert entries == [
        {
            "version": "2.0.0",
            "published_at": datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc),
        }
    ]


def test_parse_dependencies_page_reads_requirements_and_optionality() -> None:
    dependencies = parse_dependencies_page(
        fixture(
            "hex_phoenix_dependencies.html",
            "https://hex.pm/packages/phoenix/1.8.16/dependencies",
        )
    )

    bandit = next(d for d in dependencies if d["name"] == "bandit")
    assert bandit == {
        "name": "bandit",
        "constraint": "~> 1.0",
        "dependency_type": "runtime",
        "optional": True,
    }
    assert any(not d["optional"] for d in dependencies)


def test_parse_package_listing_reads_names_and_downloads() -> None:
    packages = parse_package_listing(
        fixture(
            "hex_index.html",
            "https://hex.pm/packages?sort=total_downloads&page=1",
        )
    )

    assert [package["name"] for package in packages] == ["jason", "certifi"]
    assert packages[0]["downloads"] == 215_446_181
    assert packages[0]["recent"] == 10_394_672


def test_parse_date_requires_the_index_format() -> None:
    assert parse_date("Oct 20, 2026, 10:33") == datetime(
        2026, 10, 20, 10, 33, tzinfo=timezone.utc
    )
    assert parse_date("2026-10-20") is None


def test_package_name_from_url_handles_version_paths() -> None:
    assert package_name_from_url("https://hex.pm/packages/phoenix") == "phoenix"
    assert (
        package_name_from_url("https://hex.pm/packages/phoenix/versions")
        == "phoenix"
    )
    assert package_name_from_url("https://hex.pm/packages?sort=name") is None
