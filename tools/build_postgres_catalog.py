"""Подготовить новую локальную БД каталога для проверенного экспорта на dev."""

import argparse
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE buildings(id TEXT PRIMARY KEY,guid TEXT,display TEXT,city TEXT,street TEXT,house TEXT,
    street_key TEXT,city_key TEXT,region TEXT,lineage TEXT);
CREATE TABLE codes(building_id TEXT NOT NULL,entrance TEXT NOT NULL,kind TEXT NOT NULL,code TEXT NOT NULL);
CREATE TABLE aliases(building_id TEXT NOT NULL,city_key TEXT NOT NULL,street_key TEXT NOT NULL,house TEXT NOT NULL,
    PRIMARY KEY(building_id,city_key,street_key,house));
CREATE TABLE streets(id TEXT PRIMARY KEY,city_key TEXT,street_key TEXT,words TEXT,has_codes INT);
CREATE TABLE street_houses(id TEXT PRIMARY KEY REFERENCES streets(id),data BYTEA);
CREATE TABLE alias_search(seq BIGINT PRIMARY KEY,building_id TEXT NOT NULL REFERENCES buildings(id),house TEXT,words TEXT);
CREATE TABLE provenance(source_id BIGINT PRIMARY KEY,building_id TEXT NOT NULL REFERENCES buildings(id),resolution TEXT);
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE vocabulary(term TEXT PRIMARY KEY);
CREATE TABLE entrances(building_id TEXT NOT NULL REFERENCES buildings(id),number TEXT NOT NULL,PRIMARY KEY(building_id,number));
CREATE TABLE code_sources(kind TEXT PRIMARY KEY);
"""
INDEXES = [
    "CREATE INDEX building_lookup ON buildings(street_key,house)",
    "CREATE INDEX building_street ON buildings(city_key,street_key,house)",
    "ALTER TABLE codes ADD PRIMARY KEY(building_id,entrance,kind,code)",
    "CREATE INDEX aliases_building ON aliases(building_id)",
    "CREATE INDEX alias_house ON alias_search(house)",
    "CREATE INDEX street_words ON streets USING GIN(to_tsvector('simple',words)) WITH(fastupdate=off)",
    "CREATE INDEX coded_street_words ON streets USING GIN(to_tsvector('simple',words)) WITH(fastupdate=off) WHERE has_codes=1",
    "CREATE INDEX alias_words ON alias_search USING GIN(to_tsvector('simple',words)) WITH(fastupdate=off)",
    "INSERT INTO entrances SELECT DISTINCT building_id,entrance FROM codes",
    "INSERT INTO code_sources SELECT DISTINCT kind FROM codes",
    "ALTER TABLE codes ADD FOREIGN KEY(building_id,entrance) REFERENCES entrances(building_id,number)",
    "ALTER TABLE codes ADD FOREIGN KEY(kind) REFERENCES code_sources(kind)",
    "ALTER TABLE aliases ADD FOREIGN KEY(building_id) REFERENCES buildings(id)",
]


def connect():
    dsn = os.environ.get("CATALOG_BUILD_DSN") or os.environ["CATALOG_BENCHMARK_DSN"]
    info = conninfo_to_dict(dsn)
    if not info.get("dbname", "").startswith(("domofomka_catalog_build_", "domofomka_catalog_bench")) or info.get("host") not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("Catalog builds only write an isolated local database")
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def copy_table(source: sqlite3.Connection, target, table: str, select: str, types: list[str]):
    started = time.perf_counter()
    count = 0
    with target.cursor().copy(f"COPY {table} FROM STDIN WITH(FORMAT BINARY)") as sink:
        sink.set_types(types)
        for row in source.execute(select):
            sink.write_row(row)
            count += 1
    print(json.dumps({"table": table, "rows": count, "seconds": round(time.perf_counter() - started, 2)}), flush=True)
    return count


def load(catalog: Path, full_catalog: Path, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with connect() as db:
        if db.execute("SELECT to_regclass('public.buildings') AS name").fetchone()["name"]:
            raise FileExistsError("Preserve the loaded experiment; choose another isolated database")
        with closing(sqlite3.connect(f"file:{catalog.resolve().as_posix()}?mode=ro", uri=True)) as source:
            db.execute(SCHEMA)
            counts = {}
            for table, select, types in [
                ("buildings", "SELECT * FROM buildings", ["text"] * 10),
                ("codes", "SELECT * FROM codes", ["text"] * 4),
                ("aliases", "SELECT * FROM aliases", ["text"] * 4),
                ("streets", 'SELECT * FROM streets ORDER BY street_key COLLATE BINARY,city_key COLLATE BINARY,id',
                 ["text"] * 4 + ["int4"]),
                ("street_houses", "SELECT * FROM street_houses", ["text", "bytea"]),
                (
                    "alias_search",
                    "SELECT rowid,building_id,house,words FROM alias_search",
                    ["int8", "text", "text", "text"],
                ),
                ("metadata", "SELECT * FROM metadata", ["text"] * 2),
                ("vocabulary", "SELECT term FROM vocabulary", ["text"]),
            ]:
                counts[table] = copy_table(source, db, table, select, types)
        with closing(sqlite3.connect(f"file:{full_catalog.resolve().as_posix()}?mode=ro", uri=True)) as source:
            expected = source.execute("SELECT value FROM metadata WHERE key='build'").fetchone()
            actual = db.execute("SELECT value FROM metadata WHERE key='build'").fetchone()
            if (json.loads(expected[0]) if expected else None) != (json.loads(actual["value"]) if actual else None):
                raise ValueError("Compact catalog and provenance belong to different builds")
            counts["provenance"] = copy_table(
                source, db, "provenance", "SELECT * FROM provenance", ["int8", "text", "text"]
            )
        for statement in INDEXES:
            mark = time.perf_counter()
            db.execute(statement)
            print(
                json.dumps({"operation": statement.split(" ON ")[0], "seconds": round(time.perf_counter() - mark, 2)}),
                flush=True,
            )
        db.execute("ANALYZE")
        db.execute("CHECKPOINT")
        with catalog.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        db.execute("INSERT INTO metadata VALUES('source_catalog_sha256',%s)", (digest,))
        db.execute("INSERT INTO metadata VALUES('catalog_schema_version','1')")
        db.execute("INSERT INTO metadata VALUES('publication_state','ready')")
        report = {
            "source_sha256": digest,
            "source_bytes": catalog.stat().st_size,
            "rows": counts,
            "seconds": round(time.perf_counter() - started, 2),
            "database_bytes": db.execute("SELECT pg_database_size(current_database()) AS n").fetchone()["n"],
            "tables": db.execute("""SELECT relname,pg_total_relation_size(relid) AS total_bytes,
                pg_indexes_size(relid) AS index_bytes FROM pg_stat_user_tables ORDER BY relname""").fetchall(),
            "settings": {
                key: db.execute("SHOW " + key).fetchone()[key]
                for key in ("shared_buffers", "work_mem", "maintenance_work_mem", "max_connections")
            },
            "note": "Новая отдельная БД с проверенными связями. Сжатые дома ГАР и источник v1 сохраняются.",
        }
        (output / "load-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--catalog", type=Path, default=Path("artifacts/search/serving.db"))
    p.add_argument("--full-catalog", type=Path, default=Path("artifacts/search/catalog.db"))
    p.add_argument("--output", type=Path, default=Path("artifacts/private/postgres-benchmark"))
    args = p.parse_args()
    load(args.catalog, args.full_catalog, args.output)


if __name__ == "__main__":
    main()
