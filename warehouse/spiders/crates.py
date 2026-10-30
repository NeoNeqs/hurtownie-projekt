"""crates.io collector backed by the rendered gallery pages.

crates.io is a client-rendered Svelte application, so every page here goes
through the shared Selenium layer. The crate list provides the download ranking
and the alphabetical comparison pool, the crate page supplies metadata, the
versions page supplies release dates and licenses, and each selected version's
dependencies page supplies dependency kinds and constraints.

The version list loads 100 releases at a time. The spider clicks "Load More"
the configured number of times so sampling can reach older releases.
"""

from __future__ import annotations

import math
import random
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote, urlparse

from selenium.webdriver.common.by import By
from scrapy import Request
from scrapy.http import Response

from warehouse.sampling import select_random_names, select_versions
from warehouse.spiders.base import BrowserSpider

BASE_URL = "https://crates.io"
PACKAGES_PER_PAGE = 50
DEPENDENCY_TYPES = {
    "dependencies": "runtime",
    "dev-dependencies": "dev",
    "build-dependencies": "build",
}


def crate_url(name: str) -> str:
    return f"{BASE_URL}/crates/{quote(name, safe='')}"


def versions_url(name: str) -> str:
    return f"{crate_url(name)}/versions"


def dependencies_url(name: str, version: str) -> str:
    return f"{crate_url(name)}/{quote(version, safe='')}/dependencies"


def list_url(sort: str, page: int) -> str:
    return f"{BASE_URL}/crates?sort={sort}&page={page}"


def crate_name_from_url(url: str) -> str | None:
    path = urlparse(url).path
    if not path.startswith("/crates/"):
        return None
    remainder = unquote(path[len("/crates/") :]).strip("/")
    name = remainder.split("/")[0]
    return name or None


def _text(response: Response, xpath: str) -> str | None:
    value = response.xpath(f"string({xpath})").get()
    return value.strip() if value and value.strip() else None


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_crate_listing(response: Response) -> list[dict[str, Any]]:
    crates: list[dict[str, Any]] = []
    for row in response.xpath("//div[contains(@class, 'crate-row')]"):
        href = row.xpath("string(.//a[contains(@class, 'name')]/@href)").get()
        name = crate_name_from_url(href or "")
        if not name:
            continue
        downloads = _parse_int(
            row.xpath(
                "string(.//li[contains(@class, 'downloads') "
                "and not(contains(@class, 'recent-downloads'))])"
            ).get()
        )
        crates.append(
            {
                "name": name,
                "downloads": downloads,
                "latest_version": _text(
                    row, ".//span[contains(@class, 'version')]"
                ),
                "description": _text(
                    row, ".//div[contains(@class, 'description')]"
                ),
            }
        )
    return crates


def parse_package_page(response: Response) -> dict[str, Any]:
    license_text = _text(response, "//div[contains(@class, 'license')]")
    if license_text:
        license_text = re.sub(
            r"^License:\s*", "", license_text, flags=re.IGNORECASE
        )

    latest_version = _text(
        response, "//h1[contains(@class, 'heading')]/small[1]"
    )
    if latest_version:
        latest_version = re.sub(r"^v", "", latest_version)

    return {
        "name": _text(response, "//h1[contains(@class, 'heading')]/span[1]")
        or crate_name_from_url(response.url),
        "description": response.xpath(
            "//meta[@name='description']/@content"
        ).get(),
        "latest_version": latest_version,
        "license": license_text or None,
        "latest_published_at": parse_datetime(
            response.xpath(
                "//div[contains(@class, 'metadata')]//time/@datetime"
            ).get()
        ),
        "homepage_url": response.xpath(
            "//h2[normalize-space()='Homepage']"
            "/following-sibling::div[1]//a/@href"
        ).get(),
        "repository_url": response.xpath(
            "//h2[normalize-space()='Repository']"
            "/following-sibling::div[1]//a/@href"
        ).get(),
        "downloads": _parse_int(
            response.xpath(
                "string(//div[contains(concat(' ', normalize-space(@class), ' '), "
                "' stat ') and contains(., 'Downloads all time')]"
                "//span[contains(@class, 'num__align')])"
            ).get()
        ),
    }


def parse_versions_page(
    response: Response,
) -> tuple[list[dict[str, Any]], int | None]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for card in response.xpath(
        "//div[contains(@class, 'version')]"
        "[.//a[contains(@class, 'num-link')]]"
    ):
        href = card.xpath(
            "string(.//a[contains(@class, 'num-link')]/@href)"
        ).get() or ""
        version = unquote(href).rstrip("/").rsplit("/", 1)[-1]
        if not version or version in seen:
            continue
        seen.add(version)

        row = card.xpath("ancestor::div[contains(@class, 'row')][1]")
        license_text = _text(row, ".//span[contains(@class, 'license')]")
        if license_text:
            license_text = re.sub(
                r"^License:\s*", "", license_text, flags=re.IGNORECASE
            )
        entries.append(
            {
                "version": version,
                "published_at": parse_datetime(
                    row.xpath("string(.//time/@datetime)").get()
                ),
                "license": license_text or None,
            }
        )

    total = None
    counts = response.xpath(
        "//div[contains(@class, 'results-meta')]/span/strong/text()"
    ).getall()
    if len(counts) >= 2:
        total = _parse_int(counts[1])
    return entries, total


