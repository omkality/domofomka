import sqlite3
from functools import partial

import pytest

from src.config import settings
from src.search.constraints import legacy_source_matches
from src.services.codes import get_data_from_db, scan_data_from_db
from src.services.source_index import lookup
from tools.build_legacy_index import build


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["перерва 62к1", "Перерва 62к1", "москва перерва 62к1", "нет 99", "рва 62к1", "14", "", "улица 9 мая 1"]
)
async def test_index_preserves_codes_order_duplicates_and_legacy_matching(tmp_path, monkeypatch, query):
    source, index = tmp_path / "source.db", tmp_path / "index.db"
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE codes(id INTEGER PRIMARY KEY,city TEXT,street_type TEXT,street TEXT,house TEXT,"
            "entrance TEXT,code_type TEXT,code TEXT)"
        )
        db.executemany(
            "INSERT INTO codes VALUES(?,?,?,?,?,?,?,?)",
            [
                (1, "Москва", "улица", "Перерва", "62к1", "2", "test", "A"),
                (2, "Москва, тест", "улица", "Перерва", "62к1", "2", "test", "B"),
                (3, "Москва", "улица", "Перерва", "62к1", "2", "test", "A"),
                (4, "Москва", "улица", "Перерва", "62к1", "3", "test", "C"),
                (5, "Другой город", "улица", "9 Мая", "1", "1", "test", "D"),
            ],
        )
    build(source, index)
    monkeypatch.setattr(settings, "db_name", str(source))
    monkeypatch.setattr(settings, "legacy_index_path", str(index))
    assert await get_data_from_db(query) == await scan_data_from_db(query)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE codes SET code='changed' WHERE id=1")
    assert lookup("перерва 62к1", str(source), str(index)) is None
    assert await get_data_from_db(query) == await scan_data_from_db(query)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_index", [False, True])
@pytest.mark.parametrize(
    ("query", "addresses", "whole_location", "retained"),
    [
        (
            "Москва 13-я парковая дом 4",
            [("Москва", "13-я Парковая", "4"), ("Москва", "Парковая 3-я", "14")],
            False,
            ["A"],
        ),
        (
            "Москва Самотечная 11",
            [("Москва", "Самотечная", "11"), ("Москва, другой район", "Самотечная", "11")],
            True,
            ["A"],
        ),
        (
            "Дзержинск Спортивная 10",
            [("Дзержинск", "Спортивная", "10"), ("Дзержинский", "Спортивная", "10")],
            False,
            ["A"],
        ),
        (
            "Парнас заречная 33",
            [("Санкт-Петербург", "ЗАРЕЧНАЯ", "ПАРНАС33"), ("Санкт-Петербург, ЗАРЕЧНАЯ УЛ.", "ПАРНАС", "33")],
            False,
            ["A", "B"],
        ),
    ],
)
async def test_opt_in_fallback_checks_each_source_group_without_changing_v1(
    tmp_path, monkeypatch, with_index, query, addresses, whole_location, retained
):
    source, index = tmp_path / "source.db", tmp_path / "index.db"
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE codes(id INTEGER PRIMARY KEY,city TEXT,street_type TEXT,street TEXT,house TEXT,"
            "entrance TEXT,code_type TEXT,code TEXT)"
        )
        db.executemany(
            "INSERT INTO codes VALUES(?,?,?,?,?,?,?,?)",
            [
                (n, city, "улица", street, house, "1", "test", code)
                for n, ((city, street, house), code) in enumerate(zip(addresses, ["A", "B"], strict=True), 1)
            ],
        )
    if with_index:
        build(source, index)
    monkeypatch.setattr(settings, "db_name", str(source))
    monkeypatch.setattr(settings, "legacy_index_path", str(index))
    original = await get_data_from_db(query)
    assert [code for code, _ in original["data"]["1"]] == ["A", "B"]
    accept = partial(legacy_source_matches, query, whole_location=whole_location)
    filtered = await get_data_from_db(query, accept_source=accept)
    assert [code for code, _ in filtered["data"]["1"]] == retained
    assert await get_data_from_db(query) == original


@pytest.mark.parametrize(
    ("query", "city", "street", "house", "matches"),
    [
        ("26ти Тестовых Комиссаров 10к2", "Москва", "26-ти Тестовых Комиссаров", "10к2", True),
        ("МоскваТестовая 25", "Москва", "Тестовая", "25", True),
        ("Москваулица Тестовая 25", "Москва", "Тестовая", "25", True),
        ("Тесто вая 25", "Москва", "Тестовая", "25", True),
        ("Тестовая 10 наха", "Нахабино", "Тестовая", "10", False),
        ("Дзержинск Тестовая 10", "Дзержинский", "Тестовая", "10", False),
        ("Тестовый бульвар 114а к2", "Москва", "Тестовый Бульвар 114А", "к2", True),
    ],
)
def test_source_guard_preserves_spaces_and_numeric_compatibility_without_accepting_city_prefixes(
    query, city, street, house, matches
):
    kind = "квартал" if house.startswith("к") else "улица"
    assert legacy_source_matches(query, city, kind, street, house) == matches


@pytest.mark.asyncio
@pytest.mark.parametrize("with_index", [False, True])
async def test_v2_catalog_failure_does_not_mix_another_house_into_a_valid_fallback(tmp_path, monkeypatch, with_index):
    from src.application import search

    source, index = tmp_path / "source.db", tmp_path / "index.db"
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE codes(id INTEGER PRIMARY KEY,city TEXT,street_type TEXT,street TEXT,house TEXT,"
            "entrance TEXT,code_type TEXT,code TEXT)"
        )
        db.executemany(
            "INSERT INTO codes VALUES(?,?,?,?,?,?,?,?)",
            [
                (1, "Москва", "улица", "13-я Парковая", "4", "1", "test", "VALID"),
                (2, "Москва", "улица", "Парковая 3-я", "14", "1", "test", "OTHER-HOUSE"),
            ],
        )
    if with_index:
        build(source, index)
    monkeypatch.setattr(settings, "db_name", str(source))
    monkeypatch.setattr(settings, "legacy_index_path", str(index))
    monkeypatch.setattr(settings, "opensearch_url", None)

    def unavailable():
        raise OSError("isolated catalog failure")

    monkeypatch.setattr(search, "get_catalog", unavailable)
    answer = await search.uncached_lookup("Москва 13-я парковая дом 4")
    assert answer["status"] == "found"
    assert answer["engine"] == "v1"
    assert answer["data"] == {"1": [("VALID", "test")]}
