from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from scrapy.http import HtmlResponse

from warehouse.spiders.rubygems import (
    parse_dependencies_page,
    parse_gem_index,
    parse_gem_page,
    parse_month,
    parse_stats_page,
    parse_versions_page,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str, url: str) -> HtmlResponse:
    body = (FIXTURES / name).read_bytes()
    return HtmlResponse(url=url, body=body, encoding="utf-8")


def page(body: str, url: str = "https://rubygems.org/gems/x") -> HtmlResponse:
    return HtmlResponse(url=url, body=body.encode("utf-8"), encoding="utf-8")


def test_parse_gem_page_reads_metadata_and_links() -> None:
    parsed = parse_gem_page(
        fixture("rubygems_rails.html", "https://rubygems.org/gems/rails")
    )

    assert parsed["name"] == "rails"
    assert parsed["latest_version"] == "8.1.4"
    assert parsed["latest_published_at"] == datetime(
        2026, 9, 24, tzinfo=timezone.utc
    )
    assert parsed["license"] == "MIT"
    assert parsed["homepage_url"] == "https://rubyonrails.org"
    assert parsed["repository_url"] == "https://github.com/rails/rails/tree/v8.1.4"
    assert parsed["downloads"] == 808_697_693
    assert parsed["description"].startswith("Ruby on Rails is a full-stack")


def test_parse_versions_page_reads_history_and_total() -> None:
    entries, total = parse_versions_page(
        fixture(
            "rubygems_rake_versions.html",
            "https://rubygems.org/gems/rake/versions",
        )
    )

    assert total == 98
    assert [entry["version"] for entry in entries] == [
        "13.4.2",
        "13.4.1",
        "13.4.0",
        "13.3.1",
    ]
    assert entries[-1]["published_at"] == datetime(
        2025, 10, 29, tzinfo=timezone.utc
    )


def test_parse_dependencies_page_separates_runtime_and_development() -> None:
    dependencies = parse_dependencies_page(
        fixture(
            "rubygems_rails_dependencies.html",
            "https://rubygems.org/gems/rails/versions/8.1.4/dependencies",
        )
    )

    runtime = [d for d in dependencies if d["dependency_type"] == "runtime"]
    assert len(runtime) == 13
    assert {d["name"] for d in runtime} >= {"actioncable", "activesupport", "railties"}
    bundler = next(d for d in dependencies if d["name"] == "bundler")
    assert bundler["constraint"] == ">= 1.15.0"


def test_parse_dependencies_page_reads_development_section() -> None:
    body = """
    <html><body>
      <section>
        <h2><span>2</span> Development Dependencies</h2>
        <table><tbody>
          <tr>
            <td><a href="/gems/rspec/versions/3.13.0">rspec <span>3.13.0</span></a></td>
            <td><code>~&gt; 3.13</code></td>
          </tr>
        </tbody></table>
      </section>
    </body></html>
    """
    dependencies = parse_dependencies_page(page(body))

    assert dependencies == [
        {
            "name": "rspec",
            "constraint": "~> 3.13",
            "dependency_type": "dev",
            "optional": False,
        }
    ]


def test_parse_month_accepts_full_and_abbreviated_months() -> None:
    assert parse_month("September 24, 2026") == datetime(
        2026, 9, 24, tzinfo=timezone.utc
    )
    assert parse_month("Aug 13, 2026") == datetime(2026, 8, 13, tzinfo=timezone.utc)
    assert parse_month("not a date") is None


def test_discovery_helpers_ignore_version_links() -> None:
    body = """
    <html><body>
      <a href="/gems/rails">rails</a>
      <a href="/gems/rails/versions/8.1.4">8.1.4</a>
      <a href="/gems/rake?locale=de">rake</a>
      <a href="/gems/rails">rails again</a>
    </body></html>
    """
    response = page(body, url="https://rubygems.org/stats")

    assert parse_stats_page(response) == ["rails", "rake"]
    assert parse_gem_index(response) == ["rails", "rake"]
