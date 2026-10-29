"""OSV vulnerability ingestion.

OSV is the primary advisory source and the only ecosystem API this project
calls. Package metadata and version data still come from website collection.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable

from warehouse.db import connect, get_dsn, seed_ecosystems
from warehouse.processing import normalize_package_name, parse_timestamp

try:
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - psycopg is a runtime dependency
    Jsonb = json.dumps  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

OSV_API = "https://api.osv.dev/v1"
BATCH_SIZE = 500
USER_AGENT = "hurtownie-warehouse/0.1 (academic vulnerability research)"
CVSS_PREFIXES = ("CVSS_V2", "CVSS_V3", "CVSS_V4")


class OsvError(RuntimeError):
    pass


def _request(url: str, payload: dict[str, Any] | None = None, retries: int = 4) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == retries:
                raise OsvError(f"{url}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise OsvError(f"{url}: {exc}") from exc
        time.sleep(2**attempt)
    raise OsvError(f"{url}: request failed")


def query_batch(ecosystem: str, names: Iterable[str]) -> dict[str, list[str]]:
    names = list(names)
    results: dict[str, list[str]] = {}
    for start in range(0, len(names), BATCH_SIZE):
        chunk = names[start : start + BATCH_SIZE]
        payload = {
            "queries": [
                {"package": {"name": name, "ecosystem": ecosystem}} for name in chunk
            ]
        }
        data = _request(f"{OSV_API}/querybatch", payload)
        for name, result in zip(chunk, data.get("results", [])):
            results[name] = sorted(
                entry["id"] for entry in result.get("vulns", []) if entry.get("id")
            )
        logger.info(
            "OSV package query %d/%d (%d vulnerabilities found)",
            min(start + BATCH_SIZE, len(names)),
            len(names),
            sum(len(ids) for ids in results.values()),
        )
    return results


def fetch_vulnerability(vuln_id: str) -> dict[str, Any]:
    return _request(f"{OSV_API}/vulns/{vuln_id}")


def _severity(record: dict[str, Any]) -> tuple[str | None, str | None]:
    vector = None
    for entry in record.get("severity") or []:
        if str(entry.get("type", "")).startswith(CVSS_PREFIXES) and entry.get("score"):
            vector = entry["score"]
            break

    severity = (record.get("database_specific") or {}).get("severity")
    if isinstance(severity, str):
        severity = severity.upper()
    return severity, vector


def _alias_type(alias: str) -> str:
    if alias.startswith("CVE-"):
        return "cve"
    if alias.startswith("GHSA-"):
        return "ghsa"
    return "alias"


def _range_intervals(events: list[dict[str, str]]) -> list[tuple[str | None, str | None, str | None]]:
    intervals: list[tuple[str | None, str | None, str | None]] = []
    introduced: str | None = None
    started = False

    for event in events:
        if "introduced" in event:
            if started:
                intervals.append((introduced, None, None))
            introduced = event["introduced"]
            started = True
        elif "fixed" in event:
            intervals.append((introduced, event["fixed"], None))
            introduced, started = None, False
        elif "last_affected" in event:
            intervals.append((introduced, None, event["last_affected"]))
            introduced, started = None, False
        elif "limit" in event:
            intervals.append((introduced, event["limit"], None))
            introduced, started = None, False

    if started:
        intervals.append((introduced, None, None))
    return intervals


def _affected_rows(
    record: dict[str, Any], ecosystem: str, package_name: str
) -> list[dict[str, Any]]:
    wanted = normalize_package_name(package_name)
    rows: list[dict[str, Any]] = []

    for affected in record.get("affected") or []:
        package = affected.get("package") or {}
        if package.get("ecosystem") != ecosystem:
            continue
        if normalize_package_name(package.get("name", "")) != wanted:
            continue

        versions = affected.get("versions") or []
        ranges = affected.get("ranges") or []
        if not ranges:
            rows.append(
                {
                    "range_type": "VERSIONS",
                    "introduced": None,
                    "fixed": None,
                    "last_affected": None,
                    "versions": versions or None,
                    "raw_range": affected,
                }
            )
            continue

        for range_entry in ranges:
            range_type = range_entry.get("type") or "ECOSYSTEM"
            intervals = _range_intervals(range_entry.get("events") or [])
            if not intervals and versions:
                rows.append(
                    {
                        "range_type": range_type,
                        "introduced": None,
                        "fixed": None,
                        "last_affected": None,
                        "versions": versions,
                        "raw_range": range_entry,
                    }
                )
            for introduced, fixed, last_affected in intervals:
                rows.append(
                    {
                        "range_type": range_type,
                        "introduced": introduced,
                        "fixed": fixed,
                        "last_affected": last_affected,
                        "versions": versions or None,
                        "raw_range": range_entry,
                    }
                )
    return rows


def _store(
    conn: Any,
    record: dict[str, Any],
    ecosystem: str,
    package_name: str,
) -> None:
    severity, vector = _severity(record)
    row = conn.execute(
        """
        INSERT INTO vulnerability (
            source, source_id, summary, details, severity, cvss_vector,
            published_at, modified_at, withdrawn_at
        )
        VALUES ('OSV', %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source, source_id) DO UPDATE SET
            summary = EXCLUDED.summary,
            details = EXCLUDED.details,
            severity = EXCLUDED.severity,
            cvss_vector = EXCLUDED.cvss_vector,
            published_at = EXCLUDED.published_at,
            modified_at = EXCLUDED.modified_at,
            withdrawn_at = EXCLUDED.withdrawn_at,
            last_seen_at = now()
        RETURNING id
        """,
        (
            record["id"],
            record.get("summary"),
            record.get("details"),
            severity,
            vector,
            parse_timestamp(record.get("published")),
            parse_timestamp(record.get("modified")),
            parse_timestamp(record.get("withdrawn")),
        ),
    ).fetchone()
    vulnerability_id = row["id"]

    conn.execute(
        "DELETE FROM vulnerability_alias WHERE vulnerability_id = %s",
        (vulnerability_id,),
    )
    for alias in record.get("aliases") or []:
        conn.execute(
            """
            INSERT INTO vulnerability_alias (vulnerability_id, alias, alias_type)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """,
            (vulnerability_id, alias, _alias_type(alias)),
        )

    conn.execute(
        "DELETE FROM vulnerability_reference WHERE vulnerability_id = %s",
        (vulnerability_id,),
    )
    for reference in record.get("references") or []:
        url = reference.get("url")
        if url:
            conn.execute(
                """
                INSERT INTO vulnerability_reference
                    (vulnerability_id, url, reference_type)
                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                """,
                (vulnerability_id, url, reference.get("type")),
            )

    ecosystem_row = conn.execute(
        "SELECT id FROM ecosystem WHERE name = %s", (ecosystem,)
    ).fetchone()
    if ecosystem_row is None:
        raise ValueError(f"unknown ecosystem: {ecosystem!r}")

    conn.execute(
        "DELETE FROM affected_package_range WHERE vulnerability_id = %s AND package_normalized_name = %s",
        (vulnerability_id, normalize_package_name(package_name)),
    )

    for affected in _affected_rows(record, ecosystem, package_name):
        conn.execute(
            """
            INSERT INTO affected_package_range (
                vulnerability_id, ecosystem_id, package_name,
                package_normalized_name, range_type, introduced, fixed,
                last_affected, versions, source, raw_range
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'OSV', %s)
            """,
            (
                vulnerability_id,
                ecosystem_row["id"],
                package_name,
                normalize_package_name(package_name),
                affected["range_type"],
                affected["introduced"],
                affected["fixed"],
                affected["last_affected"],
                Jsonb(affected["versions"]) if affected["versions"] else None,
                Jsonb(affected["raw_range"]),
            ),
        )


def ingest_packages(
    conn: Any,
    ecosystem: str,
    packages: list[str],
    delay: float = 0.1,
    max_vulnerabilities: int | None = None,
) -> dict[str, int]:
    seed_ecosystems(conn)
    run = conn.execute(
        """
        INSERT INTO collection_run (source, spider, status, notes)
        VALUES ('OSV', 'osv_ingest', 'running', %s) RETURNING id
        """,
        (f"ecosystem={ecosystem}, packages={len(packages)}",),
    ).fetchone()
    run_id = run["id"]
    conn.commit()

    errors: list[tuple[str, str, str]] = []
    stored = 0
    unique_ids: list[str] = []
    try:
        matches = query_batch(ecosystem, packages)
        vulnerable_packages = {name: ids for name, ids in matches.items() if ids}
        unique_ids = sorted({vuln_id for ids in vulnerable_packages.values() for vuln_id in ids})
        if max_vulnerabilities is not None:
            unique_ids = unique_ids[:max_vulnerabilities]

        for index, vuln_id in enumerate(unique_ids, start=1):
            try:
                record = fetch_vulnerability(vuln_id)
            except OsvError as exc:
                errors.append((vuln_id, "OSV", str(exc)))
                logger.warning("Could not fetch %s: %s", vuln_id, exc)
                continue

            owners = [
                name
                for name, ids in vulnerable_packages.items()
                if vuln_id in ids
            ] or [""]
            for name in owners:
                with conn.transaction():
                    _store(conn, record, ecosystem, name)
            stored += 1
            if delay:
                time.sleep(delay)

            if index % 50 == 0:
                logger.info("Stored %d/%d OSV records", index, len(unique_ids))
    except Exception:
        with conn.transaction():
            conn.execute(
                """
                UPDATE collection_run
                SET finished_at = now(), status = 'failed', items_stored = %s,
                    errors = %s
                WHERE id = %s
                """,
                (stored, len(errors) + 1, run_id),
            )
        raise

    with conn.transaction():
        if errors:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO collection_error
                        (collection_run_id, url, stage, error_type, message, occurred_at)
                    VALUES (%s, %s, 'osv_fetch', 'OsvError', %s, %s)
                    """,
                    [
                        (
                            run_id,
                            f"{OSV_API}/vulns/{vuln_id}",
                            message,
                            datetime.now(timezone.utc),
                        )
                        for vuln_id, _stage, message in errors
                    ],
                )
        conn.execute(
            """
            UPDATE collection_run
            SET finished_at = now(), status = %s, items_scraped = %s,
                items_stored = %s, errors = %s
            WHERE id = %s
            """,
            (
                "partial" if errors else "succeeded",
                len(unique_ids),
                stored,
                len(errors),
                run_id,
            ),
        )
    logger.info(
        "OSV ingestion stored %d advisories for %d packages (%d errors)",
        stored,
        len(packages),
        len(errors),
    )
    return {"advisories": stored, "packages": len(packages), "errors": len(errors)}


def sampled_packages(conn: Any, ecosystem: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT p.name
        FROM package p JOIN ecosystem e ON e.id = p.ecosystem_id
        WHERE e.name = %s
        ORDER BY p.sampling_category, p.sampling_rank NULLS LAST, p.name
        """,
        (ecosystem,),
    ).fetchall()
    return [row["name"] for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest OSV vulnerability data")
    parser.add_argument("--ecosystem", required=True)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--limit", type=int, default=None, help="max packages")
    parser.add_argument(
        "--max-vulnerabilities", type=int, default=None, help="stop after N advisories"
    )
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--package", action="append", dest="packages")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = connect(args.dsn or get_dsn())
    try:
        packages = args.packages or sampled_packages(conn, args.ecosystem)
        if args.limit:
            packages = packages[: args.limit]
        if not packages:
            print("no packages found; run a collector first")
            return 1
        result = ingest_packages(
            conn,
            args.ecosystem,
            packages,
            delay=args.delay,
            max_vulnerabilities=args.max_vulnerabilities,
        )
        print(result)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
