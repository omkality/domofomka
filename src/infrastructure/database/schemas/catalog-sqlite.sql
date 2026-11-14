-- sqlfluff:dialect:sqlite
PRAGMA journal_mode = OFF;

PRAGMA synchronous = OFF;


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


CREATE TABLE streets (
    id TEXT PRIMARY KEY,
    city_key TEXT, street_key TEXT, words TEXT
);


CREATE TABLE codes (building_id TEXT, entrance TEXT, kind TEXT, code TEXT, UNIQUE (building_id, entrance, kind, code));


CREATE INDEX codes_building ON codes (building_id);


CREATE TABLE provenance (
    source_id INTEGER PRIMARY KEY,
    building_id TEXT, resolution TEXT
);


CREATE TABLE aliases (
    building_id TEXT, city_key TEXT, street_key TEXT, house TEXT, UNIQUE (building_id, city_key, street_key, house)
);


CREATE VIRTUAL TABLE SEARCH USING fts5 (
    id UNINDEXED,
    words,
    tokenize = 'unicode61'
);


CREATE VIRTUAL TABLE vocabulary USING fts5vocab (
    SEARCH,
    'row'
);


CREATE TABLE metadata (
    KEY TEXT PRIMARY KEY,
    value TEXT
);
