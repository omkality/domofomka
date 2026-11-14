-- sqlfluff:dialect:sqlite
PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;
CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, level INTEGER);
CREATE TABLE houses (id INTEGER PRIMARY KEY, guid TEXT, number TEXT);
CREATE TABLE parents (id INTEGER PRIMARY KEY, parent INTEGER);
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
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
