import concurrent.futures
import os
import sqlite3
import threading
import time
import uuid
from contextlib import suppress
from typing import Any
from unittest.mock import AsyncMock

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ops.deploy import street_layout_sql
from src.search.engine import Catalog
from src.search.normalize import text
from src.search.postgres import PostgresCatalog
from tools import build_postgres_catalog as importer
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
                    ("h", "Москва", "Большая Коломенская", "2"),
                    ("i", "Москва", "Самотечная", "11"),
                    ("j", "Щербинка", "Самотечная", "11"),
                    ("k", "Москва, Центральный район", "Самотечная", "11"),
                    ("m", "деревня Улица", "Зеленая", "9"),
                    ("n", "Нахабино", "Тестовая", "7"),
                ]:
                    db.execute(
                        "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            bid,
                            None if bid == "k" else bid,
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
                        ("h", "1", "source", "OTHER-STREET"),
                        ("i", "1", "source", "MAIN-LOCALITY"),
                        ("j", "2", "source", "PARENT-LOCALITY-ALIAS"),
                        ("k", "3", "source", "SOURCE-ADMINISTRATIVE-VARIANT"),
                        ("n", "1", "source", "PREFIX-CITY"),
                    ],
                )
                # Compact aliases must only refer to actual code-bearing rows.
                db.execute("DELETE FROM aliases WHERE building_id IN ('b','f','m')")
                db.execute("INSERT INTO aliases VALUES('j','москва щербинка','самотечная','11')")
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


def test_street_layout_preserves_rows_indexes_and_foreign_keys(local_experiment):
    with importer.connect() as db:
        before = db.execute("SELECT * FROM streets ORDER BY id").fetchall()
        index_query = "SELECT indexname,indexdef FROM pg_indexes WHERE tablename='streets' ORDER BY indexname"
        indexes = db.execute(index_query).fetchall()
        db.execute(street_layout_sql(180, 5, 16))
        assert db.execute("SELECT * FROM streets ORDER BY id").fetchall() == before
        assert db.execute(index_query).fetchall() == indexes
        actual = [row["street_key"] for row in db.execute("SELECT street_key FROM streets ORDER BY ctid")]
        assert actual == sorted(actual)
        assert db.execute("SELECT value FROM metadata WHERE key='street_layout'").fetchone()["value"] == "street-key-v1"
        count = db.execute("SELECT count(*) AS n FROM street_houses h JOIN streets s ON s.id=h.id").fetchone()["n"]
        assert count == len(before)


def test_new_catalog_load_groups_same_street_names_physically(local_experiment):
    with importer.connect() as db:
        actual = [row["street_key"] for row in db.execute("SELECT street_key FROM streets ORDER BY ctid")]
        assert actual == sorted(actual)


def test_failed_street_layout_rolls_back_metadata_and_temporary_index(local_experiment):
    with importer.connect() as db:
        db.execute("CLUSTER public.streets USING streets_pkey")
        before = db.execute("SELECT * FROM streets ORDER BY ctid").fetchall()
        statement = street_layout_sql(180, 5, 16).replace("ANALYZE public.streets;", "SELECT 1/0;")
        with pytest.raises(psycopg.errors.DivisionByZero):
            db.execute(statement)
        db.execute("ROLLBACK")
        assert db.execute("SELECT * FROM streets ORDER BY ctid").fetchall() == before
        assert not db.execute("SELECT value FROM metadata WHERE key='street_layout'").fetchone()
        assert db.execute("SELECT to_regclass('public.catalog_street_layout') AS name").fetchone()["name"] is None


def test_experiment_rejects_non_benchmark_database(monkeypatch):
    dsn = os.getenv("TEST_QUEUE_DATABASE_URL")
    if not dsn:
        pytest.skip("Requires isolated real PostgreSQL")
    monkeypatch.setenv("CATALOG_BENCHMARK_DSN", dsn)
    with pytest.raises(ValueError, match="isolated local"):
        importer.connect()


def production_reader(**options: Any):
    return PostgresCatalog(os.environ["CATALOG_BENCHMARK_DSN"], **options)


def test_bounded_reader_preserves_results_and_compact_building_ids(local_experiment):
    sqlite = Catalog(str(local_experiment))
    reader = production_reader()
    try:
        empty = reader.resolve("Москва Коломенская 2")
        assert empty["status"] == "no_codes"
        identity = reader.building(empty["building_id"])
        assert identity["street"] == "Коломенская"
        assert identity["house"] == "2"
        for query in [
            "Перерва 62к1",
            "Москва Перерва 62к1",
            "Москва Перева 62к1",
            "Москва первая Парковая 3",
            "Москва Коломенская 2",
            "Москва Коломенская 2а",
        ]:
            actual = reader.resolve(query)
            assert actual == sqlite.resolve(query)
            for item in actual.get("candidates", []) or ([actual] if actual.get("building_id") else []):
                assert reader.building(item["building_id"]) == sqlite.building(item["building_id"])
    finally:
        reader.close()


