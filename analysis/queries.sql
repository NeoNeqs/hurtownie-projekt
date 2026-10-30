-- Analytical queries over the latest collection run of each ecosystem.
--
-- Every query starts from the packages sampled in the most recent successful
-- run per source, so adding a new collection run changes the results on
-- purpose instead of silently mixing runs.
--
-- Caveats that apply throughout:
--   * The sample is not representative of an ecosystem.
--   * An affected version in the warehouse only means OSV lists it in a range;
--     it does not prove exploitation or that an application installs it.
--   * A package without a matching advisory is not known to be secure.
--   * Dependencies are declared constraints, not a resolved install graph.

-- name: sample_overview
-- Denominator for every per-ecosystem share: sampled packages in the latest run.
WITH latest_run AS (
    SELECT DISTINCT ON (source) id, source
    FROM collection_run
    WHERE spider IN ('npm', 'pypi', 'rubygems', 'hex', 'packagist', 'nuget', 'crates')
      AND status IN ('succeeded', 'partial')
    ORDER BY source, started_at DESC
)
SELECT e.name AS ecosystem,
       count(*) AS sampled_packages,
       count(*) FILTER (WHERE sp.sampling_category = 'popular') AS popular,
       count(*) FILTER (WHERE sp.sampling_category = 'random') AS random
FROM sampled_package sp
JOIN latest_run r ON r.id = sp.collection_run_id
JOIN package p ON p.id = sp.package_id
JOIN ecosystem e ON e.id = p.ecosystem_id
GROUP BY e.name
ORDER BY e.name;

-- name: packages_with_vulnerability
-- A package counts once no matter how many of its collected versions match.
WITH latest_run AS (
    SELECT DISTINCT ON (source) id, source
    FROM collection_run
    WHERE spider IN ('npm', 'pypi', 'rubygems', 'hex', 'packagist', 'nuget', 'crates')
      AND status IN ('succeeded', 'partial')
    ORDER BY source, started_at DESC
), sample AS (
    SELECT sp.package_id, sp.sampling_category AS category, e.name AS ecosystem
    FROM sampled_package sp
    JOIN latest_run r ON r.id = sp.collection_run_id
    JOIN package p ON p.id = sp.package_id
    JOIN ecosystem e ON e.id = p.ecosystem_id
)
SELECT s.ecosystem,
       count(*) AS sampled_packages,
       count(*) FILTER (WHERE has_vulnerability) AS packages_with_vulnerability,
       round(100.0 * count(*) FILTER (WHERE has_vulnerability) / count(*), 1)
           AS percent_of_sample
FROM (
    SELECT s.*,
           EXISTS (
               SELECT 1
               FROM package_version pv
               JOIN version_vulnerability vv
                 ON vv.package_version_id = pv.id AND vv.affected
               WHERE pv.package_id = s.package_id
           ) AS has_vulnerability
    FROM sample s
) s
GROUP BY s.ecosystem
ORDER BY s.ecosystem;

-- name: vulnerability_counts_and_severity
-- Distinct advisories affecting at least one collected version of a sampled
-- package. Severity comes from the advisory source and is often only "HIGH"
-- or missing, so "unknown" is kept instead of being dropped.
WITH latest_run AS (
    SELECT DISTINCT ON (source) id, source
    FROM collection_run
    WHERE spider IN ('npm', 'pypi', 'rubygems', 'hex', 'packagist', 'nuget', 'crates')
      AND status IN ('succeeded', 'partial')
    ORDER BY source, started_at DESC
), sample AS (
    SELECT sp.package_id, e.name AS ecosystem
    FROM sampled_package sp
    JOIN latest_run r ON r.id = sp.collection_run_id
    JOIN package p ON p.id = sp.package_id
    JOIN ecosystem e ON e.id = p.ecosystem_id
)
SELECT s.ecosystem,
       coalesce(v.severity, 'unknown') AS severity,
       count(DISTINCT v.id) AS advisories
FROM sample s
JOIN package_version pv ON pv.package_id = s.package_id
JOIN version_vulnerability vv
  ON vv.package_version_id = pv.id AND vv.affected
JOIN vulnerability v ON v.id = vv.vulnerability_id
GROUP BY s.ecosystem, coalesce(v.severity, 'unknown')
ORDER BY s.ecosystem, advisories DESC;

