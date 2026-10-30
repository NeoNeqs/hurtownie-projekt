"""Render a Markdown report for a scale collection run.

The report is intentionally built from the same named queries as the dashboard,
so every number can be traced back to ``analysis/queries.sql``. It also folds in
the orchestrator's ``summary.tsv`` when one is supplied.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from warehouse.db import connect, get_dsn

sys.path.insert(0, str(Path(__file__).parent))
from build_dashboard import DESCRIPTIONS, TITLES  # noqa: E402
from run_queries import load_queries  # noqa: E402

SECTIONS = (
    "sample_overview",
    "data_completeness",
    "collection_freshness",
    "collection_errors",
    "packages_with_vulnerability",
    "vulnerability_counts_and_severity",
    "popular_vs_random",
    "latest_versions_affected",
)


def format_cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def markdown_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_(no rows)_"
    columns = list(rows[0])
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        lines.append(
            "| " + " | ".join(format_cell(row.get(column)) for column in columns) + " |"
        )
    return "\n".join(lines)


def summary_table(path: Path) -> str:
    if not path.exists():
        return "_(no summary.tsv)_"
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        started, finished, spider, ecosystem, status, exit_code = line.split("\t")
        rows.append(
            {
                "spider": spider,
                "ecosystem": ecosystem,
                "status": status,
                "exit": exit_code,
                "started": started,
                "finished": finished,
            }
        )
    return markdown_table(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a Markdown scale report")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=None)
    args = parser.parse_args(argv)

    queries = load_queries()
    conn = connect(args.dsn or get_dsn())
    try:
        lines = [
            "# Scale collection report",
            "",
            f"Generated {datetime.now().isoformat(sep=' ', timespec='seconds')}.",
            "",
            "## Orchestrator runs",
            "",
            summary_table(args.summary) if args.summary else "_(no summary supplied)_",
            "",
        ]
        for name in SECTIONS:
            lines.extend(
                [
                    f"## {TITLES.get(name, name)}",
                    "",
                    DESCRIPTIONS.get(name, ""),
                    "",
                    markdown_table(conn.execute(queries[name]).fetchall()),
                    "",
                ]
            )
    finally:
        conn.close()

    text = "\n".join(lines).rstrip() + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
