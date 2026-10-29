"""npm collector backed by the public www.npmjs.com pages.

Only the website is used: search pages for discovery, package pages for
metadata, the versions tab for release history, and per-version dependency
tabs. npm's registry API is deliberately not called.
"""

from __future__ import annotations

import logging
import math
import random
import re
import string
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote, urlparse

from scrapy import Request
from scrapy.http import Response

from warehouse.processing import parse_timestamp
from warehouse.sampling import select_versions
from warehouse.spiders.base import BrowserSpider

logger = logging.getLogger(__name__)

BASE_URL = "https://www.npmjs.com"
SEARCH_RESULTS_PER_PAGE = 20
DEPENDENCY_TYPES = {
    "dependencies": ("runtime", False),
    "dev dependencies": ("dev", False),
    "peer dependencies": ("peer", False),
    "optional dependencies": ("optional", True),
    "bundled dependencies": ("bundled", False),
}


@dataclass(frozen=True)
class Candidate:
    name: str
    category: str
    query_index: int
    page: int
    position: int


@dataclass
class PackageDraft:
    name: str
    category: str
    rank: int
    seed: int | None
    source_url: str
    metadata: dict[str, Any]
    versions: list[dict[str, Any]] = field(default_factory=list)
    pending_versions: int = 0
    total_versions: int | None = None


def package_url(name: str) -> str:
    return f"{BASE_URL}/package/{quote(name, safe='@/')}"


def search_url(query: str, page: int = 0, sort_by: str | None = "downloads_monthly") -> str:
    params = [f"q={quote(query)}", f"page={page}"]
    if sort_by:
        params.append(f"sortBy={quote(sort_by)}")
    return f"{BASE_URL}/search?{'&'.join(params)}"


def versions_url(name: str) -> str:
    return f"{package_url(name)}?activeTab=versions"


def dependencies_url(name: str, version: str) -> str:
    return f"{package_url(name)}/v/{quote(version, safe='')}?activeTab=dependencies"


def _text(response: Response, xpath: str) -> str | None:
    value = response.xpath(f"string({xpath})").get()
    return value.strip() if value and value.strip() else None


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def parse_search_results(response: Response) -> list[str]:
    names: list[str] = []
    for href in response.xpath("//a[starts-with(@href, '/package/')]/@href").getall():
        path = urlparse(href).path
        if "/v/" in path:
            continue
        name = unquote(path[len("/package/") :]).strip("/")
        if name and name not in names:
            names.append(name)
    return names


def parse_package_page(response: Response) -> dict[str, Any]:
    description = response.xpath("//meta[@property='og:description']/@content").get()
    if not description:
        description = response.xpath("//meta[@name='description']/@content").get()
    if description:
        description = description.split(" Latest version:")[0].strip()

    latest_version = _text(
        response, "//h3[normalize-space()='Version']/following-sibling::*[1]//p[1]"
    )
    latest_published_at = parse_timestamp(
        response.xpath(
            "//h3[normalize-space()='Last publish']/following-sibling::p//time/@datetime"
        ).get()
    )
    downloads = _parse_int(
        _text(
            response,
            "//h3[contains(., 'Weekly Downloads')]/parent::div//p[1]",
        )
    )
    license_name = _text(
        response, "//h3[normalize-space()='License']/following-sibling::p[1]"
    )
    repository_url = response.xpath(
        "//h3[@id='repository']/following-sibling::p//a/@href"
    ).get()
    homepage_url = response.xpath(
        "//h3[@id='homePage']/following-sibling::p//a/@href"
    ).get()

    return {
        "name": response.xpath("//meta[@property='og:title']/@content").get(),
        "description": description or None,
        "latest_version": latest_version,
        "latest_published_at": latest_published_at,
        "downloads": downloads,
        "license": license_name,
        "repository_url": repository_url,
        "homepage_url": homepage_url,
    }


