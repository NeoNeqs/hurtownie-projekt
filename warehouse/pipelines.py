from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from scrapy.exceptions import DropItem, NotConfigured

from warehouse.db import connect, get_dsn, seed_ecosystems
from warehouse.processing import dedupe_key, normalize_package_name

logger = logging.getLogger(__name__)

SAMPLING_CATEGORIES = {"popular", "random"}


class ValidationPipeline:
    def __init__(self, settings: Any, stats: Any) -> None:
        self.stats = stats
        self.required = tuple(settings.getlist("REQUIRED_FIELDS"))
        self.ecosystems = set(settings.getlist("SUPPORTED_ECOSYSTEMS"))

    @classmethod
    def from_crawler(cls, crawler: Any) -> "ValidationPipeline":
        return cls(crawler.settings, crawler.stats)

    def process_item(self, item: Any, spider: Any) -> Any:
        def reject(reason: str) -> None:
            self.stats.inc_value("validation/rejected")
            raise DropItem(f"validation failed: {reason}")

        missing = [field for field in self.required if not item.get(field)]
        if missing:
            reject(f"missing required fields: {', '.join(missing)}")

        if item["ecosystem"] not in self.ecosystems:
            reject(f"unsupported ecosystem: {item['ecosystem']!r}")
        if item["sampling_category"] not in SAMPLING_CATEGORIES:
            reject(f"invalid sampling_category: {item['sampling_category']!r}")

        name = str(item["name"]).strip()
        if not name:
            reject("package name is empty")

        versions = item.get("versions")
        if versions is None:
            reject("versions list is missing")
        if not isinstance(versions, list):
            reject("versions must be a list")

        seen: set[str] = set()
        for entry in versions:
            if not isinstance(entry, dict) or not str(entry.get("version") or "").strip():
                reject(f"invalid version entry for {name}")
            version = str(entry["version"]).strip()
            if version in seen:
                reject(f"duplicate version {version!r} for {name}")
            seen.add(version)

            dependencies = entry.get("dependencies")
            if dependencies is not None and not isinstance(dependencies, list):
                reject(f"dependencies must be a list for {name}@{version}")

            published_at = entry.get("published_at")
            if published_at is not None and not isinstance(published_at, datetime):
                reject(f"published_at must be datetime for {name}@{version}")

        for field in ("collected_at", "first_published_at", "latest_published_at"):
            value = item.get(field)
            if value is not None and not isinstance(value, datetime):
                reject(f"{field} must be datetime")

        downloads = item.get("downloads")
        if downloads is not None and (not isinstance(downloads, int) or downloads < 0):
            reject("downloads must be a non-negative integer")

        latest = item.get("latest_version")
        if latest and seen and latest not in seen:
            self.stats.inc_value("validation/warnings/latest_version_missing")
            logger.warning(
                "%s: latest_version %r is not in the collected version list", name, latest
            )

        self.stats.inc_value("validation/accepted")
        return item


class DuplicatePackagePipeline:
    def __init__(self, stats: Any) -> None:
        self.stats = stats
        self.seen: set[tuple[str, str]] = set()

    @classmethod
    def from_crawler(cls, crawler: Any) -> "DuplicatePackagePipeline":
        return cls(crawler.stats)

    def process_item(self, item: Any, spider: Any) -> Any:
        key = dedupe_key(item["ecosystem"], item["name"])
        if key in self.seen:
            self.stats.inc_value("dedupe/dropped")
            raise DropItem(f"duplicate package: {key!r}")
        self.seen.add(key)
        return item


