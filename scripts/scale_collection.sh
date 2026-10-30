#!/usr/bin/env bash
#
# Moderate scale-up run: several hundred packages per ecosystem, then OSV
# ingestion, vulnerability matching and the analysis report.
#
# The run is deliberately sequential: every Selenium ecosystem shares one
# browser pool, and hammering seven sites at once would make blocking results
# impossible to interpret. Each step appends to its own log and to summary.tsv,
# so a timeout or a blocked ecosystem does not discard the rest of the run.
#
# Usage:
#   scripts/scale_collection.sh
#
# Environment:
#   WAREHOUSE_DSN      PostgreSQL DSN (default: local warehouse on port 55432)
#   SCALE_RUN_DIR      output directory (default: output/scale-<timestamp>)
#   SCALE_ECOSYSTEMS   space-separated subset to run (default: all seven);
#                      useful for resuming after an interrupted run
#   SCALE_TIMEOUT_HTTP seconds per HTTP-only ecosystem (default: 10800 = 3 h)
#   SCALE_TIMEOUT_BROWSER seconds per Selenium ecosystem (default: 21600 = 6 h)
#   SCALE_MAX_VERSIONS versions sampled per package (default: 5)

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DSN="${WAREHOUSE_DSN:-postgresql://warehouse:warehouse@localhost:55432/warehouse}"
RUN_DIR="${SCALE_RUN_DIR:-output/scale-$(date +%Y%m%d-%H%M%S)}"
TIMEOUT_HTTP="${SCALE_TIMEOUT_HTTP:-10800}"
TIMEOUT_BROWSER="${SCALE_TIMEOUT_BROWSER:-21600}"
MAX_VERSIONS="${SCALE_MAX_VERSIONS:-5}"
PYTHON="$ROOT/.venv/bin/python"
SCRAPY="$ROOT/.venv/bin/scrapy"

mkdir -p "$RUN_DIR"
SUMMARY="$RUN_DIR/summary.tsv"
SELECTED="${SCALE_ECOSYSTEMS:-nuget pypi packagist hex rubygems npm crates}"

log() {
    printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$RUN_DIR/driver.log"
}

should_run() {
    case " $SELECTED " in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

if [ ! -x "$PYTHON" ] || [ ! -x "$SCRAPY" ]; then
    log "missing .venv; run: uv venv .venv && uv pip install -e '.[dev]'"
    exit 2
fi

log "run directory: $RUN_DIR"
log "dsn: $DSN"
log "max versions per package: $MAX_VERSIONS"

# Make the schema idempotent so a fresh machine can run this unattended.
if ! env WAREHOUSE_DSN="$DSN" "$PYTHON" -m warehouse.db init >> "$RUN_DIR/db-init.log" 2>&1; then
    log "database init failed; see $RUN_DIR/db-init.log"
    exit 2
fi

run_osv_and_matching() {
    local spider="$1" ecosystem="$2"

    if timeout 1800 env WAREHOUSE_DSN="$DSN" "$PYTHON" -m warehouse.osv \
        --ecosystem "$ecosystem" --delay 0.1 >> "$RUN_DIR/osv-$spider.log" 2>&1; then
        log "$spider OSV ingestion ok"
    else
        log "$spider OSV ingestion exit=$?"
    fi

    if timeout 900 env WAREHOUSE_DSN="$DSN" "$PYTHON" -m warehouse.matching \
        --ecosystem "$ecosystem" >> "$RUN_DIR/matching-$spider.log" 2>&1; then
        log "$spider matching ok"
    else
        log "$spider matching exit=$?"
    fi
}

run_spider() {
    local spider="$1" ecosystem="$2" timeout_s="$3"
    shift 3

    local started finished code status timeout_pid monitor_pid
    started="$(date -Is)"
    log "$spider start (timeout ${timeout_s}s)"

    timeout --signal=TERM --kill-after=120 "$timeout_s" \
        env WAREHOUSE_DSN="$DSN" SCRAPY_LOG_LEVEL=INFO PYTHONUNBUFFERED=1 "$@" \
        "$SCRAPY" crawl "$spider" >> "$RUN_DIR/$spider.log" 2>&1 &
    timeout_pid=$!

    # Scrapy writes its own progress to the spider log, but the orchestrator log
    # would otherwise stay silent until the whole ecosystem finishes. Mirror the
    # latest logstats line once a minute so a long run is visibly alive.
    (
        while kill -0 "$timeout_pid" 2>/dev/null; do
            sleep 60
            if [ -f "$RUN_DIR/$spider.log" ]; then
                line="$(grep -E 'Crawled [0-9]+ pages' "$RUN_DIR/$spider.log" | tail -1 || true)"
                if [ -n "$line" ]; then
                    printf '%s %s progress: %s\n' "$(date -Is)" "$spider" "$line" \
                        >> "$RUN_DIR/driver.log"
                fi
            fi
        done
    ) &
    monitor_pid=$!

    wait "$timeout_pid"
    code=$?
    kill "$monitor_pid" 2>/dev/null || true
    wait "$monitor_pid" 2>/dev/null || true
    finished="$(date -Is)"

    case "$code" in
        0) status="succeeded" ;;
        124|137) status="timeout" ;;
        *) status="failed" ;;
    esac

    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$started" "$finished" "$spider" "$ecosystem" "$status" "$code" >> "$SUMMARY"
    log "$spider finished status=$status exit=$code"

    # Partial runs still stored packages, and both commands are idempotent.
    run_osv_and_matching "$spider" "$ecosystem"
}

