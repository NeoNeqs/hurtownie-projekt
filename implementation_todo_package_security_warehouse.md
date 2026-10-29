# Implementation TODO --- Cross-Ecosystem Package Security Data Warehouse

## 1. Project Goal

Build a PostgreSQL data warehouse that collects package metadata,
version-specific dependency information, and known vulnerability data
across seven package ecosystems. Use the warehouse to compare dependency
structures and known vulnerabilities, and investigate how vulnerability
exposure can propagate through dependency chains.

The implementation must reuse the architecture and conventions of the
user's existing sample project. Inspect that project before designing or
replacing its components.

## 2. Supported Ecosystems

Implement collectors for:

-   [ ] npm
-   [ ] PyPI
-   [ ] crates.io
-   [ ] RubyGems
-   [ ] NuGet
-   [ ] Packagist
-   [ ] Hex

Do not add GitHub or Hugging Face as package ecosystems for this
project.

The initial sampling target is approximately **2,500 packages per
ecosystem**:

-   Around 2,000 popular packages.
-   Around 500 randomly selected comparison packages.

This is a target, not a guaranteed scope. Run a pilot first, estimate
collection cost and data availability, and adjust the target if
necessary. Record the sampling method and preserve the distinction
between popular and random comparison groups.

## 3. Inspect the Existing Project First

-   [ ] Review the sample project's directory structure, dependencies,
    configuration, logging, error handling, and test setup.
-   [ ] Identify its existing Scrapy and Selenium integration patterns.
-   [ ] Reuse suitable components and conventions instead of replacing
    the architecture without a clear reason.
-   [ ] Identify what can be generalized across websites and what must
    remain ecosystem-specific.
-   [ ] Document any necessary architectural changes briefly, focusing
    on why they are needed.
-   [ ] Establish a minimal working baseline before adding the new
    collectors.

## 4. Build a Reusable Scrapy/Selenium Collection Framework

Create reusable abstractions for common browser interactions. Keep
website-specific selectors and extraction rules in small, configurable
site-specific components.

-   [ ] Create a shared Selenium browser manager with consistent setup,
    teardown, and resource cleanup.
-   [ ] Integrate browser-driven collection with Scrapy's request and
    item flow, following the existing project's architecture.
-   [ ] Implement a reusable infinite-scroll handler. Site-specific
    implementations should primarily supply selectors and configuration
    rather than duplicate the scrolling logic.
-   [ ] Implement reusable pagination handling where sites use page
    links, next buttons, or other predictable pagination patterns.
-   [ ] Add shared helpers for waiting, locating elements, extracting
    text and attributes, and handling missing elements.
-   [ ] Support configurable selectors, wait conditions, timeouts, and
    scroll limits.
-   [ ] Provide a common base spider or equivalent abstraction for
    shared collection behavior.
-   [ ] Keep ecosystem-specific selectors and extraction logic isolated
    so that site changes do not require changes to the shared framework.
-   [ ] Add consistent retry handling, structured logging, and clear
    error reporting.
-   [ ] Handle browser and driver cleanup even when collection fails.
-   [ ] Add tests for shared behavior, including scrolling, pagination,
    timeouts, missing elements, retries, and cleanup.
-   [ ] Add authentication support only when needed and permitted by the
    website's terms and access rules.
-   [ ] Respect site terms, robots rules where applicable, rate limits,
    and other access restrictions.
-   [ ] Never bypass CAPTCHAs, access controls, or anti-bot protections.

Official registry APIs or other permitted interfaces may supplement
website collection for discovery and metadata. Do not use an API as an
excuse to omit the required website-scraping component. Record which
source and collection method supplied each dataset.

## 5. Package Sampling and Discovery

-   [ ] Define a repeatable method for identifying popular packages in
    each ecosystem.
-   [ ] Define a reproducible random sampling method for the comparison
    group.
-   [ ] Store the sampling category (`popular` or `random`) and relevant
    sampling metadata.
-   [ ] Avoid duplicate packages within an ecosystem's sample.
-   [ ] Record exclusions, inaccessible pages, failed requests, and
    other collection limitations.
-   [ ] Pilot the sampling and collection process on one ecosystem
    before scaling to all seven.
-   [ ] Do not imply that the selected packages represent the entire
    ecosystem; document the sampling limitations in the final analysis.

## 6. PostgreSQL Warehouse

Design a normalized, maintainable PostgreSQL schema. Add migrations or
another repeatable schema-management mechanism consistent with the
existing project.

### Core entities

-   [ ] `ecosystem` --- supported package ecosystem and its identifying
    metadata.
-   [ ] `package` --- package identity within an ecosystem, name,
    descriptive metadata, repository or homepage where available,
    license information, and first/last observation timestamps.
