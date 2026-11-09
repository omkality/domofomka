import os
import sqlite3
import uuid

import psycopg
import pytest
from litestar.testing import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from src.code_service.app import app
from src.code_service.config import settings
from src.code_service.v2 import search
from src.code_service.v2.engine import Catalog
from src.code_service.v2.normalize import legacy_house_matches, parse_query
from src.code_service.v2.postgres import PostgresCatalog
from tools.catalog import build_postgres_catalog as importer
from tools.catalog.build_catalog import SCHEMA, index_streets
from tools.catalog.compact_catalog import compact

# Explicit expectations: using the parser to generate these would hide its mistakes.
HOUSE_FORMS = [
    ("10", "10"),
    ("10А", "10а"),
    ("10 А", "10а"),
    ("10Б", "10б"),
    ("10 Б", "10б"),
    ("10к1", "10к1"),
    ("10 к 1", "10к1"),
    ("10 корпус 1", "10к1"),
    ("10 корп. 1", "10к1"),
    ("10к-1", "10к1"),
    ("10с-1", "10с1"),
    ("10к2", "10к2"),
    ("10с1", "10с1"),
    ("10 строение 1", "10с1"),
    ("10 стр. 1", "10с1"),
    ("10/1", "10/1"),
    ("10 / 1", "10/1"),
    ("10А корпус 1", "10ак1"),
    ("10А к 1", "10ак1"),
    ("10к1А", "10к1а"),
    ("10к1 строение 2", "10к1с2"),
    ("10 корпус А", "10ка"),
    ("2-4", "2-4"),
    ("2 - 4", "2-4"),
    ("2–4", "2-4"),
    ("24", "24"),
    ("2/4", "2/4"),
    ("18-20 к1", "18-20к1"),
    ("1820к1", "1820к1"),
    ("10-А", "10а"),
    ("8-10-12", "8-10-12"),
    ("81012", "81012"),
    ("10-Д", "10д"),
    ("10Д", "10д"),
    ("17с1Б-Г", "17с1б-г"),
    ("17с1БГ", "17с1бг"),
    ("71к4Г-Д", "71к4г-д"),
    ("71к4Г", "71к4г"),
    ("70А-70Б", "70а-70б"),
    ("70А-20ББ", "70а-20бб"),
    ("10 корпус Б-В", "10кб-в"),
    ("10кБВ", "10кбв"),
    ("1", "1"),
    ("2", "2"),
    ("11", "11"),
    ("36", "36"),
    ("к36", "к36"),
    ("корпус 36", "к36"),
    ("вл36", "вл36"),
    ("36к1", "36к1"),
    ("36А", "36а"),
    ("20к1", "20к1"),
]
HOUSES = sorted({house for _, house in HOUSE_FORMS})


def write_houses(path):
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for house in HOUSES:
            db.execute(
                "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    house,
                    "guid-" + house,
                    "Москва, улица Тестовая, дом " + house,
                    "Москва",
                    "Тестовая",
                    house,
                    "тестовая",
                    "москва",
                    "77",
                    "москва",
                ),
            )
            db.execute(
                "INSERT INTO codes VALUES(?,?,?,?)",
                (house, "1", "test", "ONLY-" + house),
            )
        index_streets(db)


@pytest.fixture
def neighboring_houses(tmp_path):
    path = tmp_path / "houses.db"
    write_houses(path)
    return Catalog(str(path))


@pytest.mark.parametrize(("spelling", "expected"), HOUSE_FORMS)
def test_house_components_select_only_the_requested_house(neighboring_houses, spelling, expected):
    query = "Москва, улица Тестовая, дом " + spelling
    assert parse_query(query).house == expected
    answer = neighboring_houses.resolve(query)
    assert answer["status"] == "found"
    assert answer["building_id"] == expected
    assert answer["data"] == {"1": [["ONLY-" + expected, "test"]]}


@pytest.mark.parametrize(("spelling", "expected"), HOUSE_FORMS)
def test_house_without_codes_does_not_borrow_codes_from_neighbors(neighboring_houses, spelling, expected):
    with sqlite3.connect(neighboring_houses.path) as db:
        db.execute("DELETE FROM codes WHERE building_id=?", (expected,))
    answer = neighboring_houses.resolve("Москва, улица Тестовая, дом " + spelling)
    assert answer["status"] == "no_codes"
    assert answer["building_id"] == expected
    assert answer["data"] == {}


@pytest.mark.parametrize(("spelling", "expected"), HOUSE_FORMS)
def test_legacy_guard_distinguishes_all_neighboring_house_components(spelling, expected):
    query = "Москва Тестовая " + spelling
    for returned in HOUSES:
        address = "Москва, улица Тестовая, дом " + returned
        assert legacy_house_matches(query, address) == (returned == expected), (
            spelling,
            returned,
        )


