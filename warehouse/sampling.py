from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any


def select_versions(
    entries: list[dict[str, Any]], limit: int, strategy: str
) -> list[dict[str, Any]]:
    if limit <= 0 or len(entries) <= limit:
        return list(entries)
    if strategy == "downloads":
        return entries[:limit]
    if limit == 1:
        return [entries[0]]

    # Spread the sample over the release history. Taking only the most
    # downloaded versions hides exactly the old versions advisories target.
    dated = sorted(
        entries,
        key=lambda entry: entry.get("published_at")
        or datetime.min.replace(tzinfo=timezone.utc),
    )
    step = (len(dated) - 1) / (limit - 1)
    indices = sorted({round(index * step) for index in range(limit)})
    return [dated[index] for index in indices]


def select_random_names(
    pool: list[str], count: int, seed: int, exclude: set[str]
) -> list[str]:
    candidates = sorted({name for name in pool if name not in exclude})
    rng = random.Random(seed)
    rng.shuffle(candidates)
    return candidates[:count]
