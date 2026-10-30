# Cross-ecosystem package security warehouse

A PostgreSQL warehouse for package metadata, version-specific dependencies and
known vulnerabilities across package ecosystems. Collectors use Scrapy with a
shared Selenium browser layer, so every package, version and dependency record
comes from a registry website. OSV supplies vulnerability records, and a
version-aware matcher maps affected ranges onto the versions that were
collected.

Implemented ecosystems: **npm**, **PyPI**, **crates.io**, **RubyGems**, **NuGet**,
**Packagist**, **Hex**.

## Layout

| Path | Purpose |
| --- | --- |
| `warehouse/browser.py` | Selenium session, browser pool, challenge detection, cleanup |
| `warehouse/middlewares.py` | Selenium downloader middleware and challenge handling |
| `warehouse/spiders/base.py` | Shared spider request/error helpers |
| `warehouse/spiders/npm.py` | npm discovery, metadata, versions, dependencies |
| `warehouse/spiders/pypi.py` | PyPI discovery, metadata, release history |
| `warehouse/spiders/rubygems.py` | RubyGems discovery, metadata, versions, dependencies |
| `warehouse/spiders/crates.py` | crates.io SPA discovery, metadata, versions, dependencies |
| `warehouse/spiders/hex.py` | Hex discovery, metadata, versions, dependencies |
| `warehouse/spiders/packagist.py` | Packagist discovery, metadata, versions, dependencies |
| `warehouse/spiders/nuget.py` | NuGet discovery, metadata, versions, dependencies |
| `warehouse/sampling.py` | Popular/random selection and release-history sampling |
| `warehouse/osv.py` | OSV ingestion (advisories, aliases, references, ranges) |
| `warehouse/nvd.py` | NVD CVSS/severity enrichment for OSV advisories |
| `warehouse/versioning.py` | SemVer, PEP 440 and RubyGems version ordering |
| `warehouse/matching.py` | Version-to-range matching and persistence |
| `warehouse/pipelines.py` | Validation, deduplication and storage |
| `warehouse/migrations/` | Repeatable schema migrations |
| `analysis/queries.sql` | Analytical SQL with documented denominators |
| `analysis/run_queries.py` | Executes the analytical queries |
| `analysis/build_dashboard.py` | Renders the queries as a static HTML dashboard |

## Setup

```bash
docker compose up -d
python -m venv .venv
.venv/bin/pip install -e ".[dev]"

# The application reads environment variables; it does not load .env itself.
set -a; source .env; set +a
.venv/bin/python -m warehouse.db init
```

The database defaults to `postgresql://warehouse:warehouse@localhost:55432/warehouse`.

`BROWSER_PROFILE_DIR` is worth setting to a persistent directory. The npm
collector needs the browser profile that has already passed the site's bot
check; a fresh profile is more likely to be challenged. `BROWSER_HEADLESS=false`
is the default because headless Chrome is easier to fingerprint.

## Running a collection

```bash
# npm pilot: sample 25 popular and 5 random packages, 10 versions each
.venv/bin/scrapy crawl npm

# smaller pilot
NPM_POPULAR_COUNT=4 NPM_RANDOM_COUNT=2 NPM_MAX_VERSIONS=3 \
  .venv/bin/scrapy crawl npm

# PyPI: popular list + random sitemap shards
PYPI_POPULAR_COUNT=4 PYPI_RANDOM_COUNT=2 PYPI_MAX_VERSIONS=3 \
  .venv/bin/scrapy crawl pypi

# RubyGems: stats page + random index pages
RUBYGEMS_POPULAR_COUNT=4 RUBYGEMS_RANDOM_COUNT=2 RUBYGEMS_MAX_VERSIONS=3 \
  .venv/bin/scrapy crawl rubygems

# Hex: total-download ranking + random alphabetical index pages
HEX_POPULAR_COUNT=4 HEX_RANDOM_COUNT=2 HEX_MAX_VERSIONS=3 \
  .venv/bin/scrapy crawl hex

# Packagist: explore popular pages + seeded names from the package list
PACKAGIST_POPULAR_COUNT=4 PACKAGIST_RANDOM_COUNT=2 PACKAGIST_MAX_VERSIONS=3 \
  .venv/bin/scrapy crawl packagist

# NuGet: downloads ranking + random search pages; the full version table comes
# from the gallery's load-more endpoint
NUGET_POPULAR_COUNT=4 NUGET_RANDOM_COUNT=2 NUGET_MAX_VERSIONS=3 \
  .venv/bin/scrapy crawl nuget

# crates.io: downloads ranking + alphabetical pages, rendered by Selenium
CRATES_POPULAR_COUNT=4 CRATES_RANDOM_COUNT=2 CRATES_MAX_VERSIONS=3 \
  .venv/bin/scrapy crawl crates
```

Every run creates a `collection_run` row and records failures in
`collection_error`. A run left behind by a killed process is marked `partial`
on the next start.

