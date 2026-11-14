-- sqlfluff:dialect:sqlite
PRAGMA journal_mode = OFF;
PRAGMA synchronous = OFF;

CREATE TABLE "groups" (
  id INTEGER PRIMARY KEY,
  city TEXT,
  kind TEXT,
  street TEXT,
  house TEXT,
  words TEXT
);

CREATE TABLE source_rows (
  group_id INTEGER,
  source_id INTEGER PRIMARY KEY
);

CREATE INDEX source_group ON source_rows (group_id);

CREATE TABLE metadata (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE VIRTUAL TABLE legacy_search USING fts5 (
  words,
  content = 'groups',
  content_rowid = 'id',
  tokenize = 'trigram'
);
