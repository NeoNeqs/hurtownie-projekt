"""Hex collector backed by hex.pm pages.

The package index supplies both discovery groups (sorted by total downloads for
the popular group, alphabetical pages for the comparison group) and the
download counts. The package page supplies metadata, the versions page the
release history, and each selected version's dependencies page its dependency
requirements.

Hex does not render a license on the package page, so license stays empty for
this ecosystem rather than being inferred from the package description.
"""

from __future__ import annotations

import random
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

from scrapy import Request
from scrapy.http import Response

from warehouse.sampling import select_random_names, select_versions
from warehouse.spiders.base import BrowserSpider

BASE_URL = "https://hex.pm"
PACKAGES_PER_PAGE = 30
REPOSITORY_LABELS = {"github", "source", "repository", "gitlab", "bitbucket"}
HOMEPAGE_LABELS = {"homepage", "home page", "website"}


def package_url(name: str) -> str:
    return f"{BASE_URL}/packages/{name}"


def versions_url(name: str) -> str:
    return f"{BASE_URL}/packages/{name}/versions"


def dependencies_url(name: str, version: str) -> str:
    return f"{BASE_URL}/packages/{name}/{version}/dependencies"


def index_url(sort: str, page: int) -> str:
    return f"{BASE_URL}/packages?sort={sort}&page={page}"


def package_name_from_url(url: str) -> str | None:
    path = urlparse(url).path
    if not path.startswith("/packages/"):
        return None
    name = unquote(path[len("/packages/") :]).strip("/")
    return name.split("/")[0] or None


def _text(response: Response, xpath: str) -> str | None:
    value = response.xpath(f"string({xpath})").get()
    return value.strip() if value and value.strip() else None


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def parse_package_listing(response: Response) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    for item in response.xpath("//li[contains(@class, 'group')]"):
        href = item.xpath("string(.//a[starts-with(@href, '/packages/')]/@href)").get()
        name = unquote((href or "").split("?")[0].strip("/"))
        if name.startswith("packages/"):
            name = name[len("packages/") :]
        if not name or "/" in name:
            continue
        downloads = _parse_int(
            item.xpath(
                "string(.//div[contains(@class, 'text-[22px]')])"
            ).get()
        )
        recent_title = item.xpath(
            "string(.//div[starts-with(@title, 'recent downloads')]/@title)"
        ).get()
        recent = None
        if recent_title and ":" in recent_title:
            recent = _parse_int(recent_title.split(":", 1)[1])
        packages.append({"name": name, "downloads": downloads, "recent": recent})
    return packages


def parse_package_page(response: Response) -> dict[str, Any]:
    links: dict[str, str] = {}
    for anchor in response.xpath(
        "//h3[normalize-space()='Links']/following-sibling::ul[1]//a[@href]"
    ):
        href = anchor.xpath("@href").get()
        label = " ".join(anchor.xpath("string(.)").get().split()).lower()
        if href and label:
            links.setdefault(label, href)

    return {
        "name": response.xpath("//meta[@property='og:title']/@content").get()
        or package_name_from_url(response.url),
        "description": response.xpath("//meta[@name='description']/@content").get(),
        "latest_version": _text(
            response,
            "//details[contains(@class, 'package-version-picker')]"
            "//summary//span[contains(@class, 'font-mono')][1]",
        ),
        "repository_url": next(
            (href for label, href in links.items() if label in REPOSITORY_LABELS),
            None,
        ),
        "homepage_url": next(
            (href for label, href in links.items() if label in HOMEPAGE_LABELS),
            None,
        ),
    }


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%b %d, %Y, %H:%M").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def parse_versions_page(
    response: Response,
) -> tuple[list[dict[str, Any]], int | None]:
    name = package_name_from_url(response.url)
    prefix = f"/packages/{name}/"
    entries: dict[str, dict[str, Any]] = {}

    # Restrict to release rows: the version picker links to every version too,
    # but only a release row pairs the link with its own dated paragraph.
    rows = response.xpath(
        "//p[@title]/parent::div[.//a[starts-with(@href, $prefix)]]",
        prefix=prefix,
    )
    for row in rows:
        href = row.xpath(
            "string(.//a[starts-with(@href, $prefix)]/@href)", prefix=prefix
        ).get() or ""
        remainder = href[len(prefix) :].strip("/")
        if not remainder or "/" in remainder:
            continue
        published = parse_date(row.xpath("string(p[1]/@title)").get())
        entry = entries.setdefault(
            remainder,
            {"version": remainder, "published_at": None},
        )
        if published and (
            entry["published_at"] is None or published < entry["published_at"]
        ):
            entry["published_at"] = published

    total = None
    for label in response.xpath("//*[contains(text(), 'Versions')]/text()").getall():
        match = re.search(r"([\d, ]+)\s+Versions", label or "")
        if match:
            total = _parse_int(match.group(1))
            break
    return list(entries.values()), total


def parse_dependencies_page(response: Response) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    table = response.xpath("//table[.//th[normalize-space()='Package']]")[0:1]
    if not table:
        return dependencies

    for row in table[0].xpath(".//tbody/tr"):
        name = row.xpath("string(td[1]//a/@title)").get() or _text(
            row, "td[1]"
        )
        if not name:
            continue
        requirement = row.xpath(
            "string(td[2]//span[contains(@class, 'font-mono')][1])"
        ).get()
        optional = "optional" in " ".join(
            row.xpath("string(td[2])").get().split()
        ).lower()
        dependencies.append(
            {
                "name": name.strip(),
                "constraint": requirement.strip() if requirement else None,
                "dependency_type": "runtime",
                "optional": optional,
            }
        )
    return dependencies