## Scale run

`scripts/scale_collection.sh` runs a moderate sample across all seven
ecosystems, then ingests OSV, matches versions, and writes a Markdown report and
dashboard:

```bash
scripts/scale_collection.sh
# output/scale-<timestamp>/report.md
# output/scale-<timestamp>/dashboard.html
# output/scale-<timestamp>/summary.tsv  (per-spider status and exit code)
scripts/scale_status.sh                # progress of the newest run
```

Defaults: 250 popular + 50 random packages per ecosystem, 5 sampled versions per
package, sequential execution, and a per-ecosystem timeout (3 h HTTP, 6 h
Selenium). Override the sample size with the usual `*_POPULAR_COUNT` /
`*_RANDOM_COUNT` variables, or the timeouts with `SCALE_TIMEOUT_HTTP` /
`SCALE_TIMEOUT_BROWSER`. RubyGems is the exception: its stats pages expose only
the top 100 gems, so the script requests 100 popular + 50 random there.

The orchestrator is deliberately sequential. All Selenium ecosystems share one
browser pool, and running them in parallel would mix rate-limit and challenge
failures, making the blocking measurements meaningless. A failed or timed-out
spider does not stop the run; its partial data is still processed and the exit
code is recorded in `summary.tsv`.

To resume after an interruption without repeating finished ecosystems, point at
the same output directory and list what is left:

```bash
SCALE_RUN_DIR=output/scale-20261030-122225 \
SCALE_ECOSYSTEMS="pypi packagist hex rubygems npm crates" \
  scripts/scale_collection.sh
```

The analysis, report and dashboard at the end always cover the whole database,
not just the ecosystems selected for that invocation.

Version selection defaults to `spread`: the sampler takes the oldest, newest and
evenly spaced releases. Taking only the most downloaded versions hides exactly
the old versions advisories target. `NPM_VERSION_SELECTION=downloads` restores
the old behaviour for comparison; the same setting exists per ecosystem.

## Vulnerabilities

```bash
.venv/bin/python -m warehouse.osv --ecosystem npm --delay 0.1
.venv/bin/python -m warehouse.matching --ecosystem npm
.venv/bin/python -m warehouse.nvd --limit 100   # optional CVSS/severity fill
```

Repeat with `PyPI`, `crates.io`, `RubyGems`, `NuGet`, `Hex` and `Packagist`.
Ingestion is idempotent: advisories are upserted by source id, and ranges are
replaced per advisory and package. Matching deletes and recomputes results for
the ecosystem, so reruns are safe.

NVD is used only as a supplement: the enricher looks up CVE aliases that OSV
already recorded and fills missing severity and CVSS fields. It never adds
advisories on its own. Without `NVD_API_KEY` the client waits 6 seconds between
requests, which is still slow for a large backlog.

Unsupported range formats (for example `GIT`) are stored with
`certainty = 'uncertain'` instead of being guessed; a missing advisory is never
treated as proof that a package is safe.

## Analysis

```bash
.venv/bin/python analysis/run_queries.py
.venv/bin/python analysis/run_queries.py --query packages_with_vulnerability

.venv/bin/python analysis/build_dashboard.py          # output/dashboard.html
.venv/bin/python analysis/build_dashboard.py --output /tmp/report.html
```

Each query starts from the latest successful run per ecosystem and defines its
denominator. Results are based on collected versions only, and dependencies are
declared constraints rather than a resolved install graph.

The dashboard is a single self-contained HTML file. Every section is one named
query, and the page includes row filters, sample sizes, vulnerability shares,
severity mix, dependency exposure, field completeness and collection freshness.
It needs no server; open `output/dashboard.html` in a browser. Rebuild it after
new collections so the figures match the current database.

## Data dictionary

| Table | Important fields |
| --- | --- |
| `ecosystem` | `name`, `homepage_url`, `registry_url` |
| `package` | `ecosystem_id`, `name`, `normalized_name`, `latest_version`, `first_published_at`, `latest_published_at`, `sampling_category`, `sampling_rank`, `sampling_seed`, `source_url` |
| `package_version` | `package_id`, `version`, `published_at`, `license`, `is_deprecated`, `source` |
| `dependency` | `package_version_id`, `dependency_name`, `dependency_normalized_name`, `declared_constraint`, `dependency_type`, `optional` |
| `vulnerability` | `source`, `source_id`, `summary`, `severity`, `cvss_vector`, `published_at`, `modified_at`, `withdrawn_at` |
| `vulnerability_alias` | `vulnerability_id`, `alias`, `alias_type` |
| `vulnerability_reference` | `vulnerability_id`, `url`, `reference_type` |
| `affected_package_range` | `vulnerability_id`, `ecosystem_id`, `package_name`, `range_type`, `introduced`, `fixed`, `last_affected`, `versions`, `raw_range` |
| `version_vulnerability` | `package_version_id`, `vulnerability_id`, `affected`, `certainty`, `affected_range_id` |
| `package_observation` | `package_id`, `collection_run_id`, `observed_at`, metadata snapshot |
| `download_observation` | `package_id`, `period`, `downloads`, `observed_at` |
| `sampled_package` | `collection_run_id`, `package_id`, `sampling_category`, `sampling_rank`, `sampling_seed` |
| `collection_run` | `source`, `spider`, `status`, `items_stored`, `errors`, `notes` |
| `collection_error` | `collection_run_id`, `url`, `stage`, `error_type`, `message` |

