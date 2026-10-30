"""NuGet collector backed by the nuget.org gallery.

The gallery is server-rendered, so this spider uses plain HTTP requests instead
of the Selenium middleware. The package list supplies the download ranking and
the random comparison pool; the package page supplies metadata; the full version
history comes from the gallery's anti-forgery-protected "load more" endpoint;
and each selected version page supplies its dependencies.

NuGet declares dependencies per target framework. The warehouse has one
dependency row per package and version, so the framework groups are merged by
package name: a dependency is stored if any target framework requires it.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote, urlparse

from scrapy import FormRequest, Request
from scrapy.http import Response

from warehouse.sampling import select_random_names, select_versions
from warehouse.spiders.base import BrowserSpider

BASE_URL = "https://www.nuget.org"
PACKAGES_PER_PAGE = 20
POPULAR_SORT = "totalDownloads-desc"
VERSION_DATE_FORMATS = ("%m/%d/%Y",)
SIZE_MULTIPLIERS = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def package_url(name: str) -> str:
    return f"{BASE_URL}/packages/{quote(name, safe='')}"


def package_version_url(name: str, version: str) -> str:
    return f"{package_url(name)}/{quote(version, safe='')}"


def search_url(page: int, sort: str | None = None) -> str:
    url = f"{BASE_URL}/packages?page={page}"
    if sort:
        url += f"&sortBy={sort}"
    return url


def package_name_from_url(url: str) -> str | None:
    path = urlparse(url).path
    if not path.startswith("/packages/"):
        return None
    remainder = unquote(path[len("/packages/") :]).strip("/")
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


def parse_compact_int(value: str | None) -> int | None:
    """Expand the abbreviated counts the gallery sidebar renders (9.7B)."""
    if not value:
        return None
    match = re.search(r"([\d.,]+)\s*([KMB])?", value.strip(), re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    multiplier = SIZE_MULTIPLIERS.get((match.group(2) or "").upper(), 1)
    return int(number * multiplier)


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in VERSION_DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


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


def _response_cookies(response: Response) -> dict[str, str]:
    """Keep the anti-forgery token bound to its own response.

    Concurrent package pages overwrite the shared cookie jar, so a token from an
    earlier page would otherwise be sent with a later page's cookie.
    """
    cookies: dict[str, str] = {}
    for header in response.headers.getlist(b"Set-Cookie"):
        pair = header.decode("latin-1").split(";", 1)[0]
        if "=" in pair:
            name, value = pair.split("=", 1)
            cookies[name.strip()] = value.strip()
    return cookies


def parse_package_listing(response: Response) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    for item in response.xpath("//li[contains(@class, 'package')]"):
        name = item.xpath(
            "string(.//a[contains(@class, 'package-title')]/@data-package-id)"
        ).get()
        if not name:
            href = item.xpath(
                "string(.//a[contains(@class, 'package-title')]/@href)"
            ).get()
            name = package_name_from_url(href or "")
        if not name:
            continue

        text = " ".join(item.xpath("string(.)").get().split())
        downloads_match = re.search(r"([\d,]+)\s+total downloads", text)
        packages.append(
            {
                "name": name.strip(),
                "downloads": _parse_int(downloads_match.group(1))
                if downloads_match
                else None,
            }
        )
    return packages


def parse_package_page(response: Response) -> dict[str, Any]:
    license_anchor = response.xpath(
        "//aside//a[starts-with(@href, 'https://licenses.nuget.org/')]"
    )
    license_text = license_anchor.xpath("string(.)").get()
    if license_text:
        license_text = re.sub(
            r"\s+license$", "", " ".join(license_text.split()), flags=re.IGNORECASE
        )

    return {
        "name": _text(
            response,
            "//h1/span[contains(concat(' ', normalize-space(@class), ' '), ' title ')]",
        )
        or package_name_from_url(response.url),
        "description": response.xpath(
            "//meta[@name='description']/@content"
        ).get(),
        "latest_version": _text(
            response,
            "//h1/span[contains(concat(' ', normalize-space(@class), ' '), ' version-title ')]",
        ),
        "license": license_text or None,
        "homepage_url": response.xpath(
            "//aside//a[@data-track='outbound-project-url']/@href"
        ).get(),
        "repository_url": response.xpath(
            "//aside//a[@data-track='outbound-repository-url']/@href"
        ).get(),
        "downloads": parse_compact_int(
            _text(
                response,
                "//aside//div[contains(@class, 'download-info-row')]"
                "[.//span[normalize-space()='Total']]"
                "//span[contains(@class, 'download-info-content')]",
            )
        ),
    }


def parse_version_table(response: Response) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in response.xpath("//tr[contains(@class, 'version-row')]"):
        version = _text(row, "td[1]")
        if not version or version in seen:
            continue
        seen.add(version)
        entries.append(
            {
                "version": version,
                "published_at": parse_date(_text(row, "td[3]")),
                "downloads": _parse_int(_text(row, "td[2]")),
                "is_prerelease": row.xpath("@data-prerelease").get() == "true",
            }
        )
    return entries


def parse_dependencies_page(response: Response) -> list[dict[str, Any]]:
    dependencies: dict[str, dict[str, Any]] = {}
    for group in response.xpath("//ul[@id='dependency-groups']/li"):
        for item in group.xpath(
            ".//ul[contains(@class, 'dependency-group')]/li[a]"
        ):
            href = item.xpath("string(a/@href)").get() or ""
            if "/packages/" not in href:
                continue
            name = unquote(href.split("/packages/", 1)[1]).strip("/").split("/")[0]
            if not name:
                continue
            constraint = _text(item, "span")
            if constraint:
                constraint = constraint.strip().strip("()").strip()
            normalized = name.lower()
            if normalized in dependencies:
                continue
            dependencies[normalized] = {
                "name": name,
                "constraint": constraint or None,
                "dependency_type": "runtime",
                "optional": False,
            }
    return list(dependencies.values())


class NugetSpider(BrowserSpider):
    name = "nuget"
    ecosystem = "NuGet"
    source = "nuget.org"

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
        popular_count = self.settings.getint("NUGET_POPULAR_COUNT", 25)
        random_count = self.settings.getint("NUGET_RANDOM_COUNT", 5)
        seed = self.settings.getint("NUGET_SAMPLE_SEED", 1)

        requests: list[Request] = []
        if popular_count > 0:
            pages = max(1, math.ceil(popular_count / PACKAGES_PER_PAGE))
            self._pending_discovery += pages
            for page in range(1, pages + 1):
                requests.append(
                    Request(
                        search_url(page, POPULAR_SORT),
                        callback=self.parse_popular,
                        errback=self.discovery_errback,
                        meta={"nuget_discovery": "popular"},
                    )
                )
        else:
            self._popular_ready = True

        pages = max(0, self.settings.getint("NUGET_RANDOM_PAGES", 5))
        if random_count > 0 and pages > 0:
            page_limit = max(1, self.settings.getint("NUGET_PAGE_LIMIT", 27000))
            chosen = random.Random(seed).sample(
                range(1, page_limit + 1), min(pages, page_limit)
            )
            self._pending_discovery += len(chosen)
            for page in chosen:
                requests.append(
                    Request(
                        search_url(page),
                        callback=self.parse_random,
                        errback=self.discovery_errback,
                        meta={"nuget_discovery": "random"},
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
        for entry in parse_package_listing(response):
            self.random_pool.setdefault(entry["name"], entry)
        self._random_ready = True
        return self._discovery_finished()

    def discovery_errback(self, failure: Any) -> Any:
        super().errback(failure)
        if failure.request.meta.get("nuget_discovery") == "popular":
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

        popular_count = self.settings.getint("NUGET_POPULAR_COUNT", 25)
        random_count = self.settings.getint("NUGET_RANDOM_COUNT", 5)
        seed = self.settings.getint("NUGET_SAMPLE_SEED", 1)

        popular = self.popular[:popular_count]
        popular_names = {entry["name"] for entry in popular}
        random_names = select_random_names(
            list(self.random_pool), random_count, seed, popular_names
        )
        downloads = {
            entry["name"]: entry["downloads"] for entry in self.popular
        }
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
            request = Request(
                package_url(entry["name"]),
                callback=self.parse_package,
                errback=self.package_errback,
                meta={
                    "nuget_name": entry["name"],
                    "nuget_category": entry["category"],
                    "nuget_rank": entry["rank"],
                    "nuget_seed": entry["seed"],
                    "nuget_downloads": downloads.get(entry["name"]),
                },
            )
            crawl.engine.crawl(request)

        self.logger.info(
            "Sampled %d NuGet packages (%d popular, %d random) from %d candidates",
            len(sample),
            len(popular),
            len(random_names),
            len(self.random_pool),
        )
        return None

    def parse_package(self, response: Response) -> Any:
        name = response.meta["nuget_name"]
        metadata = parse_package_page(response)
        if not metadata.get("name"):
            metadata["name"] = name
        if not metadata.get("latest_version"):
            self.record_error(
                response.url,
                "parse",
                "MissingField",
                f"latest version not found for {name}",
            )

        draft = {
            "name": name,
            "category": response.meta["nuget_category"],
            "rank": response.meta["nuget_rank"],
            "seed": response.meta["nuget_seed"],
            "downloads": response.meta.get("nuget_downloads")
            or metadata.get("downloads"),
            "metadata": metadata,
            "versions": [],
            "fallback": parse_version_table(response),
            "pending": 0,
        }
        self._drafts[name] = draft

        token = response.xpath(
            "//form[@id='AntiForgeryForm']"
            "//input[@name='__RequestVerificationToken']/@value"
        ).get()
        data_url = response.xpath(
            "//button[@id='load-more-versions']/@data-url"
        ).get()
        if not token or not data_url:
            return self._start_version_pages(draft, draft["fallback"])

        return FormRequest(
            f"{BASE_URL}{data_url}",
            method="POST",
            formdata={"__RequestVerificationToken": token},
            callback=self.parse_versions,
            errback=self.versions_errback,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": response.url,
            },
            cookies=_response_cookies(response),
            meta={"nuget_name": name},
        )

    def parse_versions(self, response: Response) -> Any:
        draft = self._drafts.get(response.meta["nuget_name"])
        if draft is None:
            return None
        entries = parse_version_table(response) or draft["fallback"]
        return self._start_version_pages(draft, entries)

    def versions_errback(self, failure: Any) -> Any:
        super().errback(failure)
        draft = self._drafts.get(failure.request.meta.get("nuget_name"))
        if draft is None:
            return None
        self.record_error(
            failure.request.url,
            "versions",
            "VersionListFailed",
            f"full version list unavailable for {draft['name']}, using the first page",
        )
        return self._start_version_pages(draft, draft["fallback"])

    def _start_version_pages(
        self, draft: dict[str, Any], entries: list[dict[str, Any]]
    ) -> Any:
        if not entries:
            self.record_error(
                package_url(draft["name"]),
                "parse",
                "NoVersionsStored",
                f"version history not rendered for {draft['name']}",
            )
            return self._finish(draft)

        metadata = draft["metadata"]
        dates = [
            entry["published_at"]
            for entry in entries
            if entry.get("published_at") is not None
        ]
        metadata["first_published_at"] = min(dates) if dates else None

        latest = metadata.get("latest_version")
        latest_entry = next(
            (entry for entry in entries if entry["version"] == latest), None
        )
        metadata["latest_published_at"] = (
            latest_entry["published_at"] if latest_entry else None
        )

        limit = self.settings.getint("NUGET_MAX_VERSIONS", 10)
        strategy = self.settings.get("NUGET_VERSION_SELECTION", "spread")
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
                Request(
                    package_version_url(draft["name"], entry["version"]),
                    callback=self.parse_version_page,
                    errback=self.version_errback,
                    meta={
                        "nuget_name": draft["name"],
                        "nuget_version": entry["version"],
                    },
                )
            )
        return requests

    def parse_version_page(self, response: Response) -> Any:
        draft = self._drafts.get(response.meta.get("nuget_name"))
        if draft is None:
            return None

        version = response.meta["nuget_version"]
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
        draft = self._drafts.pop(failure.request.meta.get("nuget_name"), None)
        if draft is not None:
            self.record_error(
                package_url(draft["name"]),
                "package",
                "DownloadFailed",
                f"package page unavailable for {draft['name']}",
            )
        return None

    def version_errback(self, failure: Any) -> Any:
        super().errback(failure)
        draft = self._drafts.get(failure.request.meta.get("nuget_name"))
        if draft is None:
            return None

        version = failure.request.meta.get("nuget_version")
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
                "collection_method": "http",
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
