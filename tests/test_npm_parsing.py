from __future__ import annotations

from pathlib import Path

from scrapy.http import HtmlResponse

from warehouse.spiders.npm import (
    Candidate,
    dependencies_url,
    package_url,
    parse_dependencies_page,
    parse_package_page,
    parse_search_results,
    parse_versions_page,
    select_sample,
)

FIXTURES = Path(__file__).parent / "fixtures"


def response(name: str, url: str) -> HtmlResponse:
    body = (FIXTURES / name).read_bytes()
    return HtmlResponse(url=url, body=body, encoding="utf-8")


def test_parse_package_page_reads_sidebar_and_meta() -> None:
    parsed = parse_package_page(
        response("npm_express_package.html", "https://www.npmjs.com/package/express")
    )

    assert parsed["name"] == "express"
    assert parsed["latest_version"] == "5.3.0"
    assert parsed["license"] == "MIT"
    assert parsed["downloads"] == 162_948_095
    assert parsed["repository_url"] == "https://github.com/expressjs/express"
    assert parsed["homepage_url"] == "https://expressjs.com/"
    assert parsed["description"] == "Fast, unopinionated, minimalist web framework."
    assert parsed["latest_published_at"].isoformat() == "2026-10-09T12:56:54.505000+00:00"


def test_parse_versions_page_reports_partial_history() -> None:
    entries, total = parse_versions_page(
        response(
            "npm_express_versions.html",
            "https://www.npmjs.com/package/express?activeTab=versions",
        )
    )

    assert total == 290
    assert len(entries) == 117
    assert entries[0]["version"] == "5.3.0"
    assert entries[0]["published_at"] is not None


def test_parse_dependencies_page_separates_runtime_and_dev() -> None:
    parsed = parse_dependencies_page(
        response(
            "npm_express_v4_dependencies.html",
            "https://www.npmjs.com/package/express/v/4.18.2?activeTab=dependencies",
        )
    )

    assert parsed is not None
    assert parsed["license"] == "MIT"
    runtime = [d for d in parsed["dependencies"] if d["dependency_type"] == "runtime"]
    dev = [d for d in parsed["dependencies"] if d["dependency_type"] == "dev"]
    assert len(runtime) == 31
    assert len(dev) == 17
    assert {d["name"] for d in runtime} >= {"accepts", "body-parser", "qs"}
    assert all(d["constraint"] is None for d in parsed["dependencies"])


def test_parse_dependencies_page_accepts_zero_dependencies() -> None:
    parsed = parse_dependencies_page(
        response(
            "npm_react_v19_dependencies.html",
            "https://www.npmjs.com/package/react/v/19.3.1?activeTab=dependencies",
        )
    )

    assert parsed is not None
    assert parsed["dependencies"] == []
    assert parsed["license"] == "MIT"


def test_parse_dependencies_page_without_section_returns_none() -> None:
    empty = HtmlResponse(
        url="https://www.npmjs.com/package/react/v/19.3.1?activeTab=dependencies",
        body=b"<html><body><h1>react</h1></body></html>",
        encoding="utf-8",
    )
    assert parse_dependencies_page(empty) is None


def test_parse_search_results_skips_version_links_and_duplicates() -> None:
    page = HtmlResponse(
        url="https://www.npmjs.com/search?q=framework",
        body=b"""
        <html><body>
          <a href="/package/express">express</a>
          <a href="/package/express">express again</a>
          <a href="/package/express/v/5.3.0">a version</a>
          <a href="/package/%40scope%2Fpkg">scoped</a>
        </body></html>
        """,
        encoding="utf-8",
    )
    assert parse_search_results(page) == ["express", "@scope/pkg"]


def test_urls_keep_scoped_names_intact() -> None:
    assert package_url("@scope/pkg") == "https://www.npmjs.com/package/@scope/pkg"
    assert (
        dependencies_url("@scope/pkg", "1.0.0")
        == "https://www.npmjs.com/package/@scope/pkg/v/1.0.0?activeTab=dependencies"
    )


def test_select_sample_is_reproducible_and_disjoint() -> None:
    candidates = [
        Candidate("a", "popular", 0, 0, 0),
        Candidate("b", "popular", 1, 0, 0),
        Candidate("c", "popular", 0, 0, 1),
        Candidate("a", "random", 0, 0, 0),
        Candidate("d", "random", 0, 0, 1),
        Candidate("e", "random", 1, 0, 0),
        Candidate("f", "random", 1, 0, 1),
    ]

    first = select_sample(candidates, popular_count=2, random_count=2, seed=7)
    second = select_sample(candidates, popular_count=2, random_count=2, seed=7)

    assert [c.name for c in first] == [c.name for c in second]
    assert [c.category for c in first[:2]] == ["popular", "popular"]
    assert len({c.name for c in first}) == 4
    assert {c.name for c in first} & {"a"} == {"a"}


def test_select_sample_never_reuses_popular_package_for_random_group() -> None:
    candidates = [
        Candidate("shared", "popular", 0, 0, 0),
        Candidate("shared", "random", 0, 0, 0),
        Candidate("other", "random", 0, 0, 1),
    ]
    chosen = select_sample(candidates, popular_count=1, random_count=1, seed=1)

    assert [c.category for c in chosen] == ["popular", "random"]
    assert chosen[1].name == "other"
