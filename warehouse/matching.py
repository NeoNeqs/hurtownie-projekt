"""Match collected package versions against OSV affected ranges."""

from __future__ import annotations

import argparse
import logging
from typing import Any, Iterable

from warehouse.db import connect, get_dsn
from warehouse.processing import normalize_package_name
from warehouse.versioning import UnsupportedVersion, compare_versions, version_in_range

logger = logging.getLogger(__name__)

SUPPORTED_RANGE_TYPES = {"SEMVER", "ECOSYSTEM"}
UNSUPPORTED_RANGE_TYPES = {"GIT"}


def _same_version(ecosystem: str, left: str, right: str) -> bool:
    try:
        return compare_versions(ecosystem, left, right) == 0
    except UnsupportedVersion:
        return left.strip() == right.strip()


def evaluate_ranges(
    ecosystem: str, version: str, ranges: Iterable[dict[str, Any]]
) -> tuple[bool, str, dict[str, Any] | None]:
    """Return (affected, certainty, matching range).

    Certainty is "exact_list" when the advisory lists the version explicitly,
    "range" when a comparison range matched, and "uncertain" when at least one
    range could not be evaluated and nothing matched.
    """
    uncertain = False
    uncertain_range: dict[str, Any] | None = None

    for entry in ranges:
        listed = entry.get("versions") or []
        if listed and any(_same_version(ecosystem, version, item) for item in listed):
            return True, "exact_list", entry

        range_type = str(entry.get("range_type") or "").upper()
        if range_type == "VERSIONS":
            # The explicit version list was already checked above.
            continue
        if range_type in UNSUPPORTED_RANGE_TYPES:
            uncertain = True
            uncertain_range = uncertain_range or entry
            continue
        if range_type not in SUPPORTED_RANGE_TYPES:
            uncertain = True
            uncertain_range = uncertain_range or entry
            continue

        result = version_in_range(
            ecosystem,
            version,
            entry.get("introduced"),
            entry.get("fixed"),
            entry.get("last_affected"),
        )
        if result is True:
            return True, "range", entry
        if result is None:
            uncertain = True
            uncertain_range = uncertain_range or entry

    if uncertain:
        return False, "uncertain", uncertain_range
    return False, "range", None


def match_ecosystem(conn: Any, ecosystem: str, package: str | None = None) -> dict[str, int]:
    where = "e.name = %s"
    params: list[Any] = [ecosystem]
    if package:
        where += " AND p.normalized_name = %s"
        params.append(normalize_package_name(package))

    versions = conn.execute(
        f"""
        SELECT v.id, v.version, p.name, p.normalized_name
        FROM package_version v
        JOIN package p ON p.id = v.package_id
        JOIN ecosystem e ON e.id = p.ecosystem_id
        WHERE {where}
        """,
        params,
    ).fetchall()

    ranges = conn.execute(
        """
        SELECT r.id, r.vulnerability_id, r.package_normalized_name, r.range_type,
               r.introduced, r.fixed, r.last_affected, r.versions
        FROM affected_package_range r
        JOIN ecosystem e ON e.id = r.ecosystem_id
        WHERE e.name = %s
        """,
        (ecosystem,),
    ).fetchall()

    by_package: dict[str, list[dict[str, Any]]] = {}
    for row in ranges:
        by_package.setdefault(row["package_normalized_name"], []).append(row)

    with conn.transaction():
        if package:
            conn.execute(
                """
                DELETE FROM version_vulnerability vv
                USING package_version v, package p, ecosystem e
                WHERE vv.package_version_id = v.id AND v.package_id = p.id
                  AND p.ecosystem_id = e.id AND e.name = %s
                  AND p.normalized_name = %s
                """,
                (ecosystem, normalize_package_name(package)),
            )
        else:
            conn.execute(
                """
                DELETE FROM version_vulnerability vv
                USING package_version v, package p, ecosystem e
                WHERE vv.package_version_id = v.id AND v.package_id = p.id
                  AND p.ecosystem_id = e.id AND e.name = %s
                """,
                (ecosystem,),
            )

        counts = {"versions": len(versions), "affected": 0, "uncertain": 0}
        for version_row in versions:
            package_ranges = by_package.get(version_row["normalized_name"]) or []
            if not package_ranges:
                continue

            grouped: dict[int, list[dict[str, Any]]] = {}
            for entry in package_ranges:
                grouped.setdefault(entry["vulnerability_id"], []).append(entry)

            for vulnerability_id, entries in grouped.items():
                affected, certainty, evidence = evaluate_ranges(
                    ecosystem, version_row["version"], entries
                )
                if not affected and certainty != "uncertain":
                    continue
                conn.execute(
                    """
                    INSERT INTO version_vulnerability (
                        package_version_id, vulnerability_id, affected,
                        certainty, affected_range_id
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (package_version_id, vulnerability_id) DO UPDATE SET
                        affected = EXCLUDED.affected,
                        certainty = EXCLUDED.certainty,
                        affected_range_id = EXCLUDED.affected_range_id,
                        computed_at = now()
                    """,
                    (
                        version_row["id"],
                        vulnerability_id,
                        affected,
                        certainty,
                        evidence["id"] if evidence else None,
                    ),
                )
                counts["affected" if affected else "uncertain"] += 1

    logger.info(
        "Matched %d versions for %s: %d affected, %d uncertain",
        counts["versions"],
        ecosystem,
        counts["affected"],
        counts["uncertain"],
    )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Match versions to OSV ranges")
    parser.add_argument("--ecosystem", required=True)
    parser.add_argument("--package", default=None)
    parser.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = connect(args.dsn or get_dsn())
    try:
        print(match_ecosystem(conn, args.ecosystem, args.package))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