def parse_versions_page(response: Response) -> tuple[list[dict[str, Any]], int | None]:
    table = response.xpath("//table[.//th[contains(., 'Published')]]")[0:1]
    if not table:
        return [], None

    entries: list[dict[str, Any]] = []
    for row in table[0].xpath(".//tbody/tr"):
        version = row.xpath("./td[1]/a/text()").get()
        if not version:
            continue
        version = version.strip()
        if any(entry["version"] == version for entry in entries):
            continue
        entries.append(
            {
                "version": version,
                "published_at": parse_timestamp(row.xpath(".//time/@datetime").get()),
                "downloads": _parse_int(row.xpath("string(./td[2])").get()),
            }
        )

    total = None
    for label in response.xpath(
        "//a[contains(@href, 'activeTab=versions')]"
    ).xpath("string(.)").getall():
        match = re.search(r"([\d,]+)\s+Versions", label or "")
        if match:
            total = _parse_int(match.group(1))
            break
    return entries, total


def parse_dependencies_page(response: Response) -> dict[str, Any] | None:
    section = response.xpath("//section[@id='tabpanel-dependencies']")
    if not section:
        return None

    dependencies: list[dict[str, Any]] = []
    heading = "Dependencies"
    for node in section.xpath("./*"):
        if not isinstance(node.root.tag, str):
            continue
        if node.root.tag == "h2":
            heading = node.xpath("string(.)").get() or heading
            continue
        if node.root.tag != "ul":
            continue

        dependency_type, optional = DEPENDENCY_TYPES.get(
            re.sub(r"\s*\(\d+\)$", "", heading.strip().lower()), ("runtime", False)
        )
        for href in node.xpath("./li/a/@href").getall():
            path = urlparse(href).path
            name = unquote(path[len("/package/") :]).strip("/")
            if not name:
                continue
            dependencies.append(
                {
                    "name": name,
                    "constraint": None,
                    "dependency_type": dependency_type,
                    "optional": optional,
                }
            )

    license_name = _text(
        response, "//h3[normalize-space()='License']/following-sibling::p[1]"
    )
    return {"dependencies": dependencies, "license": license_name}


def select_sample(
    candidates: Iterable[Candidate],
    popular_count: int,
    random_count: int,
    seed: int,
) -> list[Candidate]:
    """Choose disjoint popular and random groups.

    Popular candidates come from queries already sorted by monthly downloads.
    They are interleaved by result page so a single query cannot fill the
    whole group, and their order is the sampling rank.
    """
    popular_pool = sorted(
        (c for c in candidates if c.category == "popular"),
        key=lambda c: (c.page, c.query_index, c.position),
    )
    chosen: list[Candidate] = []
    taken: set[str] = set()
    for candidate in popular_pool:
        if len(chosen) >= popular_count:
            break
        if candidate.name in taken:
            continue
        taken.add(candidate.name)
        chosen.append(candidate)

    random_pool = [
        c for c in candidates if c.category == "random" and c.name not in taken
    ]
    rng = random.Random(seed)
    by_query: dict[int, list[Candidate]] = {}
    for candidate in random_pool:
        by_query.setdefault(candidate.query_index, []).append(candidate)
    for group in by_query.values():
        rng.shuffle(group)

    # Round-robin across random search queries so one lucky query cannot
    # dominate the comparison group.
    picked: list[Candidate] = []
    while len(picked) < random_count and any(by_query.values()):
        for query_index in sorted(by_query):
            if not by_query[query_index]:
                continue
            picked.append(by_query[query_index].pop())
            if len(picked) >= random_count:
                break
    chosen.extend(picked)
    return chosen


