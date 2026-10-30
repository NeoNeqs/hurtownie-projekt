"""RubyGems collector backed by rubygems.org pages.

Discovery uses the stats page for the popular group and randomly chosen gem
index pages for the comparison group. The gem page supplies metadata, the
versions page the full release history, and each selected version's
dependencies page its runtime and development dependencies.

The gem index is alphabetical, so random index pages pull from a few letter
ranges rather than the whole catalogue. The sampling limitation is recorded by
the collector instead of being hidden.
"""

from __future__ import annotations

import random
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from scrapy import Request
from scrapy.http import Response

from warehouse.sampling import select_random_names, select_versions
from warehouse.spiders.base import BrowserSpider

BASE_URL = "https://rubygems.org"
STATS_URL = f"{BASE_URL}/stats"
STATS_PER_PAGE = 10
GEMS_PER_PAGE = 30
MONTH_FORMATS = ("%B %d, %Y", "%b %d, %Y")


def gem_url(name: str) -> str:
    return f"{BASE_URL}/gems/{name}"


def stats_url(page: int) -> str:
    return STATS_URL if page <= 1 else f"{STATS_URL}?page={page}"


def versions_url(name: str) -> str:
    return f"{BASE_URL}/gems/{name}/versions"


def dependencies_url(name: str, version: str) -> str:
    return f"{BASE_URL}/gems/{name}/versions/{version}/dependencies"


def index_page_url(page: int) -> str:
    return f"{BASE_URL}/gems?page={page}"