Package identity is scoped by ecosystem. Dependencies belong to a specific
package version. Affected ranges keep the advisory's own range rows, and
`version_vulnerability` points back to the range that produced each result.

## Sampling method

- **npm popular**: search result pages sorted by monthly downloads, interleaved
  across queries so one query cannot fill the group. **Random**: short random
  search terms, round-robin across queries, excluding popular packages.
- **PyPI popular**: the public top-pypi-packages dataset (monthly download
  ranking). **Random**: project sitemap shards chosen by seed.
- **RubyGems popular**: the stats page (top gems by downloads). rubygems.org
  serves ten stats pages and redirects later pages back to page 1, so the
  popular group is capped at 100 gems. **Random**: randomly chosen pages of the
  alphabetical gem index.
- **Hex popular**: the package index sorted by total downloads. **Random**:
  randomly chosen pages of the alphabetical package index.
- **Packagist popular**: the explore pages sorted by downloads. **Random**:
  seeded names drawn from the published package name list.
- **NuGet popular**: gallery search sorted by total downloads. **Random**:
  seeded search-result pages; the gallery stops paginating around page 1000,
  so this covers the tail of the relevance ordering rather than all 554k
  packages.
- **crates.io popular**: the crate list sorted by all-time downloads. **Random**:
  seeded pages of the alphabetical list. crates.io refuses alphabetical pages
  above ~200, so the pool is about 10k crates.

The random groups are reproducible from the recorded seed. They are not uniform
samples of the whole catalogue: npm random terms are arbitrary search words,
PyPI shard choice covers a few letter ranges, the RubyGems and Hex indexes are
alphabetical, and the NuGet and crates.io pools stop at the registries' own
pagination limits.

## Collection limitations

- **PyPI project pages behind Fastly.** Uncached project pages are sometimes
  answered with a "Client Challenge" interstitial. The browser layer waits for
  the interstitial to clear and records a `ChallengeBlocked` collection error
  when it does not. Packages that remain blocked are absent from the results.
- **PyPI dependencies are not collected.** pypi.org does not render
  per-release dependencies on the project page, and its JSON API is disallowed
  by robots.txt. PyPI rows therefore contribute metadata and versions only.
- **npm version list is partial.** The versions tab renders about 120 rows, not
  the full history. Sampling is therefore over the rendered window.
- **RubyGems dependencies are collected per selected version**, one extra page
  per version. Dependency pages list runtime and development dependencies;
  optionality is not exposed on the page and is stored as `false`.
- **Hex dependencies are collected per selected version**, one extra page per
  version. Hex publishes no development dependencies and does not show a
  license on the package page, so Hex license values stay empty.
- **Packagist dependencies come from each version's panel**, the same JSON the
  site loads when a version is expanded. Platform requirements (`php`, `ext-*`)
  are excluded because they are not Composer packages. Branch aliases
  (`dev-main`, `13.x-dev`) are not treated as releases, and the first selection
  pass spreads over version order because dates arrive with the panels.
- **NuGet dependencies are per target framework.** The warehouse stores one row
  per dependency name and version, so a dependency is kept if any framework
  requires it; the constraint is the first one the gallery lists. The full
  version table comes from a POST to the gallery's load-more endpoint, using the
  anti-forgery token from the same package page. If that request fails the
  spider falls back to the eight versions shown initially.
- **crates.io is a client-rendered Svelte app**, so every page goes through
  Selenium. The versions page loads 100 releases at a time; `CRATES_VERSION_PAGES`
  controls how many "Load More" clicks are made. If the full list was not
  loaded, `first_published_at` is left empty instead of reporting the oldest
  loaded release as the first one. Dependencies come from the version-specific
  dependencies page, which exposes runtime, build and dev kinds.
- **Rates and scale.** Concurrent requests per domain is 1 by default. The
  2,500-packages-per-ecosystem target is a goal, not a result; run a pilot,
  measure, and adjust.
- A known advisory does not prove exploitation, and a dependency path does not
  prove that an application installs the vulnerable version.

## Tests

```bash
.venv/bin/python -m pytest
```

Tests are deterministic and use saved HTML/JSON fixtures; they do not visit
live websites or require PostgreSQL. The matcher and version comparison are
covered for SemVer, PEP 440 and RubyGems ordering, including disjoint ranges,
boundaries and unsupported formats.