class StoragePipeline:
    def __init__(self, dsn: str, stats: Any) -> None:
        self.dsn = dsn
        self.stats = stats
        self.conn: Any = None
        self.run_id: int | None = None
        self._ecosystems: dict[str, int] = {}
        self.stored = 0
        self.failed = 0

    @classmethod
    def from_crawler(cls, crawler: Any) -> "StoragePipeline":
        dsn = get_dsn()
        if not dsn:
            raise NotConfigured("WAREHOUSE_DSN is not set")
        return cls(dsn, crawler.stats)

    def open_spider(self, spider: Any) -> None:
        self.conn = connect(self.dsn)
        seed_ecosystems(self.conn)
        source = getattr(spider, "source", spider.name)
        # A killed process cannot close its run; do not leave it "running" forever.
        self.conn.execute(
            """
            UPDATE collection_run
            SET status = 'partial', finished_at = now(),
                notes = COALESCE(notes, '') || ' [abandoned: process ended without shutdown]'
            WHERE source = %s AND status = 'running'
              AND started_at < now() - interval '1 hour'
            """,
            (source,),
        )
        row = self.conn.execute(
            "INSERT INTO collection_run (source, spider, status) VALUES (%s, %s, 'running') RETURNING id",
            (source, spider.name),
        ).fetchone()
        self.run_id = row["id"]
        self.conn.commit()
        spider.logger.info("Collection run %s started", self.run_id)

    def close_spider(self, spider: Any, reason: str = "finished") -> None:
        if self.conn is None:
            return

        errors = getattr(spider, "collection_errors", [])
        status = "succeeded"
        if self.stored == 0:
            status = "failed"
        elif self.failed or errors:
            status = "partial"
        if reason in {"shutdown", "cancel"} and self.stored:
            status = "partial"
        with self.conn.transaction():
            if errors:
                with self.conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO collection_error
                            (collection_run_id, url, stage, error_type, message, occurred_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        [
                            (
                                self.run_id,
                                error.get("url"),
                                error.get("stage"),
                                error.get("error_type"),
                                error.get("message"),
                                error.get("occurred_at", datetime.now().astimezone()),
                            )
                            for error in errors
                        ],
                    )
            self.conn.execute(
                """
                UPDATE collection_run
                SET finished_at = now(), status = %s, items_stored = %s,
                    items_scraped = %s, items_rejected = %s, errors = %s
                WHERE id = %s
                """,
                (
                    status,
                    self.stored,
                    self.stats.get_value("item_scraped_count", 0),
                    self.stats.get_value("item_dropped_count", 0),
                    self.failed + len(errors),
                    self.run_id,
                ),
            )
        self.conn.close()
        self.conn = None
        spider.logger.info(
            "Collection run %s finished: %s, %d stored, %d errors",
            self.run_id,
            status,
            self.stored,
            self.failed + len(errors),
        )

    def process_item(self, item: Any, spider: Any) -> Any:
        try:
            with self.conn.transaction():
                ecosystem_id = self._ecosystem_id(item["ecosystem"])
                package_id = self._upsert_package(ecosystem_id, item)
                self._record_sample(package_id, item)
                for version in item.get("versions") or []:
                    version_id = self._upsert_version(package_id, version, item["source"])
                    self._replace_dependencies(version_id, version.get("dependencies") or [])
                self._insert_observation(package_id, item)
                self._insert_download(package_id, item)
        except Exception as exc:
            self.failed += 1
            self.stats.inc_value("storage/failed")
            logger.exception("Failed to store %s", item.get("name"))
            raise DropItem(f"storage failed for {item.get('name')!r}: {exc}") from exc

        self.stored += 1
        self.stats.inc_value("storage/stored")
        return item

    def _ecosystem_id(self, name: str) -> int:
        cached = self._ecosystems.get(name)
        if cached is not None:
            return cached
        row = self.conn.execute(
            "SELECT id FROM ecosystem WHERE name = %s", (name,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown ecosystem: {name!r}")
        self._ecosystems[name] = row["id"]
        return row["id"]

    def _upsert_package(self, ecosystem_id: int, item: Any) -> int:
        row = self.conn.execute(
            """
            INSERT INTO package (
                ecosystem_id, name, normalized_name, description, homepage_url,
                repository_url, license, latest_version, first_published_at,
                latest_published_at, sampling_category, sampling_rank, sampling_seed,
                source, source_url, collection_method
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ecosystem_id, normalized_name) DO UPDATE SET
                name = EXCLUDED.name,
                description = COALESCE(EXCLUDED.description, package.description),
                homepage_url = COALESCE(EXCLUDED.homepage_url, package.homepage_url),
                repository_url = COALESCE(EXCLUDED.repository_url, package.repository_url),
                license = COALESCE(EXCLUDED.license, package.license),
                latest_version = COALESCE(EXCLUDED.latest_version, package.latest_version),
                first_published_at = COALESCE(package.first_published_at, EXCLUDED.first_published_at),
                latest_published_at = COALESCE(EXCLUDED.latest_published_at, package.latest_published_at),
                last_seen_at = now()
            RETURNING id
            """,
            (
                ecosystem_id,
                item["name"],
                normalize_package_name(item["name"]),
                item.get("description"),
                item.get("homepage_url"),
                item.get("repository_url"),
                item.get("license"),
                item.get("latest_version"),
                item.get("first_published_at"),
                item.get("latest_published_at"),
                item["sampling_category"],
                item.get("sampling_rank"),
                item.get("sampling_seed"),
                item["source"],
                item.get("source_url"),
                item.get("collection_method"),
            ),
        ).fetchone()
        return row["id"]

    def _record_sample(self, package_id: int, item: Any) -> None:
        self.conn.execute(
            """
            INSERT INTO sampled_package (
                collection_run_id, package_id, sampling_category,
                sampling_rank, sampling_seed
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (collection_run_id, package_id) DO UPDATE SET
                sampling_category = EXCLUDED.sampling_category,
                sampling_rank = EXCLUDED.sampling_rank,
                sampling_seed = EXCLUDED.sampling_seed
            """,
            (
                self.run_id,
                package_id,
                item["sampling_category"],
                item.get("sampling_rank"),
                item.get("sampling_seed"),
            ),
        )

    def _upsert_version(self, package_id: int, version: dict[str, Any], source: str) -> int:
        row = self.conn.execute(
            """
            INSERT INTO package_version (
                package_id, version, published_at, license, is_deprecated, source
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (package_id, version) DO UPDATE SET
                published_at = COALESCE(EXCLUDED.published_at, package_version.published_at),
                license = COALESCE(EXCLUDED.license, package_version.license),
                is_deprecated = EXCLUDED.is_deprecated,
                last_seen_at = now()
            RETURNING id
            """,
            (
                package_id,
                str(version["version"]),
                version.get("published_at"),
                version.get("license"),
                bool(version.get("deprecated", False)),
                source,
            ),
        ).fetchone()
        return row["id"]

    def _replace_dependencies(
        self, version_id: int, dependencies: list[dict[str, Any]]
    ) -> None:
        self.conn.execute(
            "DELETE FROM dependency WHERE package_version_id = %s", (version_id,)
        )
        if not dependencies:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO dependency (
                    package_version_id, dependency_name, dependency_normalized_name,
                    declared_constraint, dependency_type, optional
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (package_version_id, dependency_normalized_name, dependency_type)
                DO UPDATE SET
                    dependency_name = EXCLUDED.dependency_name,
                    declared_constraint = EXCLUDED.declared_constraint,
                    optional = EXCLUDED.optional
                """,
                [
                    (
                        version_id,
                        dependency["name"],
                        normalize_package_name(dependency["name"]),
                        dependency.get("constraint"),
                        dependency.get("dependency_type", "runtime"),
                        bool(dependency.get("optional", False)),
                    )
                    for dependency in dependencies
                ],
            )

    def _insert_observation(self, package_id: int, item: Any) -> None:
        self.conn.execute(
            """
            INSERT INTO package_observation (
                package_id, collection_run_id, source, description, homepage_url,
                repository_url, license, latest_version, downloads
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                package_id,
                self.run_id,
                item["source"],
                item.get("description"),
                item.get("homepage_url"),
                item.get("repository_url"),
                item.get("license"),
                item.get("latest_version"),
                item.get("downloads"),
            ),
        )

    def _insert_download(self, package_id: int, item: Any) -> None:
        downloads = item.get("downloads")
        if downloads is None:
            return
        self.conn.execute(
            """
            INSERT INTO download_observation (
                package_id, collection_run_id, source, period, downloads
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (package_id, period, source, observed_at) DO NOTHING
            """,
            (
                package_id,
                self.run_id,
                item["source"],
                item.get("downloads_period") or "total",
                downloads,
            ),
        )