-- name: latest_versions_affected
-- Only versions the collector recorded as the current latest release.
WITH latest_run AS (
    SELECT DISTINCT ON (source) id, source
    FROM collection_run
    WHERE spider IN ('npm', 'pypi', 'rubygems', 'hex', 'packagist', 'nuget', 'crates')
      AND status IN ('succeeded', 'partial')
    ORDER BY source, started_at DESC
), sample AS (
    SELECT sp.package_id, e.name AS ecosystem
    FROM sampled_package sp
    JOIN latest_run r ON r.id = sp.collection_run_id
    JOIN package p ON p.id = sp.package_id
    JOIN ecosystem e ON e.id = p.ecosystem_id
)
SELECT s.ecosystem,
       count(DISTINCT s.package_id) AS sampled_packages,
       count(DISTINCT s.package_id) FILTER (WHERE affected_package) AS latest_affected
FROM (
    SELECT s.package_id, s.ecosystem,
           EXISTS (
               SELECT 1
               FROM package p
               JOIN package_version pv
                 ON pv.package_id = p.id AND pv.version = p.latest_version
               JOIN version_vulnerability vv
                 ON vv.package_version_id = pv.id AND vv.affected
               WHERE p.id = s.package_id
           ) AS affected_package
    FROM sample s
) s
GROUP BY s.ecosystem
ORDER BY s.ecosystem;

-- name: dependency_count_vs_vulnerabilities
-- Correlation between direct dependencies of the latest version and the number
-- of advisories affecting that same version. Packages whose latest version has
-- no dependency rows are excluded: the collector may not have collected them.
WITH latest_run AS (
    SELECT DISTINCT ON (source) id, source
    FROM collection_run
    WHERE spider IN ('npm', 'pypi', 'rubygems', 'hex', 'packagist', 'nuget', 'crates')
      AND status IN ('succeeded', 'partial')
    ORDER BY source, started_at DESC
), sample AS (
    SELECT sp.package_id, e.name AS ecosystem
    FROM sampled_package sp
    JOIN latest_run r ON r.id = sp.collection_run_id
    JOIN package p ON p.id = sp.package_id
    JOIN ecosystem e ON e.id = p.ecosystem_id
), latest AS (
    SELECT s.ecosystem,
           pv.id AS version_id,
           (SELECT count(*) FROM dependency d
             WHERE d.package_version_id = pv.id) AS dependencies,
           (SELECT count(*) FROM version_vulnerability vv
             WHERE vv.package_version_id = pv.id AND vv.affected) AS advisories
    FROM sample s
    JOIN package p ON p.id = s.package_id
    JOIN package_version pv
      ON pv.package_id = p.id AND pv.version = p.latest_version
)
SELECT ecosystem,
       count(*) AS packages,
       round(avg(dependencies), 1) AS avg_dependencies,
       round(avg(advisories), 2) AS avg_advisories,
       round(corr(dependencies, advisories)::numeric, 3) AS correlation
FROM latest
GROUP BY ecosystem
ORDER BY ecosystem;

-- name: direct_dependency_exposure
-- A declared dependency whose target package has at least one known-affected
-- version anywhere in the warehouse. This is potential exposure: constraints
-- are not resolved, so the installed version may differ.
WITH latest_run AS (
    SELECT DISTINCT ON (source) id, source
    FROM collection_run
    WHERE spider IN ('npm', 'pypi', 'rubygems', 'hex', 'packagist', 'nuget', 'crates')
      AND status IN ('succeeded', 'partial')
    ORDER BY source, started_at DESC
), sample AS (
    SELECT sp.package_id, e.name AS ecosystem, p.ecosystem_id
    FROM sampled_package sp
    JOIN latest_run r ON r.id = sp.collection_run_id
    JOIN package p ON p.id = sp.package_id
    JOIN ecosystem e ON e.id = p.ecosystem_id
), latest AS (
    SELECT s.package_id, s.ecosystem, s.ecosystem_id, pv.id AS version_id
    FROM sample s
    JOIN package p ON p.id = s.package_id
    JOIN package_version pv
      ON pv.package_id = p.id AND pv.version = p.latest_version
)
SELECT l.ecosystem,
       count(DISTINCT l.package_id) AS packages_with_affected_dependency
FROM latest l
JOIN dependency d ON d.package_version_id = l.version_id
JOIN package target
  ON target.ecosystem_id = l.ecosystem_id
 AND target.normalized_name = d.dependency_normalized_name
WHERE EXISTS (
    SELECT 1
    FROM package_version tv
    JOIN version_vulnerability vv
      ON vv.package_version_id = tv.id AND vv.affected
    WHERE tv.package_id = target.id
)
GROUP BY l.ecosystem
ORDER BY l.ecosystem;