-   [ ] `package_version` --- version identifier, publication/release
    date where available, and version-specific metadata.
-   [ ] `dependency` --- dependency relationships associated with a
    package version, including dependency name, declared version/range,
    dependency type, and optionality where available.
-   [ ] `vulnerability` --- vulnerability identity, source, summary or
    references where available, severity and CVSS information where
    available, and relevant dates.
-   [ ] `affected_package_range` --- ecosystem/package identity and the
    affected version ranges associated with a vulnerability, including
    fixed-version information when available.
-   [ ] `package_observation` --- timestamped observations of package
    metadata when historical comparison is useful.
-   [ ] `download_observation` --- timestamped download counts or other
    popularity measurements, when available and comparable.
-   [ ] `collection_run` --- run identifier, source, start/end
    timestamps, status, counts, and error or limitation summaries.

### Schema requirements

-   [ ] Define primary keys, foreign keys, unique constraints, and
    indexes for common joins and filters.
-   [ ] Keep package identity scoped to its ecosystem.
-   [ ] Preserve version-specific dependency information rather than
    storing only a package-level dependency list.
-   [ ] Preserve multiple and non-contiguous affected version ranges.
-   [ ] Keep source identifiers and provenance for vulnerability and
    package records.
-   [ ] Store observation timestamps so data freshness and collection
    history can be evaluated.
-   [ ] Use suitable data types and constraints; avoid storing
    structured relationships only as opaque JSON when relational queries
    are needed.
-   [ ] Add migrations and a repeatable way to initialize a clean
    database.
-   [ ] Include representative seed or fixture data for tests.

## 7. Package Metadata and Version History

For each sampled package, collect as much of the following as is
available and permitted:

-   [ ] Package name and ecosystem.
-   [ ] Description and project/repository/homepage links.
-   [ ] License information.
-   [ ] First publication and latest release dates, where available.
-   [ ] Observed versions and release dates.
-   [ ] Latest observed version at collection time.
-   [ ] Download or popularity measurements, where available.
-   [ ] Dependencies for individual versions.
-   [ ] Dependency type and optionality, where available.
-   [ ] Collection timestamp and source provenance.

Missing fields should be represented consistently rather than
fabricated. Record source limitations and distinguish unavailable data
from confirmed absence.

## 8. Vulnerability Data Ingestion

Use **OSV as the primary vulnerability source** and **NVD as a
supplementary source for CVE enrichment**.

-   [ ] Review the OSV API and schema documentation before implementing
    ingestion.
-   [ ] Implement a repeatable OSV ingestion process.
-   [ ] Retrieve vulnerability records relevant to the supported
    ecosystems and sampled packages, using supported interfaces and
    reasonable request rates.
-   [ ] Store source identifiers and preserve CVE identifiers when
    present; do not assume every vulnerability has a CVE.
-   [ ] Store severity, CVSS information, references,
    publication/modification dates, and fix information when provided.
-   [ ] Use NVD as a supplementary source for CVE details and CVSS
    information where useful.
-   [ ] Avoid creating duplicate vulnerability records when the same
    issue appears in multiple sources.
-   [ ] Preserve source-specific identifiers and provenance when
    reconciling records.
-   [ ] Make ingestion repeatable and safe to rerun.
-   [ ] Track failed requests, incomplete records, and the last
    successful ingestion time.
-   [ ] Do not interpret a missing vulnerability record as proof that a
    package is secure.

Reference documentation:

-   OSV: https://osv.dev/
-   OSV API: https://google.github.io/osv.dev/api/
-   OSV schema: https://ossf.github.io/osv-schema/
-   NVD: https://nvd.nist.gov/
-   NVD vulnerability API documentation:
    https://nvd.nist.gov/developers/vulnerabilities

## 9. Version-Specific Vulnerability Matching

This is one of the most important and technically demanding parts of the
project. Avoid marking an entire package as vulnerable when only some
versions are affected.

-   [ ] Match vulnerability records to the correct ecosystem and package
    identity.
-   [ ] Parse and preserve affected version ranges from the
    vulnerability source.
-   [ ] Support multiple and non-contiguous affected ranges.
-   [ ] Preserve fixed-version or unaffected-range information when
    supplied.
-   [ ] Use ecosystem-appropriate version comparison rules rather than
    comparing version strings lexicographically.
-   [ ] Account for ecosystem-specific version formats and range
    conventions.
-   [ ] Make the matching logic testable independently from scraping and
    ingestion.
-   [ ] Add tests for affected versions, fixed versions, boundary
    conditions, pre-releases where relevant, and multiple disjoint
    ranges.
