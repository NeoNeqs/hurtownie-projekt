# Session handoff — package security warehouse

State as of 2026-10-31. Read this first, then `README.md` for commands and the
data dictionary, and `implementation_todo_package_security_warehouse.md` for the
original spec.

## Where the project stands

A Scrapy + Selenium + PostgreSQL warehouse that collects package metadata,
version-specific dependencies and OSV advisories across package ecosystems.

**All seven ecosystems are implemented and pilot-verified:** npm, PyPI,
crates.io, RubyGems, NuGet, Packagist, Hex. NVD is wired in as a CVSS/severity
supplement for advisories that OSV imports without scores. Collection is
website-only; the `npm_registry` module is an experimental leftover and is not
part of the pipeline.

**Commit state.** `a409657` is the last commit on `master`; it keeps npm
rate-limited requests retrying after the cool-down. The branch is ahead of
`origin/master`, so check `git log` and `git status` before assuming the remote
matches. The scale orchestrator and documentation also have uncommitted edits in
the working tree.

## How to run everything

```bash
docker compose up -d                     # postgres on localhost:55432
.venv/bin/python -m warehouse.db init    # create db + apply migrations + seed ecosystems

.venv/bin/python -m pytest               # 139 passed

# spider names: npm pypi crates rubygems nuget packagist hex
.venv/bin/scrapy crawl nuget
.venv/bin/scrapy crawl crates
.venv/bin/scrapy crawl npm

.venv/bin/python -m warehouse.osv --ecosystem NuGet --delay 0.1
.venv/bin/python -m warehouse.matching --ecosystem NuGet
.venv/bin/python -m warehouse.nvd --limit 100   # optional, needs network
.venv/bin/python analysis/run_queries.py
.venv/bin/python analysis/build_dashboard.py   # output/dashboard.html

# full scale-up across all seven ecosystems, then OSV, matching and report
scripts/scale_collection.sh
```

Gotchas:

- There is **no `.env` loader**. The app reads real environment variables only;
  `.env.example` is documentation. Either `set -a; source .env; set +a` or pass
  vars inline (`PYPI_MAX_VERSIONS=3 .venv/bin/scrapy crawl pypi`).
- Default DSN: `postgresql://warehouse:warehouse@localhost:55432/warehouse`
  (`warehouse/db.py:DEFAULT_DSN`). Never touch the `postgres_lab` database on
  port 5432.
- `warehouse.db.connect()` uses `dict_row`, so rows are dicts — `row[0]` raises
  `KeyError`, use `row["col"]`.
- No `psql` binary on this machine; query through Python.
- Tests are offline: fixtures in `tests/fixtures/`, no network, no PostgreSQL.
- **NuGet POST.** The full version table is a POST to
  `/packages/{name}/{version}/versiontable` guarded by an anti-forgery token.
  The token is bound to the cookie from the *same* package response; the shared
  Scrapy cookie jar is overwritten by concurrent package pages, so the spider
  copies the response cookies onto the request (`_response_cookies`). Do not
  remove that.
- **crates.io is Selenium-only** (client-rendered Svelte). Every page needs the
  browser. Its list API answers 403 and the alphabetical pages stop around 200.
- npm needs `BROWSER_HEADLESS=false` and the persistent default profile at
  `~/.cache/hurtownie/chrome-profile`, which keeps `cf_clearance` between runs.
  A full-page “Just a moment…” managed challenge cannot be cleared by the
  browser layer; record the run as `ChallengeBlocked` and leave it failed. Do
  not substitute the registry API.

## Pilot data (2026-10-30, not the current database state)

| ecosystem | packages | popular | random | versions | dependencies | vv rows | affected |
| --- | --- | --- | --- | --- | --- | --- | --- |
| npm | 12 | 8 | 4 | 37 | 589 | 50 | 50 |
| PyPI | 4 | 4 | 0 | 12 | 0 | 9 | 8 |
| crates.io | 3 | 2 | 1 | 5 | 53 | 0 | 0 |
| RubyGems | 5 | 3 | 2 | 9 | 30 | 0 | 0 |
| NuGet | 6 | 4 | 2 | 29 | 148 | 2 | 2 |
| Packagist | 5 | 3 | 2 | 10 | 6 | 0 | 0 |
| Hex | 5 | 3 | 2 | 10 | 6 | 0 | 0 |

Totals: 40 packages (27 popular, 13 random), 112 versions, 832 dependencies,
53 vulnerabilities, 61 `version_vulnerability` rows, 21 `collection_run` rows.

RubyGems/Hex/Packagist/crates.io show no matched advisories because their OSV
batches returned none for the sampled versions (RubyGems was checked by hand:
the sample postdates the fixed ranges). It is not a matching bug.

## Scale attempt (2026-10-30)

`output/scale-20261030-122225` is incomplete:

| spider | result |
| --- | --- |
| nuget, pypi, packagist, hex, rubygems | succeeded |
| npm (website) | stored 0 packages; recorded as failed |
| npm_registry (manual run, API) | 300 packages, 1463 versions, 871 affected; ended partial after HTTP 429s. This run is outside the collection policy and should not be merged with website data. |
| crates | killed after 43 packages; no summary row |
| analysis/report/dashboard | never ran; no `report.md` or `dashboard.html` |

The script previously hardcoded 250 popular + 50 random and now defaults to the
2,500-package target (2,000 + 500). It marks an exit-0 run that stored no items
as failed before skipping OSV and matching. npm always runs the website spider;
the registry API is not used, even when the website is challenged.

