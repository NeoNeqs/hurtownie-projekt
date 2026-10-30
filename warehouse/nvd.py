"""NVD enrichment for advisories that OSV imports without CVSS data.

OSV is the primary advisory source. NVD is queried only by the CVE ids that
OSV already lists as aliases, and only to fill severity and CVSS fields.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from warehouse.db import connect, get_dsn

logger = logging.getLogger(__name__)

NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
USER_AGENT = "hurtownie-warehouse/0.1 (academic vulnerability research)"
METRIC_KEYS = (
    "cvssMetricV40",
    "cvssMetricV31",
    "cvssMetricV30",
    "cvssMetricV2",
)


class NvdError(RuntimeError):
    pass


def _request(cve_id: str, api_key: str | None, retries: int = 4) -> dict[str, Any]:
    url = f"{NVD_API}?cveId={cve_id}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if api_key:
        headers["apiKey"] = api_key

    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            if exc.code not in {403, 429, 500, 502, 503, 504} or attempt == retries:
                raise NvdError(f"{cve_id}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise NvdError(f"{cve_id}: {exc}") from exc
        time.sleep(2**attempt)
    raise NvdError(f"{cve_id}: request failed")


def parse_metric(payload: dict[str, Any]) -> dict[str, Any] | None:
    vulnerabilities = payload.get("vulnerabilities") or []
    if not vulnerabilities:
        return None
    cve = vulnerabilities[0].get("cve") or {}
    metrics = cve.get("metrics") or {}

    for key in METRIC_KEYS:
        entries = metrics.get(key) or []
        if not entries:
            continue
        entry = entries[0]
        data = entry.get("cvssData") or {}
        score = data.get("baseScore")
        if score is None:
            continue
        return {
            "severity": data.get("baseSeverity") or entry.get("baseSeverity"),
            "cvss_score": score,
            "cvss_vector": data.get("vectorString"),
            "published_at": cve.get("published"),
            "modified_at": cve.get("lastModified"),
        }
    return None


def _candidates(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT v.id, v.severity, v.cvss_score, a.alias
        FROM vulnerability v
        JOIN vulnerability_alias a ON a.vulnerability_id = v.id
        WHERE v.source = 'OSV' AND a.alias LIKE 'CVE-%'
          AND (v.severity IS NULL OR v.cvss_score IS NULL)
        ORDER BY v.id
        """
    ).fetchall()
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = grouped.setdefault(
            row["id"],
            {
                "id": row["id"],
                "severity": row["severity"],
                "cvss_score": row["cvss_score"],
                "aliases": [],
            },
        )
        entry["aliases"].append(row["alias"])
    return list(grouped.values())


def enrich(
    conn: Any,
    limit: int | None = None,
    delay: float = 6.0,
    api_key: str | None = None,
) -> dict[str, int]:
    run = conn.execute(
        """
        INSERT INTO collection_run (source, spider, status, notes)
        VALUES ('NVD', 'nvd_enrich', 'running', %s) RETURNING id
        """,
        (f"limit={limit}, delay={delay}",),
    ).fetchone()
    run_id = run["id"]
    conn.commit()

    candidates = _candidates(conn)
    if limit is not None:
        candidates = candidates[:limit]

    updated = 0
    missing = 0
    errors: list[tuple[str, str]] = []
    try:
        for index, candidate in enumerate(candidates, start=1):
            cve_id = sorted(candidate["aliases"])[0]
            try:
                payload = _request(cve_id, api_key)
            except NvdError as exc:
                errors.append((cve_id, str(exc)))
                logger.warning("NVD lookup failed: %s", exc)
                continue

            metric = parse_metric(payload)
            if metric is None:
                missing += 1
            else:
                conn.execute(
                    """
                    UPDATE vulnerability
                    SET severity = COALESCE(severity, %s),
                        cvss_score = %s,
                        cvss_vector = COALESCE(%s, cvss_vector),
                        published_at = COALESCE(published_at, %s),
                        modified_at = COALESCE(%s, modified_at),
                        last_seen_at = now()
                    WHERE id = %s
                    """,
                    (
                        metric["severity"],
                        metric["cvss_score"],
                        metric["cvss_vector"],
                        _parse_timestamp(metric["published_at"]),
                        _parse_timestamp(metric["modified_at"]),
                        candidate["id"],
                    ),
                )
                conn.commit()
                updated += 1

            if index % 50 == 0:
                logger.info("NVD enrichment %d/%d", index, len(candidates))
            if index < len(candidates) and delay:
                time.sleep(delay)
    except Exception:
        _finish_run(conn, run_id, "failed", updated, len(errors))
        raise

    _finish_run(conn, run_id, "partial" if errors else "succeeded", updated, len(errors))
    if errors:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO collection_error
                        (collection_run_id, url, stage, error_type, message, occurred_at)
                    VALUES (%s, %s, 'nvd_fetch', 'NvdError', %s, %s)
                    """,
                    [
                        (
                            run_id,
                            f"{NVD_API}?cveId={cve_id}",
                            message,
                            datetime.now(timezone.utc),
                        )
                        for cve_id, message in errors
                    ],
                )
    logger.info(
        "NVD enriched %d advisories (%d without metrics, %d errors)",
        updated,
        missing,
        len(errors),
    )
    return {
        "candidates": len(candidates),
        "updated": updated,
        "missing": missing,
        "errors": len(errors),
    }


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _finish_run(
    conn: Any, run_id: int, status: str, updated: int, errors: int
) -> None:
    with conn.transaction():
        conn.execute(
            """
            UPDATE collection_run
            SET finished_at = now(), status = %s, items_scraped = %s,
                items_stored = %s, errors = %s
            WHERE id = %s
            """,
            (status, updated, updated, errors, run_id),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enrich OSV advisories with NVD CVSS data")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=6.0)
    parser.add_argument(
        "--api-key", default=os.getenv("NVD_API_KEY"), help="NVD API key (optional)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = connect(args.dsn or get_dsn())
    try:
        print(enrich(conn, limit=args.limit, delay=args.delay, api_key=args.api_key))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