-   [ ] Record uncertainty or unsupported range formats rather than
    silently guessing.
-   [ ] Make it possible to determine whether a specific package version
    is known to be affected by a particular vulnerability.

Do not collapse version-specific results into a single package-level
`vulnerable` flag without retaining the underlying version and range
evidence.

## 10. Dependency Graph

Build a graph from the collected dependency declarations.

-   [ ] Store direct dependencies against the package versions that
    declare them.
-   [ ] Distinguish direct dependencies from transitive dependencies
    derived by traversing the graph.
-   [ ] Preserve dependency types, optionality, and declared version
    constraints where available.
-   [ ] Build queries or processing code to traverse dependency chains.
-   [ ] Identify sampled packages whose dependency chains include a
    known affected package version, where the collected data supports
    that conclusion.
-   [ ] Keep declared dependencies distinct from a fully resolved
    installation graph.
-   [ ] For the first implementation, prioritize correct direct
    dependency data and a basic transitive graph.
-   [ ] Do not attempt complete package-manager-compatible dependency
    resolution across all seven ecosystems unless the core system is
    complete and time remains.
-   [ ] Document limitations caused by missing versions, incomplete
    metadata, platform-specific dependencies, optional dependencies,
    peer dependencies, or unresolved version ranges.

When calculating exposure, distinguish between a dependency that is
declared and one that is known to resolve to an affected version. Do not
claim confirmed exposure when the available data only establishes a
potential dependency path.

## 11. Collection Scheduling and Updates

-   [ ] Make collection jobs repeatable and safe to rerun.
-   [ ] Support incremental updates where practical.
-   [ ] Record each collection run and its outcome.
-   [ ] Avoid overwriting historical observations when retaining history
    is necessary for analysis.
-   [ ] Add retries and clear failure states for transient collection
    errors.
-   [ ] Consider Apache Airflow for scheduling and orchestration if time
    permits.
-   [ ] Do not make Airflow a prerequisite for completing the core
    warehouse.
-   [ ] Document how to run collection and ingestion jobs manually.

## 12. Analytical Queries

Create reproducible SQL queries or analysis scripts for the following
questions.

-   [ ] How many sampled packages have at least one known vulnerability,
    by ecosystem?
-   [ ] How do vulnerability counts and severity distributions differ
    across ecosystems?
-   [ ] What proportion of the sampled packages has known
    vulnerabilities?
-   [ ] How many latest observed package versions are affected by known
    vulnerabilities?
-   [ ] How does direct dependency count relate to known vulnerability
    counts?
-   [ ] How many sampled packages have a direct dependency on a known
    affected package version?
-   [ ] How many have a transitive dependency path to a known affected
    package version, where the available data supports this analysis?
-   [ ] How do vulnerability findings differ between the popular and
    random comparison groups?
-   [ ] Where sufficient dates exist, how long does it take for a fixed
    version to appear after a vulnerability is published or disclosed?
-   [ ] Where sufficient release history exists, how do release cadence
    and remediation timing vary by ecosystem?
-   [ ] Which packages or dependency chains have the highest observed
    potential vulnerability exposure?

For every metric, define the denominator and the data-quality conditions
required for inclusion. Report missing or incomplete data instead of
silently excluding it.

Interpret results carefully: - The sample is not necessarily
representative of the entire ecosystem. - A known vulnerability record
does not prove that a package is exploited. - A dependency path does not
necessarily prove that a deployed application installs or uses an
affected version. - Missing vulnerability records do not prove that a
package is secure. - Comparisons across ecosystems may be affected by
differences in metadata availability, versioning, disclosure, and
reporting practices.

## 13. Dashboard and Reporting

-   [ ] Build a dashboard or concise reporting interface for comparing
    the seven ecosystems.
-   [ ] Include package sample sizes and the popular/random split.
-   [ ] Show known vulnerability counts and severity distributions.
-   [ ] Show the proportion of sampled packages with known
    vulnerabilities.
-   [ ] Show direct and transitive dependency exposure where supported
    by the data.
-   [ ] Include filters for ecosystem, package, version, severity, and
    sampling category where appropriate.
-   [ ] Display collection freshness and data completeness.
-   [ ] Make the scope and limitations of the sample visible.
-   [ ] Ensure dashboard figures can be traced to documented SQL or
    analysis logic.

Choose a dashboard technology that fits the existing project and
available time rather than introducing unnecessary infrastructure.

## 14. Testing and Data Quality

-   [ ] Add unit tests for shared scraping abstractions.
-   [ ] Add tests for ecosystem-specific extraction and normalization.
-   [ ] Add tests for schema constraints and database operations.
-   [ ] Add tests for vulnerability ingestion and duplicate handling.
-   [ ] Add tests for ecosystem-specific version comparison and
    affected-range matching.
