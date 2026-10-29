-- Sampling metadata belongs to a collection run: the same package can be
-- popular in one run and part of the random comparison group in another.
CREATE TABLE sampled_package (
    collection_run_id bigint NOT NULL
        REFERENCES collection_run(id) ON DELETE CASCADE,
    package_id bigint NOT NULL REFERENCES package(id) ON DELETE CASCADE,
    sampling_category text NOT NULL
        CHECK (sampling_category IN ('popular', 'random')),
    sampling_rank integer,
    sampling_seed integer,
    PRIMARY KEY (collection_run_id, package_id)
);

CREATE INDEX sampled_package_package_idx ON sampled_package (package_id);
CREATE INDEX sampled_package_category_idx
    ON sampled_package (collection_run_id, sampling_category);