-- name: transitive_dependency_exposure
-- Depth-limited graph over declared dependencies, resolved by package name
-- within one ecosystem. It answers "can a path reach a package with an
-- affected version", not "does an install contain one". Cycles are cut by
-- tracking visited names.
WITH RECURSIVE latest_run AS (
    SELECT DISTINCT ON (source) id, source
    FROM collection_run
    WHERE spider IN ('npm', 'pypi', 'rubygems', 'hex', 'packagist', 'nuget', 'crates')
      AND status IN ('succeeded', 'partial')
    ORDER BY source, started_at DESC
), sample AS (
    SELECT sp.package_id, e.name AS ecosystem, p.ecosystem_id, p.normalized_name
    FROM sampled_package sp
    JOIN latest_run r ON r.id = sp.collection_run_id
    JOIN package p ON p.id = sp.package_id
    JOIN ecosystem e ON e.id = p.ecosystem_id
), latest AS (
    SELECT s.*, pv.id AS version_id
    FROM sample s
    JOIN package p ON p.id = s.package_id
    JOIN package_version pv
      ON pv.package_id = p.id AND pv.version = p.latest_version
), graph AS (
    SELECT l.ecosystem,
           l.ecosystem_id,
           l.package_id AS start_package_id,
           l.normalized_name AS start_name,
           l.version_id,
           d.dependency_normalized_name AS dependency_name,
           1 AS depth,
           ARRAY[l.normalized_name, d.dependency_normalized_name] AS path
    FROM latest l
    JOIN dependency d ON d.package_version_id = l.version_id
    UNION ALL
    SELECT g.ecosystem,
           g.ecosystem_id,
           g.start_package_id,
           g.start_name,
           g.version_id,
           d.dependency_normalized_name,
           g.depth + 1,
           g.path || d.dependency_normalized_name
    FROM graph g
    JOIN package p
      ON p.ecosystem_id = g.ecosystem_id
     AND p.normalized_name = g.dependency_name
    JOIN package_version pv ON pv.package_id = p.id
    JOIN dependency d ON d.package_version_id = pv.id
    WHERE g.depth < 3
      AND NOT d.dependency_normalized_name = ANY(g.path)
), affected_names AS (
    SELECT DISTINCT p.ecosystem_id, p.normalized_name
    FROM package p
    JOIN package_version pv ON pv.package_id = p.id
    JOIN version_vulnerability vv
      ON vv.package_version_id = pv.id AND vv.affected
)
SELECT g.ecosystem,
       count(DISTINCT g.start_package_id) AS packages_with_potential_path
FROM graph g
JOIN affected_names a
  ON a.ecosystem_id = g.ecosystem_id
 AND a.normalized_name = g.dependency_name
GROUP BY g.ecosystem
ORDER BY g.ecosystem;

-- name: popular_vs_random
-- Does the popular group look different from the comparison group? Only
-- collected versions are considered, so a smaller denominator means less
-- evidence, not safety.
WITH latest_run AS (
    SELECT DISTINCT ON (source) id, source
    FROM collection_run
    WHERE spider IN ('npm', 'pypi', 'rubygems', 'hex', 'packagist', 'nuget', 'crates')
      AND status IN ('succeeded', 'partial')
    ORDER BY source, started_at DESC
), sample AS (
    SELECT sp.package_id, sp.sampling_category AS category, e.name AS ecosystem
    FROM sampled_package sp
    JOIN latest_run r ON r.id = sp.collection_run_id
    JOIN package p ON p.id = sp.package_id
    JOIN ecosystem e ON e.id = p.ecosystem_id
)
SELECT s.ecosystem, s.category,
       count(*) AS sampled_packages,
       count(*) FILTER (WHERE has_vulnerability) AS with_vulnerability,
       round(100.0 * count(*) FILTER (WHERE has_vulnerability) / count(*), 1)
           AS percent
FROM (
    SELECT s.*,
           EXISTS (
               SELECT 1
               FROM package_version pv
               JOIN version_vulnerability vv
                 ON vv.package_version_id = pv.id AND vv.affected
               WHERE pv.package_id = s.package_id
           ) AS has_vulnerability
    FROM sample s
) s
GROUP BY s.ecosystem, s.category
ORDER BY s.ecosystem, s.category;