@pytest.mark.parametrize("corpus", [4, 7, 9])
def test_legacy_guard_preserves_explicit_quarter_with_corpus_before_it(corpus):
    query = f"Тестовый бульвар к{corpus} квартал 137А"
    address = "Москва, квартал Тестовый Бульвар 137А, дом к"
    assert legacy_house_matches(query, address + str(corpus))
    assert not legacy_house_matches(query, address + str(corpus + 1))
    assert not legacy_house_matches(query, address.replace("137А", "137Б") + str(corpus))
    assert not legacy_house_matches(query, "Москва, улица Тестовая, дом к" + str(corpus))


@pytest.mark.parametrize("catalog_available", [True, False])
def test_http_fallback_cannot_change_a_house_letter_corpus_or_structure(monkeypatch, catalog_available):
    class MissingResult:
        def resolve(self, message, candidates):
            return {"status": "not_found", "engine": "v2", "data": {}}

    def catalog():
        if not catalog_available:
            raise TimeoutError("Injected catalog outage")
        return MissingResult()

    returned_house = ""

    async def legacy(message, *, accept_source):
        return {
            "address": "Москва, улица Тестовая, дом " + returned_house,
            "data": {"1": [["ONLY-" + returned_house, "test"]]},
        }

    monkeypatch.setattr(settings, "search_cache_ttl_seconds", 0)
    monkeypatch.setattr(settings, "opensearch_url", None)
    monkeypatch.setattr(search, "get_catalog", catalog)
    monkeypatch.setattr(search, "get_data_from_db", legacy)
    with TestClient(app) as client:
        for requested in HOUSES:
            for returned in HOUSES:
                returned_house = returned
                response = client.get(
                    "/api/v2/codes/msg",
                    params={"message": "Москва Тестовая " + requested},
                )
                assert response.status_code == 200
                answer = response.json()
                assert answer["fallback_reason"] == ("not_found" if catalog_available else "unavailable")
                if requested == returned:
                    assert answer["status"] == "found"
                    assert answer["engine"] == "v1"
                    assert answer["data"] == {"1": [["ONLY-" + requested, "test"]]}
                else:
                    assert answer["status"] == "not_found", (
                        requested,
                        returned,
                        answer,
                    )
                    assert answer["fallback_rejected"] == "house_conflict"
                    assert answer["data"] == {}


def test_real_postgres_and_http_preserve_house_components_and_empty_neighbors(tmp_path, monkeypatch):
    dsn = os.getenv("TEST_QUEUE_DATABASE_URL")
    if not dsn:
        pytest.skip("Requires isolated real PostgreSQL")
    connection = conninfo_to_dict(dsn)
    if connection.get("host") not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("House identity tests only create isolated local databases")
    name = "domofomka_catalog_bench_house_" + uuid.uuid4().hex[:16]
    connection["dbname"] = name
    test_dsn = make_conninfo(**connection)
    source, serving = tmp_path / "full.db", tmp_path / "serving.db"
    write_houses(source)
    empty_houses = {"10", "10а"}
    with sqlite3.connect(source) as db:
        db.executemany(
            "DELETE FROM codes WHERE building_id=?",
            [(house,) for house in empty_houses],
        )
    compact(source, serving)

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        try:
            monkeypatch.setenv("CATALOG_BUILD_DSN", test_dsn)
            importer.load(serving, source, tmp_path / "report")
            reader = PostgresCatalog(test_dsn)
            try:
                monkeypatch.setattr(search, "get_catalog", lambda: reader)
                monkeypatch.setattr(settings, "opensearch_url", None)
                monkeypatch.setattr(settings, "search_cache_ttl_seconds", 0)

                async def unexpected_legacy(message):
                    raise AssertionError("A resolved house must not be replaced by legacy search")

                monkeypatch.setattr(search, "get_data_from_db", unexpected_legacy)
                with TestClient(app) as client:
                    for spelling, expected in HOUSE_FORMS:
                        response = client.get(
                            "/api/v2/codes/msg",
                            params={"message": "Москва, улица Тестовая, дом " + spelling},
                        )
                        assert response.status_code == 200
                        answer = response.json()
                        assert answer["engine"] == "v2"
                        assert answer["address"] == "Москва, улица Тестовая, дом " + expected
                        if expected in empty_houses:
                            assert answer["status"] == "no_codes"
                            assert answer["data"] == {}
                        else:
                            assert answer["status"] == "found"
                            assert answer["data"] == {"1": [["ONLY-" + expected, "test"]]}
            finally:
                reader.close()
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH(FORCE)").format(sql.Identifier(name)))