class HexSpider(BrowserSpider):
    name = "hex"
    ecosystem = "Hex"
    source = "hex.pm"
    selenium_meta = {
        "selenium": True,
        "selenium_wait_for": "h1",
        "selenium_wait": 0,
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.popular: list[dict[str, Any]] = []
        self.random_pool: list[dict[str, Any]] = []
        self._pending_discovery = 0
        self._popular_ready = False
        self._random_ready = False
        self._scheduled = False
        self._drafts: dict[str, dict[str, Any]] = {}

    async def start(self) -> AsyncIterator[Request]:
        popular_count = self.settings.getint("HEX_POPULAR_COUNT", 25)
        random_count = self.settings.getint("HEX_RANDOM_COUNT", 5)
        seed = self.settings.getint("HEX_SAMPLE_SEED", 1)

        requests: list[Request] = []
        if popular_count > 0:
            pages = max(1, -(-popular_count // PACKAGES_PER_PAGE))
            self._pending_discovery += pages
            for page in range(1, pages + 1):
                requests.append(
                    Request(
                        index_url("total_downloads", page),
                        callback=self.parse_popular,
                        errback=self.discovery_errback,
                        meta={"hex_discovery": "popular", "selenium": False},
                    )
                )
        else:
            self._popular_ready = True

        pages = max(0, self.settings.getint("HEX_RANDOM_PAGES", 3))
        if random_count > 0 and pages > 0:
            page_limit = max(1, self.settings.getint("HEX_PAGE_LIMIT", 767))
            chosen = random.Random(seed).sample(range(1, page_limit + 1), pages)
            self._pending_discovery += len(chosen)
            for page in chosen:
                requests.append(
                    Request(
                        index_url("name", page),
                        callback=self.parse_random,
                        errback=self.discovery_errback,
                        meta={"hex_discovery": "random", "selenium": False},
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
        self.popular.extend(parse_package_listing(response))
        self._popular_ready = True
        return self._discovery_finished()

    def parse_random(self, response: Response) -> Any:
        self.random_pool.extend(parse_package_listing(response))
        self._random_ready = True
        return self._discovery_finished()

    def discovery_errback(self, failure: Any) -> Any:
        super().errback(failure)
        if failure.request.meta.get("hex_discovery") == "popular":
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

        popular_count = self.settings.getint("HEX_POPULAR_COUNT", 25)
        random_count = self.settings.getint("HEX_RANDOM_COUNT", 5)
        seed = self.settings.getint("HEX_SAMPLE_SEED", 1)

        popular = self.popular[:popular_count]
        popular_names = {entry["name"] for entry in popular}
        pool = {entry["name"]: entry for entry in self.random_pool}
        random_names = select_random_names(
            list(pool), random_count, seed, popular_names
        )

        sample = [
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
                "downloads": pool[name]["downloads"],
            }
            for rank, name in enumerate(random_names)
        )

        crawl = self.crawler
        for entry in sample:
            request = self.browser_request(
                package_url(entry["name"]),
                callback=self.parse_package,
                meta={
                    "hex_name": entry["name"],
                    "hex_category": entry["category"],
                    "hex_rank": entry["rank"],
                    "hex_seed": entry["seed"],
                    "hex_downloads": entry["downloads"],
                },
            )
            crawl.engine.crawl(request)

        self.logger.info(
            "Sampled %d Hex packages (%d popular, %d random) from %d candidates",
            len(sample),
            len(popular),
            len(random_names),
            len(self.random_pool),
        )
        return None

    def parse_package(self, response: Response) -> Any:
        name = response.meta["hex_name"]
        metadata = parse_package_page(response)
        draft = {
            "name": name,
            "category": response.meta["hex_category"],
            "rank": response.meta["hex_rank"],
            "seed": response.meta["hex_seed"],
            "downloads": response.meta.get("hex_downloads"),
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
            meta={"hex_key": name, "selenium_wait_for": "h1"},
        )

    def parse_versions(self, response: Response) -> Any:
        draft = self._drafts.get(response.meta["hex_key"])
        if draft is None:
            return None

        entries, _total = parse_versions_page(response)
        if not entries:
            self.record_error(
                response.url,
                "parse",
                "NoVersionsStored",
                f"version history not rendered for {draft['name']}",
            )
            return self._finish(draft)

        limit = self.settings.getint("HEX_MAX_VERSIONS", 10)
        strategy = self.settings.get("HEX_VERSION_SELECTION", "spread")
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
        metadata = draft["metadata"]
        metadata["first_published_at"] = min(dates) if dates else None
        latest_entry = next(
            (entry for entry in entries if entry["version"] == latest), None
        )
        metadata["latest_published_at"] = (
            latest_entry["published_at"] if latest_entry else None
        )
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
                        "hex_key": draft["name"],
                        "hex_version": entry["version"],
                        "selenium_wait_for": "table",
                    },
                )
            )
        return requests

    def parse_dependencies(self, response: Response) -> Any:
        draft = self._drafts.get(response.meta.get("hex_key"))
        if draft is None:
            return None

        version = response.meta["hex_version"]
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
        draft = self._drafts.pop(failure.request.meta.get("hex_key"), None)
        if draft is not None:
            self.record_error(
                package_url(draft["name"]),
                "versions",
                "DownloadFailed",
                f"version list unavailable for {draft['name']}",
            )
        return None

    def version_errback(self, failure: Any) -> Any:
        super().errback(failure)
        draft = self._drafts.get(failure.request.meta.get("hex_key"))
        if draft is None:
            return None

        version = failure.request.meta.get("hex_version")
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
                "source_url": package_url(draft["name"]),
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
