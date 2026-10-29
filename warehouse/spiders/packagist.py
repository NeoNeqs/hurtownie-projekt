"""Packagist collector backed by packagist.org pages.

Popular discovery uses the explore pages, which are ordered by downloads. The
random comparison group is drawn from the published package name list. A
package page carries metadata and the full version list; each selected version's
own JSON panel supplies its release date and requirements.

Platform requirements (php, ext-*) are present in the requirement lists but are
not Composer packages, so only entries linking to /packages/ are stored.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

from scrapy import Request, Selector
from scrapy.http import Response

from warehouse.sampling import select_random_names, select_versions
from warehouse.spiders.base import BrowserSpider

BASE_URL = "https://packagist.org"
POPULAR_URL = f"{BASE_URL}/explore/popular"
PACKAGE_LIST_URL = f"{BASE_URL}/packages/list.json"
PACKAGES_PER_PAGE = 15
MONTH_DAY_FORMATS = ("%Y-%m-%d %H:%M UTC",)


def package_url(name: str) -> str:
    return f"{BASE_URL}/packages/{name}"


def popular_url(page: int) -> str:
    return f"{POPULAR_URL}?page={page}"


def package_name_from_url(url: str) -> str | None:
    path = urlparse(url).path
    if not path.startswith("/packages/"):
        return None
    name = unquote(path[len("/packages/") :]).strip("/")
    return name or None


def _text(response: Response | Selector, xpath: str) -> str | None:
    value = response.xpath(f"string({xpath})").get()
    return value.strip() if value and value.strip() else None


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in MONTH_DAY_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_popular_page(response: Response) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    for item in response.xpath("//ul[contains(@class, 'packages')]/li"):
        name = item.xpath(
            "string(.//h4/a[starts-with(@href, '/packages/')]/@href)"
        ).get()
        name = unquote((name or "").strip("/"))
        if name.startswith("packages/"):
            name = name[len("packages/") :]
        if not name or "/" not in name:
            continue
        downloads = _parse_int(
            item.xpath(
                "string(.//span[contains(@class, 'metadata-block')][1])"
            ).get()
        )
        packages.append({"name": name, "downloads": downloads})
    return packages


def parse_package_list(payload: str) -> list[str]:
    data = json.loads(payload)
    names = data.get("packageNames") if isinstance(data, dict) else data
    return [
        name
        for name in (names or [])
        if isinstance(name, str)
        and name.count("/") == 1
        and "-dev/" not in name
        and not name.split("/")[0].endswith("-dev")
    ]


def parse_version_panel(payload: str) -> dict[str, Any]:
    data = json.loads(payload)
    content = Selector(text=data.get("content") or "")
    release_date = _text(content, "//span[contains(@class, 'release-date')]")

    dependencies: list[dict[str, Any]] = []
    for group in content.xpath(
        "//div[contains(@class, 'requires') or contains(@class, 'devRequires')]"
    ):
        label = " ".join(
            group.xpath("string(p[contains(@class, 'link-type')])").get().split()
        ).lower()
        if label == "requires":
            dependency_type = "runtime"
        elif label == "requires (dev)":
            dependency_type = "dev"
        else:
            continue

        for item in group.xpath("./ul/li[a[starts-with(@href, '/packages/')]]"):
            href = item.xpath("string(a[starts-with(@href, '/packages/')]/@href)").get()
            name = unquote(href[len("/packages/") :].strip("/"))
            constraint = _text(item, ".")
            if constraint and ":" in constraint:
                constraint = constraint.split(":", 1)[1].strip()
            dependencies.append(
                {
                    "name": name,
                    "constraint": constraint or None,
                    "dependency_type": dependency_type,
                    "optional": False,
                }
            )

    return {
        "published_at": parse_date(release_date),
        "dependencies": dependencies,
    }


def parse_package_page(response: Response) -> dict[str, Any]:
    title = response.xpath("string(//title)").get() or ""
    name = title.split(" - ")[0].strip() or package_name_from_url(response.url)

    license_text = _text(
        response, "//p[contains(@class, 'license')]/text()[1]"
    )

    publish_text = _text(response, "//p[contains(@class, 'publish-time')]")
    publish_match = re.search(r"on (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC", publish_text or "")
    latest_published_at = (
        parse_date(f"{publish_match.group(1)} UTC") if publish_match else None
    )

    installs = _text(
        response,
        "//a[contains(normalize-space(.), 'Installs')]"
        "/ancestor::p[1]",
    )
    downloads = None
    if installs:
        installs_match = re.search(r"([\d\u2009\u00a0 ,]+)\s*$", installs)
        if installs_match:
            downloads = _parse_int(installs_match.group(1))

    return {
        "name": name,
        "description": _text(response, "//p[contains(@class, 'description')]")
        or response.xpath("//meta[@name='description']/@content").get(),
        "license": license_text,
        "repository_url": response.xpath(
            "//p[contains(@class, 'canonical')]/a/@href"
        ).get(),
        "homepage_url": response.xpath(
            "//a[normalize-space()='Homepage']/@href"
        ).get(),
        "downloads": downloads,
        "latest_published_at": latest_published_at,
    }


def parse_version_list(response: Response) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in response.xpath(
        "//ul[contains(@class, 'versions')]/li[contains(@class, 'version')]"
    ):
        version = item.xpath("string(.//a[contains(@class, 'version-number')])").get()
        if not version or not version.strip():
            continue
        version = version.strip()
        if version in seen:
            continue
        seen.add(version)
        panel = item.xpath("@data-load-more").get()
        entries.append(
            {
                "version": version,
                "published_at": None,
                "panel_url": f"{BASE_URL}{panel}" if panel else None,
                # Branch aliases appear as both 13.x-dev and dev-main.
                "is_dev": version.endswith("-dev") or version.startswith("dev-"),
                "is_current": "open" in (item.xpath("@class").get() or ""),
            }
        )
    return entries


class PackagistSpider(BrowserSpider):
    name = "packagist"
    ecosystem = "Packagist"
    source = "packagist.org"
    selenium_meta = {
        "selenium": True,
        "selenium_wait_for": "ul.versions",
        "selenium_wait": 0,
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.popular: list[dict[str, Any]] = []
        self.random_pool: list[str] = []
        self._pending_discovery = 0
        self._popular_ready = False
        self._random_ready = False
        self._scheduled = False
        self._drafts: dict[str, dict[str, Any]] = {}

    async def start(self) -> AsyncIterator[Request]:
        popular_count = self.settings.getint("PACKAGIST_POPULAR_COUNT", 25)
        random_count = self.settings.getint("PACKAGIST_RANDOM_COUNT", 5)
        seed = self.settings.getint("PACKAGIST_SAMPLE_SEED", 1)

        requests: list[Request] = []
        if popular_count > 0:
            pages = max(1, math.ceil(popular_count / PACKAGES_PER_PAGE))
            self._pending_discovery += pages
            for page in range(1, pages + 1):
                requests.append(
                    Request(
                        popular_url(page),
                        callback=self.parse_popular,
                        errback=self.discovery_errback,
                        meta={"packagist_discovery": "popular", "selenium": False},
                    )
                )
        else:
            self._popular_ready = True

        if random_count > 0:
            self._pending_discovery += 1
            requests.append(
                Request(
                    PACKAGE_LIST_URL,
                    callback=self.parse_package_list,
                    errback=self.discovery_errback,
                    meta={"packagist_discovery": "random", "selenium": False},
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
        self.popular.extend(parse_popular_page(response))
        self._popular_ready = True
        return self._discovery_finished()

    def parse_package_list(self, response: Response) -> Any:
        try:
            self.random_pool = parse_package_list(response.text)
        except ValueError as exc:
            self.record_error(
                response.url, "parse", "InvalidPayload", f"package list: {exc}"
            )
        self._random_ready = True
        return self._discovery_finished()

    def discovery_errback(self, failure: Any) -> Any:
        super().errback(failure)
        if failure.request.meta.get("packagist_discovery") == "popular":
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

        popular_count = self.settings.getint("PACKAGIST_POPULAR_COUNT", 25)
        random_count = self.settings.getint("PACKAGIST_RANDOM_COUNT", 5)
        seed = self.settings.getint("PACKAGIST_SAMPLE_SEED", 1)

        popular = self.popular[:popular_count]
        popular_names = {entry["name"] for entry in popular}
        random_names = select_random_names(
            self.random_pool, random_count, seed, popular_names
        )
        downloads = {entry["name"]: entry["downloads"] for entry in self.popular}

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
                package_url(entry["name"]),
                callback=self.parse_package,
                meta={
                    "packagist_name": entry["name"],
                    "packagist_category": entry["category"],
                    "packagist_rank": entry["rank"],
                    "packagist_seed": entry["seed"],
                    "packagist_downloads": downloads.get(entry["name"]),
                },
            )
            crawl.engine.crawl(request)

        self.logger.info(
            "Sampled %d Packagist packages (%d popular, %d random) from %d candidates",
            len(sample),
            len(popular),
            len(random_names),
            len(self.random_pool),
        )
        return None

    def parse_package(self, response: Response) -> Any:
        name = response.meta["packagist_name"]
        metadata = parse_package_page(response)
        entries = [entry for entry in parse_version_list(response) if not entry["is_dev"]]
        if not entries:
            self.record_error(
                response.url,
                "parse",
                "NoVersionsStored",
                f"version list not rendered for {name}",
            )
            return None

        latest_entry = next(
            (entry for entry in entries if entry["is_current"]), entries[0]
        )
        metadata["latest_version"] = latest_entry["version"]
        if not metadata.get("latest_published_at"):
            metadata["latest_published_at"] = latest_entry["published_at"]

        limit = self.settings.getint("PACKAGIST_MAX_VERSIONS", 10)
        strategy = self.settings.get("PACKAGIST_VERSION_SELECTION", "spread")
        # Release dates live behind the per-version panel, so the first pass
        # spreads over the rendered version order and dates arrive with panels.
        selected = select_versions(entries, limit, strategy)
        if latest_entry not in selected:
            selected.insert(0, latest_entry)

        draft = {
            "name": name,
            "category": response.meta["packagist_category"],
            "rank": response.meta["packagist_rank"],
            "seed": response.meta["packagist_seed"],
            "downloads": response.meta.get("packagist_downloads"),
            "metadata": metadata,
            "versions": selected,
            "pending": 0,
        }
        self._drafts[name] = draft

        requests = []
        for entry in selected:
            if not entry["panel_url"]:
                continue
            draft["pending"] += 1
            requests.append(
                Request(
                    entry["panel_url"],
                    callback=self.parse_version_panel,
                    errback=self.version_errback,
                    meta={
                        "packagist_key": name,
                        "packagist_version": entry["version"],
                        "selenium": False,
                    },
                    dont_filter=True,
                )
            )
        if draft["pending"] == 0:
            return self._finish(draft)
        return requests

    def parse_version_panel(self, response: Response) -> Any:
        draft = self._drafts.get(response.meta.get("packagist_key"))
        if draft is None:
            return None

        version = response.meta["packagist_version"]
        entry = next(
            (e for e in draft["versions"] if e["version"] == version), None
        )
        if entry is not None:
            panel = parse_version_panel(response.text)
            entry["published_at"] = panel["published_at"]
            entry["dependencies"] = panel["dependencies"]

        draft["pending"] -= 1
        if draft["pending"] <= 0:
            return self._finish(draft)
        return None

    def version_errback(self, failure: Any) -> Any:
        super().errback(failure)
        draft = self._drafts.get(failure.request.meta.get("packagist_key"))
        if draft is None:
            return None

        version = failure.request.meta.get("packagist_version")
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
        dates = [
            entry["published_at"]
            for entry in draft["versions"]
            if entry.get("published_at") is not None
        ]
        for entry in draft["versions"]:
            entry.pop("panel_url", None)
            entry.pop("is_dev", None)
            entry.pop("is_current", None)

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
                "first_published_at": min(dates) if dates else None,
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
