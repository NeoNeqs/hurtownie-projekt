from __future__ import annotations

from pathlib import Path

from scrapy.http import HtmlResponse

from warehouse.sampling import select_random_names
from warehouse.spiders.pypi import (
    package_name_from_url,
    parse_project_page,
    parse_release_history,
    parse_sitemap_locations,
    parse_top_packages,
    sitemap_project_names,
)

FIXTURES = Path(__file__).parent / "fixtures"


def project_response(name: str = "pypi_requests.html") -> HtmlResponse:
    body = (FIXTURES / name).read_bytes()
    return HtmlResponse(
        url="https://pypi.org/project/requests/", body=body, encoding="utf-8"
    )


def test_parse_project_page_reads_header_links_and_license() -> None:
    parsed = parse_project_page(project_response())

    assert parsed["name"] == "requests"
    assert parsed["latest_version"] == "2.34.2"
    assert parsed["description"] == "Python HTTP for Humans."
    assert parsed["license"] == "Apache Software License (Apache-2.0)"
    assert parsed["repository_url"] == "https://github.com/psf/requests"
    assert parsed["homepage_url"] == "https://requests.readthedocs.io"


def test_parse_release_history_reads_versions_and_dates() -> None:
    entries = parse_release_history(project_response())

    assert [entry["version"] for entry in entries] == [
        "2.34.2",
        "2.34.1",
        "2.34.0",
        "2.34.0.dev1",
        "2.33.1",
        "2.33.0",
        "2.32.5",
        "2.32.4",
        "2.32.3",
    ]
    assert entries[0]["published_at"].isoformat() == "2026-05-14T19:25:26+00:00"
    assert all(entry["deprecated"] is False for entry in entries)


def test_release_history_marks_yanked_releases() -> None:
    body = b"""
    <html><body>
      <div class="release-timeline">
        <div class="release release--yanked">
          <p class="release__version"><a href="/project/x/1.0/">1.0</a></p>
          <time datetime="2026-01-01T00:00:00+0000">Jan 1, 2026</time>
        </div>
        <div class="release">
          <p class="release__version"><a href="/project/x/0.9/">0.9</a></p>
          <time datetime="2025-01-01T00:00:00+0000">Jan 1, 2025</time>
        </div>
      </div>
    </body></html>
    """
    entries = parse_release_history(
        HtmlResponse(url="https://pypi.org/project/x/", body=body, encoding="utf-8")
    )

    assert [(entry["version"], entry["deprecated"]) for entry in entries] == [
        ("1.0", True),
        ("0.9", False),
    ]


def test_release_history_reads_version_headings() -> None:
    body = b"""
    <html><body>
      <div class="release-timeline">
        <div class="release release--current">
          <h3 class="release__version"><a href="/project/x/2.0/">2.0</a></h3>
          <time datetime="2026-02-01T00:00:00+0000">Feb 1, 2026</time>
        </div>
        <div class="release release--pre-release">
          <h3 class="release__version"><a href="/project/x/2.1rc1/">2.1rc1</a></h3>
          <time datetime="2026-03-01T00:00:00+0000">Mar 1, 2026</time>
        </div>
      </div>
    </body></html>
    """
    entries = parse_release_history(
        HtmlResponse(url="https://pypi.org/project/x/", body=body, encoding="utf-8")
    )

    assert [entry["version"] for entry in entries] == ["2.0", "2.1rc1"]


def test_package_name_from_url_strips_release_segments() -> None:
    assert package_name_from_url("https://pypi.org/project/requests/") == "requests"
    assert package_name_from_url("https://pypi.org/project/requests/2.34.2/") == "requests"
    assert package_name_from_url("https://pypi.org/search/?q=x") is None


def test_parse_top_packages_sorts_by_downloads() -> None:
    payload = """
    {
      "rows": [
        {"download_count": 10, "project": "small"},
        {"download_count": 1000, "project": "large"},
        {"project": "missing-count"}
      ]
    }
    """
    packages = parse_top_packages(payload)

    assert packages == [
        {"name": "large", "downloads": 1000},
        {"name": "small", "downloads": 10},
    ]


def test_sitemap_helpers_keep_only_project_urls() -> None:
    payload = """
    <urlset>
      <url><loc>https://pypi.org/project/alpha/</loc></url>
      <url><loc>https://pypi.org/project/beta/</loc></url>
      <url><loc>https://pypi.org/help/</loc></url>
    </urlset>
    """
    assert parse_sitemap_locations(payload) == [
        "https://pypi.org/project/alpha/",
        "https://pypi.org/project/beta/",
        "https://pypi.org/help/",
    ]
    assert sitemap_project_names(payload) == ["alpha", "beta"]


def test_select_random_names_is_reproducible_and_excludes_popular() -> None:
    pool = ["alpha", "beta", "gamma", "delta", "epsilon"]

    first = select_random_names(pool, 2, seed=3, exclude={"alpha"})
    second = select_random_names(pool, 2, seed=3, exclude={"alpha"})

    assert first == second
    assert len(first) == 2
    assert "alpha" not in first
