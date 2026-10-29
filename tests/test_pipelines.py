from __future__ import annotations

from datetime import datetime, timezone

import pytest
from scrapy.exceptions import DropItem

from warehouse.pipelines import DuplicatePackagePipeline, ValidationPipeline
from warehouse.processing import (
    dedupe_key,
    normalize_license,
    normalize_package_name,
    normalize_repository_url,
    normalize_url,
    parse_timestamp,
)


class FakeSettings:
    def __init__(self, values: dict[str, list[str]]) -> None:
        self.values = values

    def getlist(self, name: str) -> list[str]:
        return self.values.get(name, [])


class FakeStats:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    def inc_value(self, key: str, count: int = 1) -> None:
        self.values[key] = self.values.get(key, 0) + count


def test_normalize_repository_url_covers_git_prefixes() -> None:
    assert normalize_repository_url("git+https://github.com/a/b.git") == "https://github.com/a/b"
    assert normalize_repository_url("git://github.com/a/b") == "https://github.com/a/b"
    assert normalize_url("//example.test/x") == "https://example.test/x"


def test_normalize_license_accepts_common_shapes() -> None:
    assert normalize_license({"type": "MIT"}) == "MIT"
    assert normalize_license(["MIT", "Apache-2.0"]) == "MIT, Apache-2.0"
    assert normalize_license("  ") is None


def test_parse_timestamp_treats_naive_values_as_utc() -> None:
    assert parse_timestamp("2026-10-09T12:56:54.505Z") == datetime(
        2026, 10, 9, 12, 56, 54, 505000, tzinfo=timezone.utc
    )
    assert parse_timestamp(None) is None


def test_package_names_are_compared_case_insensitively() -> None:
    assert normalize_package_name("  React ") == "react"
    assert dedupe_key("npm", "React") == ("npm", "react")


def validation_pipeline() -> tuple[ValidationPipeline, FakeStats]:
    stats = FakeStats()
    settings = FakeSettings(
        {
            "REQUIRED_FIELDS": [
                "ecosystem",
                "name",
                "sampling_category",
                "source",
                "collected_at",
            ],
            "SUPPORTED_ECOSYSTEMS": ["npm"],
        }
    )
    return ValidationPipeline(settings, stats), stats


def valid_item() -> dict:
    return {
        "ecosystem": "npm",
        "name": "express",
        "sampling_category": "popular",
        "sampling_rank": 0,
        "source": "npmjs.com",
        "collected_at": datetime.now(timezone.utc),
        "versions": [
            {
                "version": "5.3.0",
                "published_at": datetime.now(timezone.utc),
                "dependencies": [],
            }
        ],
    }


def test_validation_accepts_a_complete_item() -> None:
    pipeline, stats = validation_pipeline()
    item = valid_item()

    assert pipeline.process_item(item, spider=None) is item
    assert stats.values["validation/accepted"] == 1


def test_validation_rejects_missing_required_fields() -> None:
    pipeline, _ = validation_pipeline()
    item = valid_item()
    item.pop("source")

    with pytest.raises(DropItem, match="missing required fields"):
        pipeline.process_item(item, spider=None)


def test_validation_rejects_duplicate_versions() -> None:
    pipeline, _ = validation_pipeline()
    item = valid_item()
    item["versions"].append({"version": "5.3.0"})

    with pytest.raises(DropItem, match="duplicate version"):
        pipeline.process_item(item, spider=None)


def test_validation_rejects_non_integer_downloads() -> None:
    pipeline, _ = validation_pipeline()
    item = valid_item()
    item["downloads"] = "1.2M"

    with pytest.raises(DropItem, match="downloads must be a non-negative integer"):
        pipeline.process_item(item, spider=None)


def test_duplicate_pipeline_drops_repeated_package() -> None:
    stats = FakeStats()
    pipeline = DuplicatePackagePipeline(stats)
    item = valid_item()

    assert pipeline.process_item(item, spider=None) is item
    with pytest.raises(DropItem, match="duplicate package"):
        pipeline.process_item(dict(item), spider=None)
    assert stats.values["dedupe/dropped"] == 1