def parse_dependencies_page(response: Response) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    for heading in response.xpath("//h2[contains(@class, 'heading')]"):
        label = (heading.xpath("string(.)").get() or "").strip().lower()
        if "dependencies" not in label:
            continue
        dependency_type = DEPENDENCY_TYPES.get(label, label.replace("-", "_"))
        for row in heading.xpath("following-sibling::ul[1]/li"):
            href = row.xpath(
                "string(.//a[contains(@class, 'link') "
                "and starts-with(@href, '/crates/')]/@href)"
            ).get() or ""
            if "/crates/" not in href:
                continue
            name = unquote(href.split("/crates/", 1)[1]).split("/", 1)[0]
            if not name:
                continue
            constraint = _text(row, ".//*[contains(@class, 'range-lg')]")
            optional = bool(
                row.xpath(".//*[contains(@class, 'optional-label')]")
            ) or "optional" in (row.xpath("@class").get() or "")
            dependencies.append(
                {
                    "name": name,
                    "constraint": constraint,
                    "dependency_type": dependency_type,
                    "optional": optional,
                }
            )
    return dependencies


def load_more_versions(session: Any, request: Request) -> None:
    pages = int(request.meta.get("crates_version_pages", 1) or 1)
    for _ in range(max(0, pages - 1)):
        buttons = session.driver.find_elements(
            By.CSS_SELECTOR, "button.load-more-button"
        )
        if not buttons or not buttons[0].is_displayed():
            return
        before = len(
            session.driver.find_elements(
                By.CSS_SELECTOR, "div.version a.num-link"
            )
        )
        buttons[0].click()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            session.sleep(0.5)
            after = len(
                session.driver.find_elements(
                    By.CSS_SELECTOR, "div.version a.num-link"
                )
            )
            if after > before:
                break


