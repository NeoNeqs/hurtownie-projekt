"""Run the analytical queries in analysis/queries.sql.

Each query is separated by a "-- name: ..." marker so results can be selected
by name and printed with a stable heading.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from warehouse.db import connect, get_dsn

QUERIES_FILE = Path(__file__).with_name("queries.sql")
MARKER = re.compile(r"^-- name: (\S+)\s*$", re.MULTILINE)


def load_queries(path: Path = QUERIES_FILE) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    matches = list(MARKER.finditer(text))
    queries: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end]
        if ";" in body:
            body = body[: body.rindex(";")]
        queries[match.group(1)] = body.strip()
    return queries


def print_rows(rows: list[dict], limit: int = 20) -> None:
    if not rows:
        print("  (no rows)")
        return
    columns = list(rows[0])
    widths = {
        column: max(
            len(column),
            *(len(str(row.get(column))) for row in rows[:limit]),
        )
        for column in columns
    }
    print("  " + "  ".join(column.ljust(widths[column]) for column in columns))
    for row in rows[:limit]:
        print(
            "  "
            + "  ".join(str(row.get(column)).ljust(widths[column]) for column in columns)
        )
    if len(rows) > limit:
        print(f"  ... {len(rows) - limit} more rows")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run warehouse analysis queries")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--query", action="append", dest="names")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    queries = load_queries()
    selected = args.names or list(queries)
    unknown = [name for name in selected if name not in queries]
    if unknown:
        print(f"unknown query: {', '.join(unknown)}", file=sys.stderr)
        return 2

    conn = connect(args.dsn or get_dsn())
    try:
        for name in selected:
            print(f"\n== {name}")
            rows = conn.execute(queries[name]).fetchall()
            print_rows(rows, args.limit)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
