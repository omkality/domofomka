CREATE TABLE buildings (
    id TEXT PRIMARY KEY,
    guid TEXT,
    display TEXT,
    city TEXT,
    street TEXT,
    house TEXT,
    street_key TEXT,
    city_key TEXT,
    region TEXT,
    lineage TEXT
);


CREATE TABLE codes (
    building_id TEXT NOT NULL,
    entrance TEXT NOT NULL,
    kind TEXT NOT NULL,
    code TEXT NOT NULL
);


CREATE TABLE aliases (
    building_id TEXT NOT NULL,
    city_key TEXT NOT NULL,
    street_key TEXT NOT NULL,
    house TEXT NOT NULL,
    PRIMARY KEY (
        building_id,
        city_key,
        street_key,
        house
    )
);


CREATE TABLE streets (
    id TEXT PRIMARY KEY,
    city_key TEXT, street_key TEXT, words TEXT, has_codes INT
);


CREATE TABLE street_houses (
    id TEXT PRIMARY KEY REFERENCES streets (id),
    DATA BYTEA
);


CREATE TABLE alias_search (
    seq BIGINT PRIMARY KEY,
    building_id TEXT NOT NULL REFERENCES buildings (id),
    house TEXT, words TEXT
);


CREATE TABLE provenance (
    source_id BIGINT PRIMARY KEY,
    building_id TEXT NOT NULL REFERENCES buildings (id),
    resolution TEXT
);


CREATE TABLE metadata (
    KEY TEXT PRIMARY KEY,
    value TEXT
);


CREATE TABLE vocabulary (term TEXT PRIMARY KEY);


CREATE TABLE entrances (
    building_id TEXT NOT NULL REFERENCES buildings (id), number TEXT NOT NULL,
    PRIMARY KEY (building_id, number)
);


CREATE TABLE code_sources (kind TEXT PRIMARY KEY);
