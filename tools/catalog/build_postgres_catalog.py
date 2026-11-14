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
import sqlparse
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from src.infrastructure.database.migrate import apply
from src.infrastructure.database.resources import migration_sql

SCHEMA = migration_sql("catalog", "0001-catalog-tables")
INDEXES = list(sqlparse.split(migration_sql("catalog", "0002-catalog-indexes")))


def connect():
    """Открыть соединение с явно выбранной локальной БД подготовки данных."""
    dsn = os.environ.get("CATALOG_BUILD_DSN") or os.environ["CATALOG_BENCHMARK_DSN"]
    info = conninfo_to_dict(dsn)
    if not info.get("dbname", "").startswith(("domofomka_catalog_build_", "domofomka_catalog_bench")) or info.get(
        "host"
    ) not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("Catalog builds only write an isolated local database")
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def copy_table(source: sqlite3.Connection, target, table: str, select: str, types: list[str]):
    """Потоково перенести строки таблицы бинарным COPY и измерить длительность."""
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
    """Загрузить подготовленные данные в отдельную локальную БД и проверить публикацию."""
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with connect() as db:
        if db.execute(r"""
            SELECT
              to_regclass('public.buildings') AS name
            """).fetchone()["name"]:
            raise FileExistsError("Preserve the loaded experiment; choose another isolated database")
        with closing(sqlite3.connect(f"file:{catalog.resolve().as_posix()}?mode=ro", uri=True)) as source:
            dsn = os.environ.get("CATALOG_BUILD_DSN") or os.environ["CATALOG_BENCHMARK_DSN"]
            apply(dsn, "catalog", through="0001-catalog-tables")
            counts = {}
            for table, select, types in [
                ("buildings", "SELECT * FROM buildings", ["text"] * 10),
                ("codes", "SELECT * FROM codes", ["text"] * 4),
                ("aliases", "SELECT * FROM aliases", ["text"] * 4),
                (
                    "streets",
                    r"""
                SELECT *
                                FROM streets
                                ORDER BY street_key COLLATE BINARY,city_key COLLATE BINARY,id
                """,
                    ["text"] * 4 + ["int4"],
                ),
                ("street_houses", "SELECT * FROM street_houses", ["text", "bytea"]),
                (
                    "alias_search",
                    r"""
                SELECT
                  rowid,
                  building_id,
                  house,
                  words
                FROM
                  alias_search
                """,
                    ["int8", "text", "text", "text"],
                ),
                ("metadata", "SELECT * FROM metadata", ["text"] * 2),
                ("vocabulary", "SELECT term FROM vocabulary", ["text"]),
            ]:
                counts[table] = copy_table(source, db, table, select, types)
        with closing(sqlite3.connect(f"file:{full_catalog.resolve().as_posix()}?mode=ro", uri=True)) as source:
            expected = source.execute(r"""
                SELECT
                  value
                FROM
                  metadata
                WHERE
                  key = 'build'
                """).fetchone()
            actual = db.execute(r"""
                SELECT
                  value
                FROM
                  metadata
                WHERE
                  key = 'build'
                """).fetchone()
            if (json.loads(expected[0]) if expected else None) != (json.loads(actual["value"]) if actual else None):
                raise ValueError("Compact catalog and provenance belong to different builds")
            counts["provenance"] = copy_table(
                source, db, "provenance", "SELECT * FROM provenance", ["int8", "text", "text"]
            )
        apply(dsn, "catalog")
        db.execute("ANALYZE")
        db.execute("CHECKPOINT")
        with catalog.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        db.execute(
            r"""
            INSERT INTO metadata VALUES ('source_catalog_sha256', %s)
            """,
            (digest,),
        )
        db.execute(r"""
            INSERT INTO metadata VALUES ('catalog_schema_version', '1')
            """)
        db.execute(r"""
            INSERT INTO metadata VALUES ('publication_state', 'ready')
            """)
        report = {
            "source_sha256": digest,
            "source_bytes": catalog.stat().st_size,
            "rows": counts,
            "seconds": round(time.perf_counter() - started, 2),
            "database_bytes": db.execute(r"""
            SELECT
              pg_database_size(current_database()) AS n
            """).fetchone()["n"],
            "tables": db.execute(r"""
            SELECT
              relname,
              pg_total_relation_size(relid) AS total_bytes,
              pg_indexes_size(relid) AS index_bytes
            FROM
              pg_stat_user_tables
            ORDER BY
              relname
            """).fetchall(),
            "settings": {
                key: db.execute("SHOW " + key).fetchone()[key]
                for key in ("shared_buffers", "work_mem", "maintenance_work_mem", "max_connections")
            },
            "note": "Новая отдельная БД с проверенными связями. Сжатые дома ГАР и источник v1 сохраняются.",
        }
        (output / "load-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    """Подготовить новую локальную БД каталога для проверенного экспорта на dev."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--catalog", type=Path, default=Path("artifacts/search/serving.db"))
    p.add_argument("--full-catalog", type=Path, default=Path("artifacts/search/catalog.db"))
    p.add_argument("--output", type=Path, default=Path("artifacts/private/postgres-benchmark"))
    args = p.parse_args()
    load(args.catalog, args.full_catalog, args.output)


if __name__ == "__main__":
    main()
