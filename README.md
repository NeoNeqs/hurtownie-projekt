# Cross-ecosystem package security warehouse

A PostgreSQL warehouse for package metadata, version-specific dependencies and
known vulnerabilities across package ecosystems. Collectors use Scrapy with a
shared Selenium browser layer, so every package, version and dependency record
comes from a registry website. OSV supplies vulnerability records, and a
version-aware matcher maps affected ranges onto the versions that were
collected.

Implemented ecosystems: **npm**, **PyPI**, **RubyGems**, **Hex**, **Packagist**.
Planned: crates.io, NuGet.

## Layout

| Path | Purpose |
| --- | --- |
| `warehouse/browser.py` | Selenium session, browser pool, challenge detection, cleanup |
| `warehouse/middlewares.py` | Selenium downloader middleware and challenge handling |
| `warehouse/spiders/base.py` | Shared spider request/error helpers |
| `warehouse/spiders/npm.py` | npm discovery, metadata, versions, dependencies |
| `warehouse/spiders/pypi.py` | PyPI discovery, metadata, release history |
| `warehouse/spiders/rubygems.py` | RubyGems discovery, metadata, versions, dependencies |
| `warehouse/spiders/hex.py` | Hex discovery, metadata, versions, dependencies |
| `warehouse/spiders/packagist.py` | Packagist discovery, metadata, versions, dependencies |
| `warehouse/sampling.py` | Popular/random selection and release-history sampling |
| `warehouse/osv.py` | OSV ingestion (advisories, aliases, references, ranges) |
| `warehouse/versioning.py` | SemVer, PEP 440 and RubyGems version ordering |
| `warehouse/matching.py` | Version-to-range matching and persistence |
| `warehouse/pipelines.py` | Validation, deduplication and storage |
| `warehouse/migrations/` | Repeatable schema migrations |
| `analysis/queries.sql` | Analytical SQL with documented denominators |
| `analysis/run_queries.py` | Executes the analytical queries |

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
```

Every run creates a `collection_run` row and records failures in
`collection_error`. A run left behind by a killed process is marked `partial`
on the next start.

Version selection defaults to `spread`: the sampler takes the oldest, newest and
evenly spaced releases. Taking only the most downloaded versions hides exactly
the old versions advisories target. `NPM_VERSION_SELECTION=downloads` restores
the old behaviour for comparison; the same setting exists per ecosystem.

## Vulnerabilities

```bash
.venv/bin/python -m warehouse.osv --ecosystem npm --delay 0.1
.venv/bin/python -m warehouse.matching --ecosystem npm
```

Repeat with `PyPI`, `RubyGems`, `Hex` and `Packagist`. Ingestion is idempotent: advisories are
upserted by source id, and ranges are replaced per advisory and package.
Matching deletes and recomputes results for the ecosystem, so reruns are safe.

Unsupported range formats (for example `GIT`) are stored with
`certainty = 'uncertain'` instead of being guessed; a missing advisory is never
treated as proof that a package is safe.

## Analysis

```bash
.venv/bin/python analysis/run_queries.py
.venv/bin/python analysis/run_queries.py --query packages_with_vulnerability
```

Each query starts from the latest successful run per ecosystem and defines its
denominator. Results are based on collected versions only, and dependencies are
declared constraints rather than a resolved install graph.

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
- **RubyGems popular**: the stats page (top gems by downloads). **Random**:
  randomly chosen pages of the alphabetical gem index.
- **Hex popular**: the package index sorted by total downloads. **Random**:
  randomly chosen pages of the alphabetical package index.
- **Packagist popular**: the explore pages sorted by downloads. **Random**:
  seeded names drawn from the published package name list.

The random groups are reproducible from the recorded seed. They are not uniform
samples of the whole catalogue: npm random terms are arbitrary search words,
PyPI shard choice covers a few letter ranges, and the RubyGems and Hex indexes
are alphabetical.

## Collection limitations

- **PyPI project pages behind Fastly.** Uncached project pages are sometimes
  answered with a "Client Challenge" interstitial. The browser layer detects it
  and records a `ChallengeBlocked` collection error; it does not try to defeat
  the challenge. Those packages are absent from the results.
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