def _text(response: Response, xpath: str) -> str | None:
    value = response.xpath(f"string({xpath})").get()
    return value.strip() if value and value.strip() else None


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def parse_month(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for fmt in MONTH_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_stats_page(response: Response) -> list[str]:
    return parse_gem_index(response)


def parse_gem_index(response: Response) -> list[str]:
    names: list[str] = []
    for href in response.xpath("//a[starts-with(@href, '/gems/')]/@href").getall():
        path = href.split("?")[0]
        if path.count("/") != 2:
            continue
        name = unquote(path[len("/gems/") :]).strip("/")
        if name and "/" not in name and name not in names:
            names.append(name)
    return names


def parse_gem_page(response: Response) -> dict[str, Any]:
    heading = "//h1[contains(@class, 'text-h3')]"
    name = _text(response, heading)
    version_block = f"{heading}/following-sibling::div[1]"

    license_text = _text(
        response,
        "//span[contains(@class, 'sr-only') and contains(., 'License')]/parent::span",
    )
    if license_text and ":" in license_text:
        license_text = license_text.split(":", 1)[1].strip()

    return {
        "name": name,
        "description": _text(response, "//div[@id='markup']//p[1]")
        or response.xpath("//meta[@name='description']/@content").get(),
        "latest_version": _text(response, f"{version_block}//code[1]"),
        "latest_published_at": parse_month(
            _text(response, f"{version_block}//div[contains(@class, 'text-b3')][1]")
        ),
        "license": license_text,
        "homepage_url": response.xpath("//a[@id='home']/@href").get(),
        "repository_url": response.xpath("//a[@id='code']/@href").get(),
        "downloads": _parse_int(
            _text(
                response,
                "//h2[normalize-space()='Total downloads']/following-sibling::p[1]",
            )
        ),
    }


def parse_versions_page(
    response: Response,
) -> tuple[list[dict[str, Any]], int | None]:
    entries: dict[str, dict[str, Any]] = {}
    for group in response.xpath(
        "//li[div/span[contains(@class, 'font-mono')]]"
    ):
        version = group.xpath(
            "string(./div/span[contains(@class, 'font-mono')][1])"
        ).get()
        if not version or not version.strip():
            continue
        version = version.strip()
        dates = [
            parse_month(date.strip())
            for date in group.xpath(
                ".//span[@data-testid='version-date']/text()"
            ).getall()
        ]
        dates = [date for date in dates if date is not None]
        published = min(dates) if dates else None
        existing = entries.get(version)
        if existing is None:
            entries[version] = {"version": version, "published_at": published}
        elif published and (
            existing["published_at"] is None or published < existing["published_at"]
        ):
            existing["published_at"] = published

    total = None
    for label in response.xpath("//*[contains(text(), 'versions since')]/text()").getall():
        match = re.search(r"([\d,]+)\s+versions since", label or "")
        if match:
            total = _parse_int(match.group(1))
            break
    return list(entries.values()), total


def parse_dependencies_page(response: Response) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    for section in response.xpath(
        "//section[h2[contains(., 'Dependencies')]]"
    ):
        heading = " ".join(section.xpath("string(h2)").get().split()).lower()
        if "runtime" in heading:
            dependency_type, optional = "runtime", False
        elif "development" in heading:
            dependency_type, optional = "dev", False
        else:
            continue

        for row in section.xpath(".//tbody/tr"):
            href = row.xpath("string(td[1]//a/@href)").get()
            match = re.match(r"^/gems/([^/]+)/versions/", href or "")
            if not match:
                continue
            constraints = [
                value.strip()
                for value in row.xpath("td[2]//code/text()").getall()
                if value.strip()
            ]
            dependencies.append(
                {
                    "name": unquote(match.group(1)),
                    "constraint": ", ".join(constraints) or None,
                    "dependency_type": dependency_type,
                    "optional": optional,
                }
            )
    return dependencies


class RubyGemsSpider(BrowserSpider):
    name = "rubygems"
    ecosystem = "RubyGems"
    source = "rubygems.org"
    selenium_meta = {
        "selenium": True,
        "selenium_wait_for": "h1",
        "selenium_wait": 0,
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.popular: list[str] = []
        self.random_pool: list[str] = []
        self._pending_discovery = 0
        self._popular_ready = False
        self._random_ready = False
        self._scheduled = False
        self._drafts: dict[str, dict[str, Any]] = {}

    async def start(self) -> AsyncIterator[Request]:
        popular_count = self.settings.getint("RUBYGEMS_POPULAR_COUNT", 25)
        random_count = self.settings.getint("RUBYGEMS_RANDOM_COUNT", 5)
        seed = self.settings.getint("RUBYGEMS_SAMPLE_SEED", 1)

        requests: list[Request] = []
        if popular_count > 0:
            pages = max(1, -(-popular_count // STATS_PER_PAGE))
            self._pending_discovery += pages
            for page in range(1, pages + 1):
                requests.append(
                    Request(
                        stats_url(page),
                        callback=self.parse_stats,
                        errback=self.discovery_errback,
                        meta={"rubygems_discovery": "popular", "selenium": False},
                    )
                )
        else:
            self._popular_ready = True

        pages = max(0, self.settings.getint("RUBYGEMS_RANDOM_PAGES", 3))
        if random_count > 0 and pages > 0:
            page_limit = max(1, self.settings.getint("RUBYGEMS_PAGE_LIMIT", 1000))
            chosen = random.Random(seed).sample(range(1, page_limit + 1), pages)
            self._pending_discovery += len(chosen)
            for page in chosen:
                requests.append(
                    Request(
                        index_page_url(page),
                        callback=self.parse_index,
                        errback=self.discovery_errback,
                        meta={"rubygems_discovery": "random", "selenium": False},
                    )
                )
        else:
            self._random_ready = True

        if not requests:
            self._schedule_packages()
            return
        for request in requests:
            yield request

    def parse_stats(self, response: Response) -> Any:
        self.popular.extend(parse_stats_page(response))
        self._popular_ready = True
        return self._discovery_finished()

    def parse_index(self, response: Response) -> Any:
        self.random_pool.extend(parse_gem_index(response))
        self._random_ready = True
        return self._discovery_finished()

    def discovery_errback(self, failure: Any) -> Any:
        super().errback(failure)
        if failure.request.meta.get("rubygems_discovery") == "popular":
            self._popular_ready = True
        else:
            self._random_ready = True
        return self._discovery_finished()

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

        popular_count = self.settings.getint("RUBYGEMS_POPULAR_COUNT", 25)
        random_count = self.settings.getint("RUBYGEMS_RANDOM_COUNT", 5)
        seed = self.settings.getint("RUBYGEMS_SAMPLE_SEED", 1)

        popular = self.popular[:popular_count]
        popular_names = set(popular)
        random_names = select_random_names(
            self.random_pool, random_count, seed, popular_names
        )

        sample = [
            {"name": name, "category": "popular", "rank": rank, "seed": None}
            for rank, name in enumerate(popular)
        ]
        sample.extend(
            {
                "name": name,
                "category": "random",
                "rank": rank,
                "seed": seed,
            }
            for rank, name in enumerate(random_names)
        )

        crawl = self.crawler
        for entry in sample:
            request = self.browser_request(
                gem_url(entry["name"]),
                callback=self.parse_gem,
                meta={
                    "rubygems_name": entry["name"],
                    "rubygems_category": entry["category"],
                    "rubygems_rank": entry["rank"],
                    "rubygems_seed": entry["seed"],
                },
            )
            crawl.engine.crawl(request)

        self.logger.info(
            "Sampled %d RubyGems packages (%d popular, %d random) from %d candidates",
            len(sample),
            len(popular),
            len(random_names),
            len(self.random_pool),
        )
        return None

    def parse_gem(self, response: Response) -> Any:
        name = response.meta["rubygems_name"]
        metadata = parse_gem_page(response)
        draft = {
            "name": name,
            "category": response.meta["rubygems_category"],
            "rank": response.meta["rubygems_rank"],
            "seed": response.meta["rubygems_seed"],
            "metadata": metadata,
            "versions": [],
            "pending": 0,
        }
        self._drafts[name] = draft

        if not metadata.get("latest_version"):
            self.record_error(
                response.url,
                "parse",
                "MissingField",
                f"latest version not found for {name}",
            )

        return self.browser_request(
            versions_url(name),
            callback=self.parse_versions,
            errback=self.package_errback,
            meta={"rubygems_key": name, "selenium_wait_for": "h1"},
        )

    def parse_versions(self, response: Response) -> Any:
        draft = self._drafts.get(response.meta["rubygems_key"])
        if draft is None:
            return None

        entries, total = parse_versions_page(response)
        if not entries:
            self.record_error(
                response.url,
                "parse",
                "NoVersionsStored",
                f"version history not rendered for {draft['name']}",
            )
            return self._finish(draft)

        limit = self.settings.getint("RUBYGEMS_MAX_VERSIONS", 10)
        strategy = self.settings.get("RUBYGEMS_VERSION_SELECTION", "spread")
        selected = select_versions(entries, limit, strategy)

        latest = draft["metadata"].get("latest_version")
        if latest and all(entry["version"] != latest for entry in selected):
            latest_entry = next(
                (entry for entry in entries if entry["version"] == latest), None
            )
            if latest_entry:
                selected.insert(0, latest_entry)

        dates = [
            entry["published_at"]
            for entry in entries
            if entry.get("published_at") is not None
        ]
        draft["metadata"]["first_published_at"] = min(dates) if dates else None
        draft["versions"] = selected
        if not selected:
            return self._finish(draft)

        requests = []
        for entry in selected:
            draft["pending"] += 1
            requests.append(
                self.browser_request(
                    dependencies_url(draft["name"], entry["version"]),
                    callback=self.parse_dependencies,
                    errback=self.version_errback,
                    meta={
                        "rubygems_key": draft["name"],
                        "rubygems_version": entry["version"],
                        "selenium_wait_for": "section",
                    },
                )
            )
        return requests

    def parse_dependencies(self, response: Response) -> Any:
        draft = self._drafts.get(response.meta.get("rubygems_key"))
        if draft is None:
            return None

        version = response.meta["rubygems_version"]
        entry = next(
            (e for e in draft["versions"] if e["version"] == version), None
        )
        if entry is not None:
            entry["dependencies"] = parse_dependencies_page(response)

        draft["pending"] -= 1
        if draft["pending"] <= 0:
            return self._finish(draft)
        return None

    def package_errback(self, failure: Any) -> Any:
        super().errback(failure)
        draft = self._drafts.pop(failure.request.meta.get("rubygems_key"), None)
        if draft is not None:
            self.record_error(
                draft["metadata"].get("homepage_url") or "",
                "versions",
                "DownloadFailed",
                f"version list unavailable for {draft['name']}",
            )
        return None

    def version_errback(self, failure: Any) -> Any:
        super().errback(failure)
        draft = self._drafts.get(failure.request.meta.get("rubygems_key"))
        if draft is None:
            return None

        version = failure.request.meta.get("rubygems_version")
        draft["versions"] = [e for e in draft["versions"] if e["version"] != version]
        draft["pending"] -= 1
        if draft["pending"] <= 0:
            return self._finish(draft)
        return None

    def _finish(self, draft: dict[str, Any]) -> Any:
        self._drafts.pop(draft["name"], None)
        if not draft["versions"]:
            return None

        metadata = draft["metadata"]
        item = self.load_package(
            {
                "ecosystem": self.ecosystem,
                "name": draft["name"],
                "sampling_category": draft["category"],
                "sampling_rank": draft["rank"],
                "sampling_seed": draft["seed"],
                "description": metadata.get("description"),
                "homepage_url": metadata.get("homepage_url"),
                "repository_url": metadata.get("repository_url"),
                "license": metadata.get("license"),
                "latest_version": metadata.get("latest_version"),
                "first_published_at": metadata.get("first_published_at"),
                "latest_published_at": metadata.get("latest_published_at"),
                "downloads": metadata.get("downloads"),
                "downloads_period": "total",
                "source": self.source,
                "source_url": gem_url(draft["name"]),
                "collection_method": "selenium",
                "collected_at": datetime.now(timezone.utc),
            }
        )
        item["versions"] = sorted(
            draft["versions"],
            key=lambda entry: entry.get("published_at")
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return item