-   [ ] Add tests for direct and transitive dependency traversal.
-   [ ] Add integration tests for at least one complete
    collection-to-analysis workflow.
-   [ ] Validate required fields, duplicate identities, invalid foreign
    keys, and inconsistent version records.
-   [ ] Report missing metadata, failed collection attempts, and
    unsupported vulnerability ranges.
-   [ ] Include a data-quality report in the final deliverables.

Use fixtures or saved sample responses for deterministic tests where
possible. Tests should not depend on live websites being available.

## 15. Documentation and Final Deliverables

-   [ ] Reusable Scrapy/Selenium framework with shared scrolling,
    pagination, waiting, extraction, and error-handling behavior.
-   [ ] Seven ecosystem collectors, or a clear record of any collector
    blocked by access restrictions or unavailable data.
-   [ ] PostgreSQL schema and migrations.
-   [ ] Repeatable package metadata and version collection.
-   [ ] Repeatable OSV ingestion and supplementary NVD enrichment.
-   [ ] Version-specific vulnerability matching with tests.
-   [ ] Direct dependency data and a basic transitive dependency graph.
-   [ ] Reproducible analytical SQL or scripts.
-   [ ] Dashboard or reporting interface.
-   [ ] Automated tests.
-   [ ] Setup and execution instructions.
-   [ ] Configuration documentation and example environment
    configuration without secrets.
-   [ ] Data dictionary describing tables, important fields, and
    relationships.
-   [ ] Data-quality report and a clear account of collection
    limitations.
-   [ ] Final analysis of findings, methodology, and limitations.

## 16. Suggested Implementation Order

Complete one end-to-end path before scaling the system across all
ecosystems.

1.  [ ] Inspect the sample project and identify reusable architecture.
2.  [ ] Implement and test the shared browser manager and generic
    interaction abstractions.
3.  [ ] Create the initial PostgreSQL schema and migrations.
4.  [ ] Select one ecosystem and collect a small pilot sample.
5.  [ ] Store package metadata, versions, and direct dependencies for
    that ecosystem.
6.  [ ] Ingest OSV vulnerability data and implement version-specific
    matching.
7.  [ ] Validate the first end-to-end path with tests and a small
    analytical query.
8.  [ ] Add the remaining six ecosystems, reusing the shared framework
    and isolating site-specific behavior.
9.  [ ] Improve dependency graph traversal and transitive exposure
    analysis.
10. [ ] Add repeatable updates and optional Airflow orchestration if
    time permits.
11. [ ] Build the dashboard and analytical queries.
12. [ ] Complete testing, data-quality checks, documentation, and final
    analysis.

## 17. Code Comments and Docstrings

-   Keep comments and docstrings to a minimum. Do not comment on code
    simply to restate what it does or explain how it works.
-   **Only add a comment when it answers "Why?"** Explain the reasoning
    behind a non-obvious decision, workaround, constraint, or piece of
    logic.
-   Comments must be short, natural, and written in clear, grammatically
    correct English. Avoid robotic, generic, or AI-sounding phrasing.
-   Write comments as a developer would when explaining a decision to
    another developer.
-   Avoid unnecessary docstrings, including docstrings that merely
    repeat a function's name, parameters, or implementation.
-   Keep code readable through clear naming, sensible structure, and
    small, focused functions rather than excessive commentary.
-   When a code fragment relies on project-specific behavior or a
    non-obvious assumption, add a brief explanation of the reasoning so
    that a developer unfamiliar with that part of the project can
    understand the intent.
-   Do not add comments just to meet a quota or make the code appear
    more documented.
-   Follow the same rules when modifying existing code. Remove redundant
    comments when appropriate, but preserve useful explanations of
    non-obvious decisions.

## 18. Implementation Priorities and Constraints

-   **First priority:** get one ecosystem working end to end before
    attempting all seven.
-   **Treat the reusable scraper framework as a deliverable of its
    own.** Shared behavior should be implemented once; site-specific
    selectors and extraction rules should remain small and configurable.
-   **Highest technical risk:** correctly matching vulnerability ranges
    to ecosystem-specific package versions.
-   Keep the first version focused on reliable data collection, correct
    version-specific vulnerability matching, direct dependency data, and
    a defensible basic transitive graph.
-   Defer full package-manager-compatible dependency resolution,
    elaborate orchestration, and advanced dashboard features until the
    core pipeline is reliable.
-   Respect website terms and access restrictions throughout
    implementation.
-   Record assumptions and limitations rather than fabricating missing
    data or presenting uncertain results as confirmed facts.