-- name: remediation_delay
-- Days between advisory publication and the release of the fixed version.
-- Only exact string matches between the OSV fixed value and a collected
-- version are used; ecosystem-normalised equivalents (for example 1.0 vs
-- 1.0.0) are skipped rather than guessed. Undated advisories or releases are
-- excluded and visible in the remaining row count.
WITH ranges AS (
    SELECT DISTINCT v.id AS vulnerability_id,
           v.published_at,
           r.ecosystem_id,
           r.package_normalized_name,
           r.fixed
    FROM affected_package_range r
    JOIN vulnerability v ON v.id = r.vulnerability_id
    WHERE r.fixed IS NOT NULL
      AND v.published_at IS NOT NULL
      AND v.withdrawn_at IS NULL
)
SELECT e.name AS ecosystem,
       count(*) AS matched_fixed_releases,
       round(avg(extract(epoch FROM (pv.published_at - r.published_at)) / 86400)::numeric, 1)
           AS avg_days_to_fix,
       round(percentile_cont(0.5) WITHIN GROUP (
           ORDER BY extract(epoch FROM (pv.published_at - r.published_at)) / 86400
       )::numeric, 1) AS median_days_to_fix
FROM ranges r
JOIN ecosystem e ON e.id = r.ecosystem_id
JOIN package p
  ON p.ecosystem_id = r.ecosystem_id
 AND p.normalized_name = r.package_normalized_name
JOIN package_version pv
  ON pv.package_id = p.id AND pv.version = r.fixed
WHERE pv.published_at IS NOT NULL
  AND pv.published_at >= r.published_at
GROUP BY e.name
ORDER BY e.name;

-- name: release_cadence
-- Median gap between collected releases per package, summarised per ecosystem.
-- The collector samples release history, so this is cadence among collected
-- versions, not the true release interval.
WITH gaps AS (
    SELECT e.name AS ecosystem,
           pv.package_id,
           extract(epoch FROM (
               pv.published_at - lag(pv.published_at) OVER (
                   PARTITION BY pv.package_id ORDER BY pv.published_at
               )
           )) / 86400 AS days
    FROM package_version pv
    JOIN package p ON p.id = pv.package_id
    JOIN ecosystem e ON e.id = p.ecosystem_id
    WHERE pv.published_at IS NOT NULL
)
SELECT ecosystem,
       count(*) AS release_gaps,
       round(avg(days)::numeric, 1) AS avg_days_between_releases,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY days)::numeric, 1)
           AS median_days_between_releases
FROM gaps
WHERE days > 0
GROUP BY ecosystem
ORDER BY ecosystem;

-- name: highest_potential_exposure
-- Ranks sampled packages by affected collected versions, then by how many
-- other sampled packages declare them. The number is an indicator of potential
-- exposure, not a claim about deployed systems.
WITH latest_run AS (
    SELECT DISTINCT ON (source) id, source
    FROM collection_run
    WHERE spider IN ('npm', 'pypi', 'rubygems', 'hex', 'packagist', 'nuget', 'crates')
      AND status IN ('succeeded', 'partial')
    ORDER BY source, started_at DESC
), sample AS (
    SELECT sp.package_id, e.name AS ecosystem, p.ecosystem_id, p.normalized_name
    FROM sampled_package sp
    JOIN latest_run r ON r.id = sp.collection_run_id
    JOIN package p ON p.id = sp.package_id
    JOIN ecosystem e ON e.id = p.ecosystem_id
)
SELECT s.ecosystem, p.name,
       count(DISTINCT vv.vulnerability_id) AS affected_advisories,
       (SELECT count(DISTINCT dpv.package_id)
          FROM dependency d
          JOIN package_version dpv ON dpv.id = d.package_version_id
          JOIN sample su ON su.package_id = dpv.package_id
         WHERE su.ecosystem_id = s.ecosystem_id
           AND d.dependency_normalized_name = s.normalized_name) AS declared_by_sampled,
       round(
           count(DISTINCT vv.vulnerability_id) *
           (1 + (SELECT count(DISTINCT dpv.package_id)
                   FROM dependency d
                   JOIN package_version dpv ON dpv.id = d.package_version_id
                   JOIN sample su ON su.package_id = dpv.package_id
                  WHERE su.ecosystem_id = s.ecosystem_id
                    AND d.dependency_normalized_name = s.normalized_name))::numeric,
           1
       ) AS exposure_score
FROM sample s
JOIN package p ON p.id = s.package_id
JOIN package_version pv ON pv.package_id = s.package_id
JOIN version_vulnerability vv
  ON vv.package_version_id = pv.id AND vv.affected
GROUP BY s.ecosystem, s.ecosystem_id, s.normalized_name, p.name
ORDER BY exposure_score DESC, affected_advisories DESC
LIMIT 20;