The npm retry/cooldown middleware is covered by unit tests. A live npm website
pilot on 2026-10-31 cleared five challenges and stored one package with 14
dependencies, using the default profile at `~/.cache/hurtownie/chrome-profile`.
A live PyPI pilot on the same day collected one package, two versions and their
PEP 658 dependencies, so both paths work.

The rate-limit middleware honours `Retry-After`, backs off exponentially from
600 s to 3,600 s with jitter, and by default retries forever, waiting until the
site lets the crawler back in. `NPM_RETRY_MAX_ATTEMPTS` and
`NPM_RETRY_MAX_TOTAL_COOLDOWN_SECONDS` can bound that if needed. Sleeps are
sliced so Ctrl+C still stops the crawl.

## What each module does

**Collection layer**

- `warehouse/sampling.py` — shared `select_versions` (`spread` / `downloads`) and
  `select_random_names` (seeded), used by every spider.
- `warehouse/spiders/npm.py` — search discovery, package page, versions tab,
  dependency tab. Headed Chrome for Cloudflare.
- `warehouse/spiders/pypi.py` — popular from the top-pypi-packages dataset,
  random from seeded sitemap shards, project page metadata, release history.
  No dependencies: pypi.org renders none and its JSON API is robots-disallowed.
- `warehouse/spiders/rubygems.py` — stats page, seeded alphabetical index pages,
  gem page, versions page, per-version dependency pages. The stats pages expose
  only the top 100 gems; later pages redirect to page 1, so the spider caps the
  popular group there.
- `warehouse/spiders/hex.py` — download-sorted and alphabetical indexes, package
  page, versions page, per-version dependency pages. No license, no dev deps.
- `warehouse/spiders/packagist.py` — explore pages, seeded names from
  `packages/list.json`, package page, version list, per-version panel JSON.
- `warehouse/spiders/nuget.py` — plain HTTP (no Selenium): download-sorted and
  relevance-paginated gallery pages, package page, full version table via POST
  with the anti-forgery cookie, per-version dependency pages. Dependencies are
  merged across target frameworks by name.
- `warehouse/spiders/crates.py` — Selenium: download-sorted and alphabetical
  list pages, crate page, versions page with configurable "Load More" clicks,
  per-version dependency pages with runtime/build/dev kinds.
- `warehouse/settings.py` + `.env.example` — per-ecosystem sample counts, seeds,
  page limits, `*_MAX_VERSIONS`, `*_VERSION_SELECTION`.

**Vulnerabilities**

- `warehouse/osv.py` — OSV ingestion: advisories, aliases, references, affected
  ranges, ecosystem package names.
- `warehouse/matching.py`, `warehouse/versioning.py` — ecosystem-aware range
  matching (SemVer, PEP 440, RubyGems); results in `version_vulnerability`.
- `warehouse/nvd.py` — looks up CVE aliases already recorded by OSV and fills
  missing severity/CVSS. Never introduces advisories of its own.

**Anti-bot / robots**

- `ROBOTSTXT_OBEY = True` everywhere.
- Challenges are **waited out automatically**, and persistent Cloudflare
  Turnstile widgets are clicked through CDP coordinates rather than Selenium
  clicks (`BrowserSession.solve_challenge`). This is the behaviour the project
  owner asked for. Unresolved challenges are recorded as `ChallengeBlocked`.
- `scripts/check_browser.py` prints the fingerprint and challenge state.

**Analysis and docs**

- `analysis/queries.sql` + `analysis/run_queries.py` — 13 queries with documented
  denominators, now covering all seven spiders.
- `analysis/build_dashboard.py` — writes a self-contained `output/dashboard.html`
  with sample cards, row filters, severity badges, completeness and freshness.
  Every section is one named query, so figures stay traceable to SQL.
- `README.md` — setup, run commands, sampling per ecosystem, dashboard, data
  dictionary, limitations, testing.

## Verification state

- `.venv/bin/python -m pytest` → **139 passed** (fixtures only, offline).
- Every spider has a successful pilot run recorded in `collection_run`.
- OSV ingestion and `warehouse.matching` ran for all seven ecosystems.
- NVD enrichment ran against the live API and filled two CVSS 3.1 scores.
- All 13 analysis queries execute with exit code 0.
- `analysis/build_dashboard.py` renders all 13 queries into `output/dashboard.html`.
- `compileall` over `warehouse/`, `analysis/`, `tests/` is clean.

## Remaining work (rough priority order)

1. **Finish the scale run** — resume npm and crates into
   `output/scale-20261030-122225`, then let the script produce the queries,
   report and dashboard. npm needs a warmed headed-Chrome profile; if it is
   still challenged the run stays failed, which is the correct record.
2. **npm has two collection sources** — the analysis `latest_run` CTE now
   excludes `npm_registry`, so only www.npmjs.com data counts. The manual
   registry data from 2026-10-30 is still in the database and can be deleted if
   a clean website-only history is wanted.
3. **PyPI dependencies** — now collected from PEP 658 `.metadata` sidecars via
   the wheel URL on each version page. Only wheel releases have sidecars; sdist-
   only versions keep empty dependencies. A live pilot on 2026-10-31 collected
   dependencies for two boto3 versions, so the path works.
4. **Resolved dependency graph** — stored constraints are declared ranges, not
   an install graph.
5. **Random-sample coverage** — reproducible per seed but not uniform:
   alphabetical indexes, sitemap letter ranges, search terms, and the NuGet /
   crates.io pagination limits. RubyGems popular is capped at 100 by its stats
   pages.
6. **OSV freshness** — advisories are upserted, never deleted; rerun ingestion
   periodically and consider tracking withdrawn advisories.
