"""PyPI collector backed by pypi.org project pages.

Discovery uses the published project sitemap for the random comparison group
and the public top-PyPI-packages dataset for the popular group. Metadata and
release history come from the project page itself. PyPI does not render
per-release dependencies on the website, so dependency rows are not collected
for this ecosystem yet.

Fastly serves an uncached project page behind a "Client Challenge"
interstitial. It is treated as a block, not something to work around: those
pages are recorded as collection errors and excluded from the sample results.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

from scrapy import Request
from scrapy.http import Response

from warehouse.processing import parse_timestamp
from warehouse.sampling import select_random_names, select_versions
from warehouse.spiders.base import BrowserSpider

BASE_URL = "https://pypi.org"
SITEMAP_INDEX_URL = f"{BASE_URL}/sitemap.xml"
TOP_PACKAGES_URL = (
    "https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json"
)
PROJECT_PREFIX = "/project/"
REPOSITORY_LABELS = {"source", "repository", "code", "github", "gitlab", "bitbucket"}
HOMEPAGE_LABELS = {"homepage", "home page", "documentation", "docs", "website"}


def package_url(name: str) -> str:
    return f"{BASE_URL}{PROJECT_PREFIX}{name}/"


def package_name_from_url(url: str) -> str | None:
    path = urlparse(url).path
    if not path.startswith(PROJECT_PREFIX):
        return None
    name = unquote(path[len(PROJECT_PREFIX) :]).strip("/")
    return name.split("/")[0] or None


def parse_top_packages(payload: str) -> list[dict[str, Any]]:
    rows = json.loads(payload).get("rows") or []
    packages = [
        {"name": row["project"], "downloads": int(row["download_count"])}
        for row in rows
        if row.get("project") and row.get("download_count") is not None
    ]
    packages.sort(key=lambda entry: entry["downloads"], reverse=True)
    return packages


def parse_sitemap_locations(payload: str) -> list[str]:
    return [
        re.sub(r"<[^>]+>", "", match).strip()
        for match in re.findall(r"<loc>(.*?)</loc>", payload, re.S)
    ]


def sitemap_project_names(payload: str) -> list[str]:
    names = (
        package_name_from_url(location)
        for location in parse_sitemap_locations(payload)
    )
    return [name for name in names if name]


def _text(response: Response, xpath: str) -> str | None:
    value = response.xpath(f"string({xpath})").get()
    return value.strip() if value and value.strip() else None


def parse_project_page(response: Response) -> dict[str, Any]:
    name = package_name_from_url(response.url)
    heading = _text(response, "//h1[contains(@class, 'project-header__name')]")
    latest_version = None
    if heading:
        prefix = f"{name} " if name else ""
        latest_version = heading[len(prefix) :].strip() if heading.startswith(prefix) else heading

    links: dict[str, str] = {}
    for anchor in response.xpath(
        "//h2[normalize-space()='Project links']"
        "/parent::div[contains(@class, 'sidebar-section')]//a[@href]"
    ):
        href = anchor.xpath("@href").get()
        label = " ".join(anchor.xpath("string(.)").get().split()).lower()
        if href and label:
            links.setdefault(label, href)

    repository_url = next(
        (href for label, href in links.items() if label in REPOSITORY_LABELS), None
    )
    homepage_url = next(
        (href for label, href in links.items() if label in HOMEPAGE_LABELS), None
    )

    return {
        "name": name,
        "description": _text(response, "//p[contains(@class, 'project-header__summary')]"),
        "latest_version": latest_version,
        "license": _text(
            response,
            "//h2[normalize-space()='License']/following-sibling::div[1]//p[1]",
        ),
        "repository_url": repository_url,
        "homepage_url": homepage_url,
    }


def parse_release_history(response: Response) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for node in response.xpath(
        "//div[contains(@class, 'release-timeline')]"
        "/div[contains(concat(' ', normalize-space(@class), ' '), ' release ')]"
    ):
        version = node.xpath(
            "string(.//*[contains(@class, 'release__version')]/a[1])"
        ).get()
        if not version or not version.strip():
            continue
        version = version.strip()
        if any(entry["version"] == version for entry in entries):
            continue
        classes = node.xpath("@class").get() or ""
        entries.append(
            {
                "version": version,
                "published_at": parse_timestamp(node.xpath(".//time/@datetime").get()),
                "deprecated": "release--yanked" in classes,
            }
        )
    return entries


class PyPISpider(BrowserSpider):
    name = "pypi"
    ecosystem = "PyPI"
    source = "pypi.org"
    selenium_meta = {
        "selenium": True,
        "selenium_wait_for": "h1.project-header__name",
        "selenium_wait": 0,
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.popular: list[dict[str, Any]] = []
        self.random_pool: list[str] = []
        self._pending_discovery = 0
        self._scheduled = False
        self._popular_ready = False
        self._random_ready = False

    async def start(self) -> AsyncIterator[Request]:
        popular_count = self.settings.getint("PYPI_POPULAR_COUNT", 25)
        random_count = self.settings.getint("PYPI_RANDOM_COUNT", 5)

        requests: list[Request] = []
        if popular_count > 0:
            self._pending_discovery += 1
            requests.append(
                Request(
                    TOP_PACKAGES_URL,
                    callback=self.parse_top_packages,
                    errback=self.discovery_errback,
                    meta={"pypi_discovery": "popular", "selenium": False},
                )
            )
        else:
            self._popular_ready = True

        if random_count > 0:
            self._pending_discovery += 1
            requests.append(
                Request(
                    SITEMAP_INDEX_URL,
                    callback=self.parse_sitemap_index,
                    errback=self.discovery_errback,
                    meta={"pypi_discovery": "random", "selenium": False},
                )
            )
        else:
            self._random_ready = True

        if not requests:
            self._schedule_packages()
            return
        for request in requests:
            yield request

    def parse_top_packages(self, response: Response) -> Any:
        try:
            self.popular = parse_top_packages(response.text)
        except (ValueError, KeyError) as exc:
            self.record_error(
                response.url, "parse", "InvalidPayload", f"top package list: {exc}"
            )
        self._popular_ready = True
        return self._discovery_finished()

    def parse_sitemap_index(self, response: Response) -> Any:
        locations = parse_sitemap_locations(response.text)
        if not locations:
            self.record_error(response.url, "parse", "MissingField", "no sitemap shards")
            self._random_ready = True
            self._discovery_finished()
            return

        shard_count = max(
            1, min(self.settings.getint("PYPI_SITEMAP_SHARDS", 5), len(locations))
        )
        seed = self.settings.getint("PYPI_SAMPLE_SEED", 1)
        shards = random.Random(seed).sample(sorted(locations), shard_count)
        self._pending_discovery -= 1
        self._pending_discovery += shard_count

        for shard in shards:
            yield Request(
                shard,
                callback=self.parse_sitemap_shard,
                errback=self.discovery_errback,
                meta={"pypi_discovery": "random", "selenium": False},
            )

    def parse_sitemap_shard(self, response: Response) -> Any:
        self.random_pool.extend(sitemap_project_names(response.text))
        self._random_ready = True
        return self._discovery_finished()

    def discovery_errback(self, failure: Any) -> Any:
        super().errback(failure)
        request = failure.request
        if request.meta.get("pypi_discovery") == "popular":
            self._popular_ready = True
        else:
            self._random_ready = True
        self._pending_discovery -= 1
        if self._pending_discovery <= 0:
            return self._schedule_packages()
        return None

    def _discovery_finished(self) -> Any:
        self._pending_discovery -= 1
        if self._pending_discovery > 0:
            return None
        if self._popular_ready and self._random_ready:
            return self._schedule_packages()
        return None

    def _schedule_packages(self) -> Any:
        if self._scheduled:
            return None
        self._scheduled = True

        popular_count = self.settings.getint("PYPI_POPULAR_COUNT", 25)
        random_count = self.settings.getint("PYPI_RANDOM_COUNT", 5)
        seed = self.settings.getint("PYPI_SAMPLE_SEED", 1)

        popular = self.popular[:popular_count]
        popular_names = {entry["name"] for entry in popular}
        random_names = select_random_names(
            self.random_pool, random_count, seed, popular_names
        )

        sample: list[dict[str, Any]] = [
            {
                "name": entry["name"],
                "category": "popular",
                "rank": rank,
                "seed": None,
                "downloads": entry["downloads"],
            }
            for rank, entry in enumerate(popular)
        ]
        sample.extend(
            {
                "name": name,
                "category": "random",
                "rank": rank,
                "seed": seed,
                "downloads": None,
            }
            for rank, name in enumerate(random_names)
        )

        crawl = self.crawler
        for entry in sample:
            request = self.browser_request(
                package_url(entry["name"]),
                callback=self.parse_package,
                meta={
                    "pypi_name": entry["name"],
                    "pypi_category": entry["category"],
                    "pypi_rank": entry["rank"],
                    "pypi_seed": entry["seed"],
                    "pypi_downloads": entry["downloads"],
                },
            )
            crawl.engine.crawl(request)

        self.logger.info(
            "Sampled %d PyPI packages (%d popular, %d random) from %d candidates",
            len(sample),
            len(popular),
            len(random_names),
            len(self.random_pool),
        )
        return None

    def parse_package(self, response: Response) -> Any:
        name = response.meta["pypi_name"]
        metadata = parse_project_page(response)
        versions = parse_release_history(response)

        if not versions:
            self.record_error(
                response.url,
                "parse",
                "NoVersionsStored",
                f"release history not rendered for {name}",
            )
            return None

        limit = self.settings.getint("PYPI_MAX_VERSIONS", 10)
        strategy = self.settings.get("PYPI_VERSION_SELECTION", "spread")
        selected = select_versions(versions, limit, strategy)

        latest = metadata.get("latest_version")
        if latest and all(entry["version"] != latest for entry in selected):
            latest_entry = next(
                (entry for entry in versions if entry["version"] == latest), None
            )
            if latest_entry:
                selected.insert(0, latest_entry)

        dates = [
            entry["published_at"]
            for entry in versions
            if entry.get("published_at") is not None
        ]
        latest_published = dates[0] if dates else None
        if latest:
            latest_entry = next(
                (entry for entry in versions if entry["version"] == latest), None
            )
            if latest_entry and latest_entry.get("published_at"):
                latest_published = latest_entry["published_at"]

        for entry in selected:
            entry["dependencies"] = []

        item = self.load_package(
            {
                "ecosystem": self.ecosystem,
                "name": name,
                "sampling_category": response.meta["pypi_category"],
                "sampling_rank": response.meta["pypi_rank"],
                "sampling_seed": response.meta["pypi_seed"],
                "description": metadata.get("description"),
                "homepage_url": metadata.get("homepage_url"),
                "repository_url": metadata.get("repository_url"),
                "license": metadata.get("license"),
                "latest_version": latest or (versions[0]["version"] if versions else None),
                "first_published_at": min(dates) if dates else None,
                "latest_published_at": latest_published,
                "downloads": response.meta.get("pypi_downloads"),
                "downloads_period": "last-month",
                "source": self.source,
                "source_url": response.url,
                "collection_method": "selenium",
                "collected_at": datetime.now(timezone.utc),
            }
        )
        item["versions"] = sorted(
            selected,
            key=lambda entry: entry.get("published_at")
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return item
