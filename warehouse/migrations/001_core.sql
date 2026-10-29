CREATE TABLE ecosystem (
    id smallserial PRIMARY KEY,
    name text NOT NULL UNIQUE,
    homepage_url text,
    registry_url text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE collection_run (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    spider text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed', 'partial')),
    items_scraped integer NOT NULL DEFAULT 0,
    items_stored integer NOT NULL DEFAULT 0,
    items_rejected integer NOT NULL DEFAULT 0,
    errors integer NOT NULL DEFAULT 0,
    notes text
);

CREATE TABLE collection_error (
    id bigserial PRIMARY KEY,
    collection_run_id bigint REFERENCES collection_run(id) ON DELETE CASCADE,
    url text,
    stage text,
    error_type text,
    message text,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX collection_error_run_idx ON collection_error (collection_run_id);

CREATE TABLE package (
    id bigserial PRIMARY KEY,
    ecosystem_id smallint NOT NULL REFERENCES ecosystem(id),
    name text NOT NULL,
    normalized_name text NOT NULL,
    description text,
    homepage_url text,
    repository_url text,
    license text,
    latest_version text,
    first_published_at timestamptz,
    latest_published_at timestamptz,
    sampling_category text NOT NULL
        CHECK (sampling_category IN ('popular', 'random')),
    sampling_rank integer,
    sampling_seed integer,
    source text NOT NULL,
    source_url text,
    collection_method text,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ecosystem_id, normalized_name)
);

CREATE INDEX package_ecosystem_sample_idx
    ON package (ecosystem_id, sampling_category);

CREATE TABLE package_version (
    id bigserial PRIMARY KEY,
    package_id bigint NOT NULL REFERENCES package(id) ON DELETE CASCADE,
    version text NOT NULL,
    published_at timestamptz,
    license text,
    is_deprecated boolean NOT NULL DEFAULT false,
    source text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (package_id, version)
);

CREATE INDEX package_version_package_idx ON package_version (package_id);

CREATE TABLE dependency (
    id bigserial PRIMARY KEY,
    package_version_id bigint NOT NULL
        REFERENCES package_version(id) ON DELETE CASCADE,
    dependency_name text NOT NULL,
    dependency_normalized_name text NOT NULL,
    declared_constraint text,
    dependency_type text NOT NULL DEFAULT 'runtime',
    optional boolean NOT NULL DEFAULT false,
    UNIQUE (package_version_id, dependency_normalized_name, dependency_type)
);

CREATE INDEX dependency_name_idx ON dependency (dependency_normalized_name);
CREATE INDEX dependency_version_idx ON dependency (package_version_id);

CREATE TABLE vulnerability (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    source_id text NOT NULL,
    summary text,
    details text,
    severity text,
    cvss_vector text,
    cvss_score numeric(3, 1),
    published_at timestamptz,
    modified_at timestamptz,
    withdrawn_at timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_id)
);

CREATE TABLE vulnerability_alias (
    vulnerability_id bigint NOT NULL
        REFERENCES vulnerability(id) ON DELETE CASCADE,
    alias text NOT NULL,
    alias_type text NOT NULL DEFAULT 'cve',
    PRIMARY KEY (vulnerability_id, alias)
);

CREATE INDEX vulnerability_alias_alias_idx ON vulnerability_alias (alias);

CREATE TABLE affected_package_range (
    id bigserial PRIMARY KEY,
    vulnerability_id bigint NOT NULL
        REFERENCES vulnerability(id) ON DELETE CASCADE,
    ecosystem_id smallint NOT NULL REFERENCES ecosystem(id),
    package_name text NOT NULL,
    package_normalized_name text NOT NULL,
    range_type text NOT NULL,
    introduced text,
    fixed text,
    last_affected text,
    versions jsonb,
    source text NOT NULL,
    raw_range jsonb
);

CREATE INDEX affected_range_lookup_idx
    ON affected_package_range (ecosystem_id, package_normalized_name);

CREATE TABLE package_observation (
    id bigserial PRIMARY KEY,
    package_id bigint NOT NULL REFERENCES package(id) ON DELETE CASCADE,
    collection_run_id bigint REFERENCES collection_run(id),
    source text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    description text,
    homepage_url text,
    repository_url text,
    license text,
    latest_version text,
    downloads bigint
);

CREATE INDEX package_observation_package_idx
    ON package_observation (package_id, observed_at DESC);

CREATE TABLE download_observation (
    id bigserial PRIMARY KEY,
    package_id bigint NOT NULL REFERENCES package(id) ON DELETE CASCADE,
    collection_run_id bigint REFERENCES collection_run(id),
    source text NOT NULL,
    period text NOT NULL DEFAULT 'total',
    downloads bigint NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (package_id, period, source, observed_at)
);

CREATE INDEX download_observation_package_idx
    ON download_observation (package_id, observed_at DESC);