def test_compact_expansion_batches_street_reads_and_preserves_empty_houses(local_experiment, monkeypatch):
    from src.search.postgres import SharedReader

    reader = production_reader()
    statements = []
    original = SharedReader.execute

    def tracked(self, statement, parameters=()):
        statements.append(statement)
        return original(self, statement, parameters)

    monkeypatch.setattr(SharedReader, "execute", tracked)
    try:
        with reader.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM streets ORDER BY id")]
            statements.clear()
            rows = reader._expand(db, ids, "2")
        assert len(statements) == 2
        assert any(row["id"].startswith("address:") for row in rows)
        with Catalog(str(local_experiment)).connect() as db:
            assert rows == Catalog(str(local_experiment))._expand(db, ids, "2")
    finally:
        reader.close()


def test_postgres_prefix_clarification_matches_sqlite_and_requires_confirmation(local_experiment):
    reader = production_reader()
    try:
        assert not reader.has_exact_locality_word("наха")
        assert reader.has_exact_locality_word("нахабино")
        result = reader.resolve("наха тестовая 7")
        assert result == Catalog(str(local_experiment)).resolve("наха тестовая 7")
        assert result["status"] == "ambiguous"
        assert [row["building_id"] for row in result["candidates"]] == ["n"]
        assert reader.resolve("Нахабино Тестовая 7")["status"] == "found"
    finally:
        reader.close()


def test_reader_preserves_distinct_codes_for_administrative_variants_and_parent_locality_aliases(local_experiment):
    sqlite = Catalog(str(local_experiment))
    reader = production_reader()
    try:
        result = reader.resolve("Москва Самотечная 11")
        assert result == sqlite.resolve("Москва Самотечная 11")
        assert result["status"] == "ambiguous"
        assert {option["building_id"] for option in result["candidates"]} == {"i", "j", "k"}
        codes = {
            code
            for option in result["candidates"]
            for items in reader.answer(reader.building(option["building_id"]))["data"].values()
            for code, _ in items
        }
        assert codes == {"MAIN-LOCALITY", "PARENT-LOCALITY-ALIAS", "SOURCE-ADMINISTRATIVE-VARIANT"}
        assert reader.resolve("Щербинка Самотечная 11")["building_id"] == "j"
        assert reader.resolve("Москва Самотечная 11к1")["status"] == "not_found"
        empty = reader.resolve("деревня Улица улица Зеленая 9")
        assert empty["status"] == "no_codes"
        assert empty["building_id"].startswith("address:")
        assert empty == sqlite.resolve("деревня Улица улица Зеленая 9")
    finally:
        reader.close()


def test_reader_reuses_one_connection_and_serializes_parallel_requests(local_experiment):
    reader = production_reader()
    try:
        expected = reader.resolve("Москва Перерва 62к1")
        with reader.connect() as db:
            pid = db.execute("SELECT pg_backend_pid()").fetchone()[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(reader.resolve, ["Москва Перерва 62к1"] * 16))
        assert all(result == expected for result in results)
        with reader.connect() as db:
            assert db.execute("SELECT pg_backend_pid()").fetchone()[0] == pid
            assert db.connection.autocommit
            assert db.connection.pgconn.transaction_status == psycopg.pq.TransactionStatus.IDLE
    finally:
        reader.close()


def test_reader_is_read_only_and_recovers_after_rejected_write(local_experiment):
    reader = production_reader()
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), reader.connect() as db:
            db.execute("DELETE FROM codes")
        assert reader.resolve("Москва Перерва 62к1")["data"]["2"][0][0] == "CODE-A"
    finally:
        reader.close()


@pytest.mark.parametrize(("statement_ms", "operation_seconds"), [(50, 2), (2000, 0.15)])
def test_server_and_client_deadlines_close_connection_and_allow_recovery(
    local_experiment, statement_ms, operation_seconds
):
    reader = production_reader(statement_timeout_ms=statement_ms, operation_timeout_seconds=operation_seconds)
    try:
        # Warm connection/metadata before measuring only the deliberately blocked command.
        reader.resolve("Москва Перерва 62к1")
        started = time.monotonic()
        with pytest.raises((psycopg.OperationalError, TimeoutError)), reader.connect() as db:
            db.execute("SELECT pg_sleep(1)")
        assert time.monotonic() - started < 0.8
        assert reader.resolve("Москва Перерва 62к1")["status"] == "found"
    finally:
        reader.close()


