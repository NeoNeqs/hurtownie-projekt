#!/usr/bin/env bash
#
# Show what a scale run is doing without tailing every spider log.
#
# Usage:
#   scripts/scale_status.sh [output/scale-YYYYmmdd-HHMMSS]
#
# With no argument the newest output/scale-* directory is used.

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUN_DIR="${1:-$(ls -dt "$ROOT"/output/scale-* 2>/dev/null | head -1)}"
if [ -z "${RUN_DIR:-}" ] || [ ! -d "$RUN_DIR" ]; then
    echo "no scale run directory found" >&2
    exit 1
fi

DSN="${WAREHOUSE_DSN:-postgresql://warehouse:warehouse@localhost:55432/warehouse}"
PYTHON="$ROOT/.venv/bin/python"

echo "run: $RUN_DIR"
echo

if [ -f "$RUN_DIR/summary.tsv" ]; then
    echo "finished spiders (start, finish, spider, ecosystem, status, exit):"
    column -t -s $'\t' "$RUN_DIR/summary.tsv"
    echo
fi

echo "latest Scrapy progress:"
found=0
for log in "$RUN_DIR"/*.log; do
    [ -f "$log" ] || continue
    name="$(basename "$log" .log)"
    case "$name" in
        driver|db-init|queries|report|dashboard|osv-*|matching-*) continue ;;
    esac
    line="$(grep -E 'Crawled [0-9]+ pages' "$log" | tail -1 || true)"
    if [ -n "$line" ]; then
        printf '  %-10s %s\n' "$name" "$line"
        found=1
    fi
done
if [ "$found" -eq 0 ]; then
    echo "  (no logstats yet)"
fi
echo

echo "active spider processes:"
active="$(pgrep -af 'scrapy crawl' || true)"
if [ -n "$active" ]; then
    echo "$active" | sed 's/^/  /'
else
    echo "  (none)"
fi
echo

if [ ! -x "$PYTHON" ]; then
    echo "missing .venv; cannot query the database" >&2
    exit 1
fi

env WAREHOUSE_DSN="$DSN" "$PYTHON" - <<'PY'
from warehouse.db import connect

with connect() as conn:
    print("database totals by ecosystem:")
    rows = conn.execute(
        """
        SELECT e.name AS ecosystem,
               (SELECT count(*) FROM package p WHERE p.ecosystem_id = e.id) AS packages,
               (SELECT count(*)
                  FROM package_version v
                  JOIN package p ON p.id = v.package_id
                 WHERE p.ecosystem_id = e.id) AS versions,
               (SELECT count(*)
                  FROM dependency d
                  JOIN package_version v ON v.id = d.package_version_id
                  JOIN package p ON p.id = v.package_id
                 WHERE p.ecosystem_id = e.id) AS dependencies
          FROM ecosystem e
         ORDER BY e.name
        """
    ).fetchall()
    if rows:
        columns = list(rows[0])
        widths = {
            column: max(len(column), *(len(str(row[column])) for row in rows))
            for column in columns
        }
        print("  " + "  ".join(c.ljust(widths[c]) for c in columns))
        for row in rows:
            print("  " + "  ".join(str(row[c]).ljust(widths[c]) for c in columns))

    print()
    print("recent collection runs:")
    runs = conn.execute(
        """
        SELECT id, spider, status, items_stored, errors, started_at, finished_at
          FROM collection_run
         ORDER BY id DESC
         LIMIT 10
        """
    ).fetchall()
    for run in runs:
        print(
            f"  #{run['id']:<3} {run['spider']:<14} {run['status']:<10} "
            f"stored={run['items_stored']:<4} errors={run['errors']:<4} "
            f"started={run['started_at']:%Y-%m-%d %H:%M} "
            f"finished={run['finished_at']:%Y-%m-%d %H:%M}"
            if run["finished_at"]
            else f"  #{run['id']:<3} {run['spider']:<14} {run['status']:<10} "
            f"stored={run['items_stored']:<4} errors={run['errors']:<4} "
            f"started={run['started_at']:%Y-%m-%d %H:%M} finished=-"
        )
PY
