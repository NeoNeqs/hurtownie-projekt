"""npm collector backed by the official registry API.

``npm.py`` remains the website collector and the primary demonstration of the
site-scraping path. This spider exists for scale runs where www.npmjs.com serves
a full-page Cloudflare managed challenge: registry.npmjs.org is an official,
robots-permitted interface, explicitly allowed by the project brief as a
supplement for discovery and metadata.

It uses the search endpoint for discovery and one packument request per package
for metadata, release history and per-version dependency declarations. Every
item records ``collection_method="registry_api"`` so the two sources are never
confused.
"""

from __future__ import annotations

import json
import logging
import random
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from scrapy import Request
from scrapy.http import Response

from warehouse.processing import parse_timestamp
from warehouse.sampling import select_random_names, select_versions
from warehouse.spiders.base import BrowserSpider

logger = logging.getLogger(__name__)

REGISTRY_URL = "https://registry.npmjs.org"
SEARCH_URL = f"{REGISTRY_URL}/-/v1/search"
SEARCH_PAGE_SIZE = 250
DEPENDENCY_TYPES = {
    "dependencies": ("runtime", False),
    "devDependencies": ("dev", False),
    "peerDependencies": ("peer", False),
    "optionalDependencies": ("optional", True),
}


@dataclass(frozen=True)
class RegistryCandidate:
    name: str
    category: str
    query_index: int
    position: int
    downloads: int | None


def search_url(query: str, size: int) -> str:
    return f"{SEARCH_URL}?text={quote(query)}&size={size}"


def packument_url(name: str) -> str:
    return f"{REGISTRY_URL}/{quote(name, safe='@')}"


def parse_search_payload(
    payload: dict[str, Any], category: str, query_index: int
) -> list[RegistryCandidate]:
    candidates: list[RegistryCandidate] = []
    for position, obj in enumerate(payload.get("objects") or []):
        package = obj.get("package") or {}
        name = package.get("name")
        if not name:
            continue
        downloads = (obj.get("downloads") or {}).get("monthly")
        candidates.append(
            RegistryCandidate(
                name=name,
                category=category,
                query_index=query_index,
                position=position,
                downloads=downloads if isinstance(downloads, int) else None,
            )
        )
    return candidates


def _repository_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("url")
    return None


def _license_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("type")
    return None


def version_dependencies(version_data: dict[str, Any]) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    for field, (dependency_type, optional) in DEPENDENCY_TYPES.items():
        values = version_data.get(field)
        if not isinstance(values, dict):
            continue
        for name, constraint in values.items():
            if not name:
                continue
            dependencies.append(
                {
                    "name": name,
                    "constraint": constraint if isinstance(constraint, str) else None,
                    "dependency_type": dependency_type,
                    "optional": optional,
                }
            )

    bundled = version_data.get("bundledDependencies") or version_data.get(
        "bundleDependencies"
    )
    if isinstance(bundled, list):
        for name in bundled:
            if name:
                dependencies.append(
                    {
                        "name": name,
                        "constraint": None,
                        "dependency_type": "bundled",
                        "optional": False,
                    }
                )
    return dependencies