-- name: data_completeness
-- Field and dependency coverage for the sampled packages in the latest run.
-- Downloads come from download_observation because package metadata keeps no
-- current count; a package counts as covered when at least one observation
-- exists.
WITH latest_run AS (
    SELECT DISTINCT ON (source) id, source
    FROM collection_run
    WHERE spider IN ('npm', 'pypi', 'rubygems', 'hex', 'packagist', 'nuget', 'crates')
      AND status IN ('succeeded', 'partial')
    ORDER BY source, started_at DESC
), sample AS (
    SELECT sp.package_id, e.name AS ecosystem
    FROM sampled_package sp
    JOIN latest_run r ON r.id = sp.collection_run_id
    JOIN package p ON p.id = sp.package_id
    JOIN ecosystem e ON e.id = p.ecosystem_id
)
SELECT s.ecosystem,
       count(*) AS sampled_packages,
       round(100.0 * count(*) FILTER (
           WHERE coalesce(p.description, '') <> ''
       ) / count(*), 1) AS description_pct,
       round(100.0 * count(*) FILTER (
           WHERE coalesce(p.license, '') <> ''
       ) / count(*), 1) AS license_pct,
       round(100.0 * count(*) FILTER (
           WHERE coalesce(p.repository_url, '') <> ''
       ) / count(*), 1) AS repository_pct,
       round(100.0 * count(*) FILTER (
           WHERE EXISTS (
               SELECT 1 FROM download_observation d WHERE d.package_id = p.id
           )
       ) / count(*), 1) AS downloads_pct,
       coalesce(sum(v.version_count), 0) AS versions,
       round(avg(v.version_count)::numeric, 1) AS avg_versions_per_package,
       count(*) FILTER (WHERE v.version_count > 0) AS packages_with_versions,
       coalesce(sum(v.dependency_versions), 0) AS versions_with_dependencies
FROM sample s
JOIN package p ON p.id = s.package_id
LEFT JOIN LATERAL (
    SELECT count(*) AS version_count,
           count(*) FILTER (
               WHERE EXISTS (
                   SELECT 1 FROM dependency d
                   WHERE d.package_version_id = pv.id
               )
           ) AS dependency_versions
    FROM package_version pv
    WHERE pv.package_id = p.id
) v ON true
GROUP BY s.ecosystem
ORDER BY s.ecosystem;

-- name: collection_freshness
-- Latest collector run per source, including failures, so stale or blocked
-- ecosystems are visible next to the analysis results.
WITH latest AS (
    SELECT DISTINCT ON (source) *
    FROM collection_run
    WHERE spider IN ('npm', 'pypi', 'rubygems', 'hex', 'packagist', 'nuget', 'crates')
    ORDER BY source, started_at DESC
)
SELECT CASE spider
           WHEN 'pypi' THEN 'PyPI'
           WHEN 'crates' THEN 'crates.io'
           WHEN 'rubygems' THEN 'RubyGems'
           WHEN 'nuget' THEN 'NuGet'
           WHEN 'packagist' THEN 'Packagist'
           WHEN 'hex' THEN 'Hex'
           ELSE spider
       END AS ecosystem,
       source, spider, status, started_at, finished_at,
       items_stored, items_rejected, errors,
       now() - started_at AS age
FROM latest
ORDER BY ecosystem;

-- name: collection_errors
-- Failures recorded for the latest run of each ecosystem, grouped by stage and
-- type. ChallengeBlocked rows here mean the collector could not read a page.
WITH latest_run AS (
    SELECT DISTINCT ON (source) id, source, spider
    FROM collection_run
    WHERE spider IN ('npm', 'pypi', 'rubygems', 'hex', 'packagist', 'nuget', 'crates')
      AND status IN ('succeeded', 'partial', 'failed')
    ORDER BY source, started_at DESC
)
SELECT CASE r.spider
           WHEN 'pypi' THEN 'PyPI'
           WHEN 'crates' THEN 'crates.io'
           WHEN 'rubygems' THEN 'RubyGems'
           WHEN 'nuget' THEN 'NuGet'
           WHEN 'packagist' THEN 'Packagist'
           WHEN 'hex' THEN 'Hex'
           ELSE r.spider
       END AS ecosystem,
       ce.stage,
       ce.error_type,
       count(*) AS occurrences,
       min(ce.message) AS example
FROM collection_error ce
JOIN latest_run r ON r.id = ce.collection_run_id
GROUP BY r.spider, ce.stage, ce.error_type
ORDER BY ecosystem, occurrences DESC;