class CratesSpider(BrowserSpider):
    name = "crates"
    ecosystem = "crates.io"
    source = "crates.io"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.popular: list[dict[str, Any]] = []
        self.random_pool: dict[str, dict[str, Any]] = {}
        self._pending_discovery = 0
        self._popular_ready = False
        self._random_ready = False
        self._scheduled = False
        self._drafts: dict[str, dict[str, Any]] = {}

    async def start(self) -> AsyncIterator[Request]:
        popular_count = self.settings.getint("CRATES_POPULAR_COUNT", 25)
        random_count = self.settings.getint("CRATES_RANDOM_COUNT", 5)
        seed = self.settings.getint("CRATES_SAMPLE_SEED", 1)

        requests: list[Request] = []
        if popular_count > 0:
            pages = max(1, math.ceil(popular_count / PACKAGES_PER_PAGE))
            self._pending_discovery += pages
            for page in range(1, pages + 1):
                requests.append(
                    self.browser_request(
                        list_url("downloads", page),
                        callback=self.parse_popular,
                        errback=self.discovery_errback,
                        meta={
                            "crates_discovery": "popular",
                            "selenium_wait_for": "div.crate-row",
                        },
                    )
                )
        else:
            self._popular_ready = True

        pages = max(0, self.settings.getint("CRATES_RANDOM_PAGES", 5))
        if random_count > 0 and pages > 0:
            page_limit = max(1, self.settings.getint("CRATES_PAGE_LIMIT", 200))
            chosen = random.Random(seed).sample(
                range(1, page_limit + 1), min(pages, page_limit)
            )
            self._pending_discovery += len(chosen)
            for page in chosen:
                requests.append(
                    self.browser_request(
                        list_url("alpha", page),
                        callback=self.parse_random,
                        errback=self.discovery_errback,
                        meta={
                            "crates_discovery": "random",
                            "selenium_wait_for": "div.crate-row",
                        },
                    )
                )
        else:
            self._random_ready = True

        if not requests:
            self._schedule_packages()
            return
        for request in requests:
            yield request

    def parse_popular(self, response: Response) -> Any:
        self.popular.extend(parse_crate_listing(response))
        self._popular_ready = True
        return self._discovery_finished()

    def parse_random(self, response: Response) -> Any:
        for entry in parse_crate_listing(response):
            self.random_pool.setdefault(entry["name"], entry)
        self._random_ready = True
        return self._discovery_finished()

    def discovery_errback(self, failure: Any) -> Any:
        super().errback(failure)
        if failure.request.meta.get("crates_discovery") == "popular":
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

        popular_count = self.settings.getint("CRATES_POPULAR_COUNT", 25)
        random_count = self.settings.getint("CRATES_RANDOM_COUNT", 5)
        seed = self.settings.getint("CRATES_SAMPLE_SEED", 1)

        popular = self.popular[:popular_count]
        popular_names = {entry["name"] for entry in popular}
        random_names = select_random_names(
            list(self.random_pool), random_count, seed, popular_names
        )
        downloads = {entry["name"]: entry["downloads"] for entry in popular}
        downloads.update(
            {name: self.random_pool[name]["downloads"] for name in random_names}
        )

        sample = [
            {
                "name": entry["name"],
                "category": "popular",
                "rank": rank,
                "seed": None,
            }
            for rank, entry in enumerate(popular)
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
                crate_url(entry["name"]),
                callback=self.parse_package,
                errback=self.package_errback,
                meta={
                    "crates_name": entry["name"],
                    "crates_category": entry["category"],
                    "crates_rank": entry["rank"],
                    "crates_seed": entry["seed"],
                    "crates_downloads": downloads.get(entry["name"]),
                    "selenium_wait_for": "h1.heading",
                },
            )
            crawl.engine.crawl(request)

        self.logger.info(
            "Sampled %d crates.io packages (%d popular, %d random) from %d candidates",
            len(sample),
            len(popular),
            len(random_names),
            len(self.random_pool),
        )
        return None

    def parse_package(self, response: Response) -> Any:
        name = response.meta["crates_name"]
        metadata = parse_package_page(response)
        if not metadata.get("name"):
            metadata["name"] = name

        draft = {
            "name": name,
            "category": response.meta["crates_category"],
            "rank": response.meta["crates_rank"],
            "seed": response.meta["crates_seed"],
            "downloads": response.meta.get("crates_downloads")
            or metadata.get("downloads"),
            "metadata": metadata,
            "versions": [],
            "pending": 0,
        }
        self._drafts[name] = draft

        return self.browser_request(
            versions_url(name),
            callback=self.parse_versions,
            errback=self.versions_errback,
            meta={
                "crates_name": name,
                "selenium_wait_for": "div.version",
                "selenium_actions": load_more_versions,
                "crates_version_pages": self.settings.getint(
                    "CRATES_VERSION_PAGES", 1
                ),
            },
        )

    def parse_versions(self, response: Response) -> Any:
        draft = self._drafts.get(response.meta["crates_name"])
        if draft is None:
            return None

        entries, total = parse_versions_page(response)
        metadata = draft["metadata"]
        if not entries:
            self.record_error(
                response.url,
                "parse",
                "NoVersionsStored",
                f"version history not rendered for {draft['name']}",
            )
            latest = metadata.get("latest_version")
            if latest:
                entries = [
                    {
                        "version": latest,
                        "published_at": metadata.get("latest_published_at"),
                        "license": metadata.get("license"),
                    }
                ]

        if not entries:
            return self._finish(draft)

        dates = [
            entry["published_at"]
            for entry in entries
            if entry.get("published_at") is not None
        ]
        # A truncated version list makes the oldest loaded release look like the
        # first release. Report the date only when the full list was loaded.
        if dates and (total is None or len(entries) >= total):
            metadata["first_published_at"] = min(dates)

        latest = metadata.get("latest_version")
        latest_entry = next(
            (entry for entry in entries if entry["version"] == latest), None
        )
        if latest_entry:
            metadata["latest_published_at"] = latest_entry["published_at"]
            metadata["license"] = metadata.get("license") or latest_entry["license"]

        limit = self.settings.getint("CRATES_MAX_VERSIONS", 10)
        strategy = self.settings.get("CRATES_VERSION_SELECTION", "spread")
        selected = select_versions(entries, limit, strategy)
        if latest and all(entry["version"] != latest for entry in selected):
            if latest_entry:
                selected.insert(0, latest_entry)

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
                        "crates_name": draft["name"],
                        "crates_version": entry["version"],
                        "selenium_wait_for": "h2.heading",
                    },
                )
            )
        return requests

    def parse_dependencies(self, response: Response) -> Any:
        draft = self._drafts.get(response.meta.get("crates_name"))
        if draft is None:
            return None

        version = response.meta["crates_version"]
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
        draft = self._drafts.pop(failure.request.meta.get("crates_name"), None)
        if draft is not None:
            self.record_error(
                crate_url(draft["name"]),
                "package",
                "DownloadFailed",
                f"crate page unavailable for {draft['name']}",
            )
        return None

    def versions_errback(self, failure: Any) -> Any:
        super().errback(failure)
        draft = self._drafts.get(failure.request.meta.get("crates_name"))
        if draft is None:
            return None
        self.record_error(
            failure.request.url,
            "versions",
            "VersionListFailed",
            f"version list unavailable for {draft['name']}",
        )
        latest = draft["metadata"].get("latest_version")
        if latest:
            draft["versions"] = [
                {
                    "version": latest,
                    "published_at": draft["metadata"].get("latest_published_at"),
                    "license": draft["metadata"].get("license"),
                }
            ]
        return self._finish(draft)

    def version_errback(self, failure: Any) -> Any:
        super().errback(failure)
        draft = self._drafts.get(failure.request.meta.get("crates_name"))
        if draft is None:
            return None

        version = failure.request.meta.get("crates_version")
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
                "downloads": draft["downloads"],
                "downloads_period": "total",
                "source": self.source,
                "source_url": crate_url(draft["name"]),
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
