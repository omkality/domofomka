import os
import sqlite3
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from src.search.engine import Catalog
from src.search.normalize import text
from tools import postgres_catalog_experiment as importer
from tools.build_catalog import SCHEMA, index_streets
from tools.compact_catalog import compact
from tools.postgres_search_experiment import PostgresExperiment


@pytest.fixture
def local_experiment(tmp_path, monkeypatch):
    dsn = os.getenv("TEST_QUEUE_DATABASE_URL")
    if not dsn:
        pytest.skip("Requires isolated real PostgreSQL")
    name = "domofomka_catalog_bench_" + uuid.uuid4().hex[:16]
    connection = conninfo_to_dict(dsn)
    connection["dbname"] = name
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        monkeypatch.setenv("CATALOG_BENCHMARK_DSN", make_conninfo(**connection))
        try:
            source = tmp_path / "full.db"
            with sqlite3.connect(source) as db:
                db.executescript(SCHEMA)
                for bid, city, street, house in [
                    ("a", "Москва", "Перерва", "62к1"),
                    ("b", "Москва", "Перерва", "62к2"),
                    ("c", "Подольск", "Перерва", "62к1"),
                    ("d", "Москва", "1-я Парковая", "3"),
                    ("e", "Москва", "2-я Парковая", "3"),
                    ("f", "Москва", "Коломенская", "2"),
                    ("g", "Москва", "Коломенская", "2а"),
                ]:
                    db.execute(
                        "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            bid,
                            bid,
                            f"{city}, улица {street}, дом {house}",
                            city,
                            street,
                            house,
                            text(street),
                            text(city),
                            "77",
                            text(city),
                        ),
                    )
                    db.execute("INSERT INTO aliases VALUES(?,?,?,?)", (bid, text(city), text(street), house))
                db.executemany(
                    "INSERT INTO codes VALUES(?,?,?,?)",
                    [
                        ("a", "2", "source", "CODE-A"),
                        ("a", "2", "source", "CODE-B"),
                        ("c", "1", "source", "OTHER-CITY"),
                        ("d", "1", "source", "FIRST"),
                        ("e", "1", "source", "SECOND"),
                        ("g", "1", "source", "NEIGHBOUR"),
                    ],
                )
                # Compact aliases must only refer to actual code-bearing rows.
                db.execute("DELETE FROM aliases WHERE building_id IN ('b','f')")
                db.execute("INSERT INTO provenance VALUES(1,'a','gar_exact')")
                index_streets(db)
            serving = tmp_path / "serving.db"
            compact(source, serving)
            importer.load(serving, source, tmp_path / "report")
            yield serving
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH(FORCE)").format(sql.Identifier(name)))


def test_postgres_preserves_cities_numbered_streets_no_codes_choices_and_code_order(local_experiment):
    sqlite = Catalog(str(local_experiment))
    postgres = PostgresExperiment()
    try:
        for query in [
            "Перерва 62к1",
            "Москва Перерва 62к1",
            "Подольск Перерва 62к1",
            "Москва Перерва 62к2",
            "Москва первая Парковая 3",
            "Москва вторая Парковая 3",
            "Москва Коломенская 2",
            "Москва Коломенская 2а",
            "Москва Перева 62к1",
            "третий Покровский проезд",
        ]:
            assert postgres.resolve(query) == sqlite.resolve(query), query
        assert postgres.resolve("Москва Перерва 62к2")["status"] == "no_codes"
        assert postgres.resolve("Москва Коломенская 2")["status"] == "no_codes"
        assert postgres.resolve("Перерва 62к1")["status"] == "ambiguous"
        assert postgres.resolve("Москва Перерва 62к1")["data"]["2"] == [["CODE-A", "source"], ["CODE-B", "source"]]
    finally:
        postgres.close()


def test_experiment_has_foreign_keys_and_refuses_overwriting_loaded_data(local_experiment, tmp_path):
    with importer.connect() as db, pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("INSERT INTO codes VALUES('missing','1','source','INVALID')")
    with pytest.raises(FileExistsError):
        importer.load(local_experiment, tmp_path / "not-used.db", tmp_path / "new-report")


def test_experiment_rejects_non_benchmark_database(monkeypatch):
    dsn = os.getenv("TEST_QUEUE_DATABASE_URL")
    if not dsn:
        pytest.skip("Requires isolated real PostgreSQL")
    monkeypatch.setenv("CATALOG_BENCHMARK_DSN", dsn)
    with pytest.raises(ValueError, match="isolated local"):
        importer.connect()