def parse_packument(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return package metadata and every release with its dependencies."""
    time_data = payload.get("time") or {}
    versions_data = payload.get("versions") or {}
    entries: list[dict[str, Any]] = []
    for version, version_data in versions_data.items():
        if not isinstance(version_data, dict):
            continue
        entries.append(
            {
                "version": version,
                "published_at": parse_timestamp(time_data.get(version)),
                "dependencies": version_dependencies(version_data),
            }
        )

    latest_version = (payload.get("dist-tags") or {}).get("latest")
    if not latest_version and entries:
        latest_version = entries[-1]["version"]

    dates = [entry["published_at"] for entry in entries if entry["published_at"]]
    metadata = {
        "name": payload.get("name"),
        "description": payload.get("description"),
        "homepage_url": payload.get("homepage"),
        "repository_url": _repository_url(payload.get("repository")),
        "license": _license_name(payload.get("license")),
        "latest_version": latest_version,
        "first_published_at": parse_timestamp(time_data.get("created"))
        or (min(dates) if dates else None),
        "latest_published_at": parse_timestamp(time_data.get(latest_version))
        if latest_version
        else None,
    }
    return metadata, entries


def choose_sample(
    candidates: Iterable[RegistryCandidate],
    popular_count: int,
    random_count: int,
    seed: int,
) -> list[RegistryCandidate]:
    popular_pool = sorted(
        (candidate for candidate in candidates if candidate.category == "popular"),
        key=lambda candidate: (
            -(candidate.downloads or 0),
            candidate.query_index,
            candidate.position,
        ),
    )
    chosen: list[RegistryCandidate] = []
    taken: set[str] = set()
    for candidate in popular_pool:
        if len(chosen) >= popular_count:
            break
        if candidate.name in taken:
            continue
        taken.add(candidate.name)
        chosen.append(candidate)

    random_pool = [
        candidate.name
        for candidate in candidates
        if candidate.category == "random" and candidate.name not in taken
    ]
    random_names = select_random_names(random_pool, random_count, seed, taken)
    by_name = {
        candidate.name: candidate
        for candidate in candidates
        if candidate.category == "random"
    }
    chosen.extend(by_name[name] for name in random_names if name in by_name)
    return chosen


class NpmRegistrySpider(BrowserSpider):
    name = "npm_registry"
    ecosystem = "npm"
    source = "registry.npmjs.org"
    selenium_meta = {"selenium": False}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._candidates: list[RegistryCandidate] = []
        self._pending_searches = 0
        self._scheduled = False

    async def start(self) -> AsyncIterator[Request]:
        popular_count = self.settings.getint("NPM_POPULAR_COUNT", 25)
        random_count = self.settings.getint("NPM_RANDOM_COUNT", 5)
        seed = self.settings.getint("NPM_SAMPLE_SEED", 1)
        queries = self.settings.getlist("NPM_SEARCH_QUERIES") or ["framework"]

        requests: list[Request] = []
        if popular_count > 0:
            for query_index, query in enumerate(queries):
                requests.append(
                    Request(
                        search_url(query, SEARCH_PAGE_SIZE),
                        callback=self.parse_search,
                        errback=self.search_errback,
                        meta={
                            "npm_registry_category": "popular",
                            "npm_registry_query_index": query_index,
                        },
                    )
                )

        for query_index, query in enumerate(self._random_queries(random_count, seed)):
            requests.append(
                Request(
                    search_url(query, 20),
                    callback=self.parse_search,
                    errback=self.search_errback,
                    meta={
                        "npm_registry_category": "random",
                        "npm_registry_query_index": query_index,
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
            query = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(3))
            if query in seen:
                continue
            seen.add(query)
            queries.append(query)
        return queries

    def parse_search(self, response: Response) -> Any:
        try:
            payload = json.loads(response.text)
        except ValueError as exc:
            self.record_error(
                response.url, "parse", "InvalidPayload", f"search response: {exc}"
            )
            payload = {}
        self._candidates.extend(
            parse_search_payload(
                payload,
                response.meta["npm_registry_category"],
                response.meta["npm_registry_query_index"],
            )
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

    def _schedule_packages(self) -> list[Request]:
        if self._scheduled:
            return []
        self._scheduled = True

        popular_count = self.settings.getint("NPM_POPULAR_COUNT", 25)
        random_count = self.settings.getint("NPM_RANDOM_COUNT", 5)
        seed = self.settings.getint("NPM_SAMPLE_SEED", 1)
        sample = choose_sample(
            self._candidates, popular_count, random_count, seed
        )

        requests: list[Request] = []
        for rank, candidate in enumerate(sample):
            category_rank = sum(
                1 for other in sample[:rank] if other.category == candidate.category
            )
            requests.append(
                Request(
                    packument_url(candidate.name),
                    callback=self.parse_packument,
                    errback=self.package_errback,
                    meta={
                        "npm_registry_name": candidate.name,
                        "npm_registry_category": candidate.category,
                        "npm_registry_rank": category_rank,
                        "npm_registry_seed": seed
                        if candidate.category == "random"
                        else None,
                        "npm_registry_downloads": candidate.downloads,
                    },
                )
            )

        self.logger.info(
            "Sampled %d npm packages from the registry (%d popular, %d random) "
            "from %d candidates",
            len(sample),
            sum(1 for candidate in sample if candidate.category == "popular"),
            sum(1 for candidate in sample if candidate.category == "random"),
            len(self._candidates),
        )
        return requests

    def parse_packument(self, response: Response) -> Any:
        try:
            payload = json.loads(response.text)
        except ValueError as exc:
            self.record_error(
                response.url, "parse", "InvalidPayload", f"packument: {exc}"
            )
            return None

        metadata, entries = parse_packument(payload)
        if not entries:
            self.record_error(
                response.url,
                "parse",
                "NoVersionsStored",
                f"packument has no releases for {response.meta['npm_registry_name']}",
            )
            return None

        limit = self.settings.getint("NPM_MAX_VERSIONS", 10)
        strategy = self.settings.get("NPM_VERSION_SELECTION", "spread")
        selected = select_versions(entries, limit, strategy)
        latest = metadata.get("latest_version")
        if latest and all(entry["version"] != latest for entry in selected):
            latest_entry = next(
                (entry for entry in entries if entry["version"] == latest), None
            )
            if latest_entry:
                selected.insert(0, latest_entry)

        name = metadata.get("name") or response.meta["npm_registry_name"]
        item = self.load_package(
            {
                "ecosystem": self.ecosystem,
                "name": name,
                "sampling_category": response.meta["npm_registry_category"],
                "sampling_rank": response.meta["npm_registry_rank"],
                "sampling_seed": response.meta.get("npm_registry_seed"),
                "description": metadata.get("description"),
                "homepage_url": metadata.get("homepage_url"),
                "repository_url": metadata.get("repository_url"),
                "license": metadata.get("license"),
                "latest_version": metadata.get("latest_version"),
                "first_published_at": metadata.get("first_published_at"),
                "latest_published_at": metadata.get("latest_published_at"),
                "downloads": response.meta.get("npm_registry_downloads"),
                "downloads_period": "last-month",
                "source": self.source,
                "source_url": response.url,
                "collection_method": "registry_api",
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

    def package_errback(self, failure: Any) -> None:
        super().errback(failure)