class NpmSpider(BrowserSpider):
    name = "npm"
    ecosystem = "npm"
    source = "npmjs.com"
    selenium_meta = {
        "selenium": True,
        "selenium_wait_for": "h1",
        "selenium_wait": 0.2,
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._candidates: dict[str, Candidate] = {}
        self._pending_searches = 0
        self._discovery_done = False
        self._drafts: dict[str, PackageDraft] = {}

    async def start(self) -> AsyncIterator[Request]:
        popular_count = self.settings.getint("NPM_POPULAR_COUNT", 25)
        random_count = self.settings.getint("NPM_RANDOM_COUNT", 5)
        queries = self.settings.getlist("NPM_SEARCH_QUERIES") or ["framework"]

        popular_pages = 1
        if popular_count > 0:
            per_page = max(1, len(queries) * SEARCH_RESULTS_PER_PAGE)
            # One extra page per query buffers against duplicates across queries.
            popular_pages = math.ceil(popular_count / per_page) + 1

        requests: list[Request] = []
        for query_index, query in enumerate(queries if popular_count > 0 else []):
            for page in range(popular_pages):
                requests.append(
                    self.browser_request(
                        search_url(query, page),
                        callback=self.parse_search,
                        errback=self.search_errback,
                        meta={
                            "npm_category": "popular",
                            "npm_query_index": query_index,
                            "npm_page": page,
                            "selenium_wait_for": "a[href^='/package/']",
                        },
                    )
                )

        seed = self.settings.getint("NPM_SAMPLE_SEED", 1)
        for query_index, query in enumerate(self._random_queries(random_count, seed)):
            requests.append(
                self.browser_request(
                    search_url(query, 0, sort_by=None),
                    callback=self.parse_search,
                    errback=self.search_errback,
                    meta={
                        "npm_category": "random",
                        "npm_query_index": query_index,
                        "npm_page": 0,
                        "selenium_wait_for": "a[href^='/package/']",
                    },
                )
            )

        self._pending_searches = len(requests)
        if not requests:
            return
        for request in requests:
            yield request

    def _random_queries(self, count: int, seed: int) -> list[str]:
        if count <= 0:
            return []
        rng = random.Random(seed)
        queries: list[str] = []
        seen: set[str] = set()
        while len(queries) < count:
            query = "".join(rng.choice(string.ascii_lowercase) for _ in range(3))
            if query in seen:
                continue
            seen.add(query)
            queries.append(query)
        return queries

    def parse_search(self, response: Response) -> Any:
        category = response.meta["npm_category"]
        query_index = response.meta["npm_query_index"]
        page = response.meta["npm_page"]

        for position, name in enumerate(parse_search_results(response)):
            self._candidates.setdefault(
                name,
                Candidate(
                    name=name,
                    category=category,
                    query_index=query_index,
                    page=page,
                    position=position,
                ),
            )

        self._pending_searches -= 1
        if self._pending_searches <= 0:
            return self._schedule_packages()
        return None

    def search_errback(self, failure: Any) -> Any:
        super().errback(failure)
        self._pending_searches -= 1
        if self._pending_searches <= 0:
            return self._schedule_packages()
        return None

    def _schedule_packages(self) -> Any:
        if self._discovery_done:
            return None
        self._discovery_done = True

        popular_count = self.settings.getint("NPM_POPULAR_COUNT", 25)
        random_count = self.settings.getint("NPM_RANDOM_COUNT", 5)
        seed = self.settings.getint("NPM_SAMPLE_SEED", 1)
        sample = select_sample(
            self._candidates.values(), popular_count, random_count, seed
        )

        popular_rank = 0
        random_rank = 0
        crawl = self.crawler
        for candidate in sample:
            if candidate.category == "popular":
                rank = popular_rank
                popular_rank += 1
            else:
                rank = random_rank
                random_rank += 1

            request = self.browser_request(
                package_url(candidate.name),
                callback=self.parse_package,
                meta={
                    "npm_name": candidate.name,
                    "npm_category": candidate.category,
                    "npm_rank": rank,
                    "npm_seed": seed if candidate.category == "random" else None,
                },
            )
            crawl.engine.crawl(request)

        self.logger.info(
            "Sampled %d npm packages (%d popular, %d random) from %d candidates",
            len(sample),
            popular_rank,
            random_rank,
            len(self._candidates),
        )
        return None

    def parse_package(self, response: Response) -> Any:
        name = response.meta["npm_name"]
        key = name.lower()
        metadata = parse_package_page(response)
        draft = PackageDraft(
            name=name,
            category=response.meta["npm_category"],
            rank=response.meta["npm_rank"],
            seed=response.meta.get("npm_seed"),
            source_url=response.url,
            metadata=metadata,
        )
        self._drafts[key] = draft

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
            meta={
                "npm_key": key,
                "selenium_wait_for": "table",
            },
        )

    def parse_versions(self, response: Response) -> Any:
        draft = self._drafts.get(response.meta["npm_key"])
        if draft is None:
            return None

        entries, total = parse_versions_page(response)
        draft.total_versions = total
        if not entries:
            self.record_error(
                response.url,
                "parse",
                "MissingField",
                f"version table not rendered for {draft.name}",
            )
            return self._finish(draft)

        limit = self.settings.getint("NPM_MAX_VERSIONS", 10)
        strategy = self.settings.get("NPM_VERSION_SELECTION", "spread")
        selected = select_versions(entries, limit, strategy)

        latest = draft.metadata.get("latest_version")
        if latest and all(entry["version"] != latest for entry in selected):
            latest_entry = next(
                (entry for entry in entries if entry["version"] == latest), None
            )
            if latest_entry:
                selected.insert(0, latest_entry)

        if total is not None and len(entries) >= total:
            dates = [
                entry["published_at"]
                for entry in entries
                if entry.get("published_at") is not None
            ]
            if dates:
                draft.metadata["first_published_at"] = min(dates)

        draft.versions = selected
        if not selected:
            return self._finish(draft)

        requests = []
        for entry in selected:
            draft.pending_versions += 1
            requests.append(
                self.browser_request(
                    dependencies_url(draft.name, entry["version"]),
                    callback=self.parse_version_dependencies,
                    errback=self.version_errback,
                    meta={
                        "npm_key": response.meta["npm_key"],
                        "npm_version": entry["version"],
                        "selenium_wait_for": "section#tabpanel-dependencies",
                    },
                )
            )
        return requests

    def parse_version_dependencies(self, response: Response) -> Any:
        draft = self._drafts.get(response.meta.get("npm_key"))
        if draft is None:
            return None

        version = response.meta["npm_version"]
        entry = next(
            (e for e in draft.versions if e["version"] == version), None
        )
        if entry is None:
            return None

        parsed = parse_dependencies_page(response)
        if parsed is None:
            self.record_error(
                response.url,
                "parse",
                "MissingField",
                f"dependency section not rendered for {draft.name}@{version}",
            )
            draft.versions.remove(entry)
        else:
            entry["dependencies"] = parsed["dependencies"]
            if parsed.get("license"):
                entry["license"] = parsed["license"]

        draft.pending_versions -= 1
        if draft.pending_versions <= 0:
            return self._finish(draft)
        return None

    def package_errback(self, failure: Any) -> Any:
        super().errback(failure)
        key = failure.request.meta.get("npm_key")
        draft = self._drafts.pop(key, None)
        if draft is not None:
            self.record_error(
                draft.source_url,
                "versions",
                "DownloadFailed",
                f"version list unavailable for {draft.name}",
            )
        return None

    def version_errback(self, failure: Any) -> Any:
        super().errback(failure)
        draft = self._drafts.get(failure.request.meta.get("npm_key"))
        if draft is None:
            return None

        version = failure.request.meta.get("npm_version")
        draft.versions = [e for e in draft.versions if e["version"] != version]
        draft.pending_versions -= 1
        if draft.pending_versions <= 0:
            return self._finish(draft)
        return None

    def _finish(self, draft: PackageDraft) -> Any:
        self._drafts.pop(draft.name.lower(), None)
        if not draft.versions:
            self.record_error(
                draft.source_url,
                "parse",
                "NoVersionsStored",
                f"no version dependency data collected for {draft.name}",
            )
            return None

        metadata = draft.metadata
        versions = sorted(
            draft.versions,
            key=lambda entry: entry.get("published_at") or datetime.min.replace(
                tzinfo=timezone.utc
            ),
            reverse=True,
        )
        item = self.load_package(
            {
                "ecosystem": self.ecosystem,
                "name": draft.name,
                "sampling_category": draft.category,
                "sampling_rank": draft.rank,
                "sampling_seed": draft.seed,
                "description": metadata.get("description"),
                "homepage_url": metadata.get("homepage_url"),
                "repository_url": metadata.get("repository_url"),
                "license": metadata.get("license"),
                "latest_version": metadata.get("latest_version"),
                "first_published_at": metadata.get("first_published_at"),
                "latest_published_at": metadata.get("latest_published_at"),
                "downloads": metadata.get("downloads"),
                "downloads_period": "last-week",
                "source": self.source,
                "source_url": draft.source_url,
                "collection_method": "selenium",
                "collected_at": datetime.now(timezone.utc),
            }
        )
        item["versions"] = versions
        return item
