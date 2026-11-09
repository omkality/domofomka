import sqlite3

import pytest

from src.code_service.v2.engine import Catalog
from src.code_service.v2.normalize import text
from tools.catalog.build_catalog import SCHEMA, index_streets
from tools.catalog.compact_catalog import compact


@pytest.fixture(params=[False, True])
def prefixes(tmp_path, request):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        for bid, city, street, house, coded in [
            ("one", "Нахабино", "Тестовая", "7", True),
            ("two", "Нахалово", "Тестовая", "7", True),
            ("letter", "Нахабино", "Тестовая", "7а", True),
            ("empty", "Нахабино", "Тестовая", "8", False),
            ("ordinal", "Нахабино", "1-я Тестовая", "7", True),
            ("full-name", "Нахабино", "Тестовая Посёлок", "7", True),
            ("exact-city", "Дзержинск", "Тестовая", "10", False),
            ("wrong-city", "Дзержинский", "Тестовая", "10", True),
            ("wrong-city-house", "Дзержинский", "Тестовая", "11", True),
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
                    "test",
                    text(city),
                ),
            )
            if coded:
                db.execute("INSERT INTO codes VALUES(?,?,?,?)", (bid, "1", "test", "ONLY-" + bid))
        index_streets(db)
    if request.param:
        serving = tmp_path / "serving.db"
        compact(source, serving)
        return Catalog(str(serving))
    return Catalog(str(source))


def test_prefix_offers_separate_coded_addresses_without_automatic_city_selection(prefixes):
    result = prefixes.resolve("наха тестовая 7")
    assert result["status"] == "ambiguous"
    assert result["clarification_reason"] == "locality_prefix"
    assert {row["building_id"] for row in result["candidates"]} == {"one", "two"}
    assert result["data"] == {}
    for row in result["candidates"]:
        assert prefixes.answer(prefixes.building(row["building_id"]))["status"] == "found"


def test_even_one_matching_city_prefix_requires_confirmation(prefixes):
    result = prefixes.resolve("нахаб тестовая 7")
    assert result["status"] == "ambiguous"
    assert [row["building_id"] for row in result["candidates"]] == ["one"]
    assert prefixes.resolve("нахабино тестовая 7")["building_id"] == "one"


@pytest.mark.parametrize("query", ["нах тестовая 7", "наха тестовая 7к1", "наха тестовая 8"])
def test_prefix_does_not_invent_houses_or_offer_empty_candidates(prefixes, query):
    assert prefixes.resolve(query)["status"] not in {"found", "ambiguous"}


def test_complete_locality_is_not_replaced_by_another_city_with_the_same_prefix(prefixes):
    assert prefixes.has_exact_locality_word("дзержинск")
    assert prefixes.resolve("Дзержинск Тестовая 10")["status"] == "no_codes"
    assert prefixes.resolve("Дзержинск Тестовая 11")["status"] == "not_found"