def test_busy_reader_does_not_close_another_active_operation(local_experiment):
    reader = production_reader(lock_timeout_seconds=0.05)
    held, release = threading.Event(), threading.Event()

    def hold():
        with reader.connect():
            held.set()
            assert release.wait(2)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(hold)
            assert held.wait(2)
            with pytest.raises(TimeoutError, match="busy"):
                reader.resolve("Москва Перерва 62к1")
            release.set()
            task.result(timeout=2)
        assert reader.resolve("Москва Перерва 62к1")["status"] == "found"
    finally:
        release.set()
        reader.close()


def test_reader_reconnects_after_postgres_terminates_its_session(local_experiment):
    reader = production_reader()
    try:
        with reader.connect() as db:
            pid = db.execute("SELECT pg_backend_pid()").fetchone()[0]
        with importer.connect() as admin:
            assert admin.execute("SELECT pg_terminate_backend(%s) AS stopped", (pid,)).fetchone()["stopped"]
        with suppress(psycopg.OperationalError):
            reader.resolve("Москва Перерва 62к1")
        assert reader.resolve("Москва Перерва 62к1")["status"] == "found"
        with reader.connect() as db:
            assert db.execute("SELECT pg_backend_pid()").fetchone()[0] != pid
    finally:
        reader.close()


def test_reader_rejects_another_dataset_revision(local_experiment):
    reader = production_reader(revision="different-revision")
    try:
        with pytest.raises(ValueError, match="revision"):
            reader.resolve("Москва Перерва 62к1")
    finally:
        reader.close()


def test_http_postgres_cache_without_sqlite_catalog_and_building_endpoint(local_experiment, monkeypatch, tmp_path):
    from litestar.testing import TestClient
    from pydantic import SecretStr

    from src.api.app import app
    from src.application import search
    from src.config import settings

    monkeypatch.setattr(settings, "search_backend", "postgres")
    monkeypatch.setattr(settings, "catalog_database_url", SecretStr(os.environ["CATALOG_BENCHMARK_DSN"]))
    monkeypatch.setattr(settings, "catalog_revision", "unknown")
    monkeypatch.setattr(settings, "search_catalog_path", str(tmp_path / "missing-serving.db"))
    source = tmp_path / "legacy.db"
    source.touch()
    monkeypatch.setattr(settings, "db_name", str(source))
    monkeypatch.setattr(settings, "opensearch_url", None)
    monkeypatch.setattr(settings, "search_cache_ttl_seconds", 120)
    fallback = AsyncMock(return_value={"data": {}})
    monkeypatch.setattr(search, "get_data_from_db", fallback)
    search.result_cache.entries.clear()
    with TestClient(app) as client:
        first = client.get("/api/v2/codes/msg", params={"message": "Москва Перерва 62к1"}).json()
        reader = search.get_catalog()
        second = client.get("/api/v2/codes/msg", params={"message": "Москва Перерва 62к1"}).json()
        assert first == second
        assert first["engine"] == "v2"
        assert len(search.result_cache.entries) == 1
        assert search.get_catalog() is reader
        building = client.get("/api/v2/buildings/a").json()
        assert building["data"] == first["data"]
        assert client.get("/api/v2/buildings/missing").status_code == 404
        fallback.assert_not_called()
        monkeypatch.setattr(settings, "catalog_database_url", SecretStr("postgresql://unused@127.0.0.1:1/unavailable"))
        monkeypatch.setattr(settings, "search_cache_ttl_seconds", 0)
        assert client.get("/api/v2/buildings/a").status_code == 503
        answer = client.get("/api/v2/codes/msg", params={"message": "Москва Перерва 62к1"}).json()
        assert answer["engine"] == "v1"
        assert answer["fallback_reason"] == "unavailable"
        fallback.assert_awaited_once()


def test_runtime_postgres_reader_reloads_configured_candidate_budget(local_experiment, monkeypatch):
    from pydantic import SecretStr

    from src.config import settings
    from src.services.catalog import close_catalog, get_catalog

    monkeypatch.setattr(settings, "search_backend", "postgres")
    monkeypatch.setattr(settings, "catalog_database_url", SecretStr(os.environ["CATALOG_BENCHMARK_DSN"]))
    monkeypatch.setattr(settings, "catalog_revision", "unknown")
    monkeypatch.setattr(settings, "search_candidate_limit", 5)
    close_catalog()
    try:
        first = get_catalog()
        assert first.candidate_limit == 5
        assert first.resolve("Москва Перерва 62к1")["building_id"] == "a"
        monkeypatch.setattr(settings, "search_candidate_limit", 200)
        second = get_catalog()
        assert second is not first
        assert second.candidate_limit == 200
        assert first._connection is None
        assert second.resolve("Москва Перерва 62к1")["building_id"] == "a"
    finally:
        close_catalog()