if should_run nuget; then
    log "=== NuGet"
    run_spider nuget NuGet "$TIMEOUT_HTTP" \
        NUGET_POPULAR_COUNT=250 NUGET_RANDOM_COUNT=50 NUGET_RANDOM_PAGES=10 \
        NUGET_MAX_VERSIONS="$MAX_VERSIONS"
fi

if should_run pypi; then
    log "=== PyPI"
    run_spider pypi PyPI "$TIMEOUT_HTTP" \
        PYPI_POPULAR_COUNT=250 PYPI_RANDOM_COUNT=50 PYPI_SITEMAP_SHARDS=10 \
        PYPI_MAX_VERSIONS="$MAX_VERSIONS"
fi

if should_run packagist; then
    log "=== Packagist"
    run_spider packagist Packagist "$TIMEOUT_BROWSER" \
        BROWSER_HEADLESS=true \
        PACKAGIST_POPULAR_COUNT=250 PACKAGIST_RANDOM_COUNT=50 \
        PACKAGIST_MAX_VERSIONS="$MAX_VERSIONS"
fi

if should_run hex; then
    log "=== Hex"
    run_spider hex Hex "$TIMEOUT_BROWSER" \
        BROWSER_HEADLESS=true \
        HEX_POPULAR_COUNT=250 HEX_RANDOM_COUNT=50 HEX_RANDOM_PAGES=10 \
        HEX_MAX_VERSIONS="$MAX_VERSIONS"
fi

if should_run rubygems; then
    log "=== RubyGems (stats pages cap the popular group at 100)"
    run_spider rubygems RubyGems "$TIMEOUT_BROWSER" \
        BROWSER_HEADLESS=true \
        RUBYGEMS_POPULAR_COUNT=100 RUBYGEMS_RANDOM_COUNT=50 RUBYGEMS_RANDOM_PAGES=10 \
        RUBYGEMS_MAX_VERSIONS="$MAX_VERSIONS"
fi

if should_run npm; then
    log "=== npm (headed Chrome for Cloudflare)"
    run_spider npm npm "$TIMEOUT_BROWSER" \
        BROWSER_HEADLESS=false \
        BROWSER_PROFILE_DIR="${BROWSER_PROFILE_DIR:-/tmp/opencode/chrome-npm-profile}" \
        NPM_POPULAR_COUNT=250 NPM_RANDOM_COUNT=50 \
        NPM_SEARCH_QUERIES="framework,http,cli,testing,react,vue,express,webpack,typescript,build,api,ui" \
        NPM_MAX_VERSIONS="$MAX_VERSIONS"
fi

if should_run crates; then
    log "=== crates.io (Selenium, slowest, runs last)"
    run_spider crates crates.io "$TIMEOUT_BROWSER" \
        BROWSER_HEADLESS=true \
        CRATES_POPULAR_COUNT=250 CRATES_RANDOM_COUNT=50 CRATES_RANDOM_PAGES=10 \
        CRATES_VERSION_PAGES=1 CRATES_MAX_VERSIONS="$MAX_VERSIONS"
fi

log "=== analysis"
env WAREHOUSE_DSN="$DSN" "$PYTHON" analysis/run_queries.py --limit 50 \
    > "$RUN_DIR/queries.txt" 2> "$RUN_DIR/queries.err"
log "queries exit=$?"

env WAREHOUSE_DSN="$DSN" "$PYTHON" analysis/scale_report.py \
    --summary "$SUMMARY" --output "$RUN_DIR/report.md" \
    > "$RUN_DIR/report.log" 2>&1
log "report exit=$?"

env WAREHOUSE_DSN="$DSN" "$PYTHON" analysis/build_dashboard.py \
    --output "$RUN_DIR/dashboard.html" > "$RUN_DIR/dashboard.log" 2>&1
log "dashboard exit=$?"

log "done: $RUN_DIR"
