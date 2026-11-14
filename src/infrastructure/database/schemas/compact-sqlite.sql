-- sqlfluff:dialect:sqlite
CREATE TABLE buildings AS
SELECT
  b.*
FROM
  (
    SELECT DISTINCT
      building_id
    FROM
      source.codes
  ) AS c
INNER JOIN
  source.buildings AS b
  ON b.id = c.building_id;
CREATE UNIQUE INDEX building_id ON buildings (id);
CREATE INDEX building_lookup ON buildings (street_key, house);
CREATE INDEX building_street ON buildings (city_key, street_key, house);
CREATE TABLE codes AS
SELECT
  *
FROM
  source.codes;
CREATE INDEX codes_building ON codes (building_id);
CREATE TABLE aliases AS
SELECT
  *
FROM
  source.aliases;
CREATE TABLE streets (
  id TEXT PRIMARY KEY,
  city_key TEXT, street_key TEXT, words TEXT, has_codes INTEGER
);
CREATE TABLE street_houses (
  id TEXT PRIMARY KEY,
  DATA BLOB
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
CREATE TABLE metadata AS
SELECT
  *
FROM
  source.metadata;
INSERT INTO metadata
VALUES ('format', 'compact');
