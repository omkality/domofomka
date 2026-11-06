import sqlite3
from pathlib import Path

import pytest

from src.search.constraints import legacy_result_matches
from src.search.engine import Catalog
from src.search.normalize import parse_query, text
from tools.build_catalog import SCHEMA, index_streets


@pytest.fixture
def names(tmp_path):
    path = tmp_path / "names.db"
    values = [
        ("village", "Москва", "улица", "Мичуринский проспект.Олимпийская деревня", "12"),
        ("wrong-village", "Тюмень", "улица", "Олимпийская", "12"),
        ("wrong-locality-label", "Новая Деревня", "улица", "Олимпийская", "12"),
        ("embankment", "Долгопрудный", "улица", "Набережная", "19"),
        ("partial", "Москва", "улица", "Маршала Тимошенко", "40"),
        ("other-town", "Тула", "улица", "Тимошенко", "2"),
        ("a6", "Москва", "квартал", "Волжский Бульвар 114А", "к6"),
        ("a7", "Москва", "квартал", "Волжский Бульвар 114А", "к7"),
        ("b6", "Москва", "квартал", "Волжский Бульвар 114Б", "к6"),
        ("scoped", "Москва", "улица", "Краснодарская", "2"),
        ("wrong-scoped", "Москва", "улица", "Краснодонская", "27"),
        ("full-name", "Москва", "улица", "Большая Набережная", "1"),
        ("wrong-type", "Москва", "набережная", "Большая", "1"),
        ("split", "Москва", "улица", "Мичурина", "1а"),
    ]
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for bid, city, kind, street, house in values:
            db.execute(
                "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    bid,
                    "guid-" + bid,
                    f"{city}, {kind} {street}, дом {house}",
                    city,
                    street,
                    house,
                    text(street),
                    text(city),
                    "test",
                    city,
                ),
            )
            db.execute("INSERT INTO codes VALUES(?,?,?,?)", (bid, "1", "test", "ONLY-" + bid))
        index_streets(db)
    return Catalog(str(path))


@pytest.mark.parametrize(
    ("query", "identifier"),
    [
        ("Олимпийская деревня 12", "village"),
        ("Мичуринский проспект Олимпийская деревня 12", "village"),
        ("Набережная улица 19", "embankment"),
        ("улица Большая Набережная 1", "full-name"),
        ("Тимошенко 40", "partial"),
        ("Москва Волжский бульвар квартал 114Ак6", "a6"),
        ("Москва Волжский бульвар 114А квартал к7", "a7"),
        ("Москва Волжский бульвар к6 квартал 114Б", "b6"),
        ("Москва Волжский бульвар квартал 114А, к6", "a6"),
        ("квартал 114А Волжский бульвар к6", "a6"),
        ("Мичу рина 1а", "split"),
    ],
)
def test_meaningful_names_and_area_numbers_do_not_select_another_identity(names, query, identifier):
    result = names.resolve(query)
    assert result["status"] == "found"
    assert result["building_id"] == identifier
    assert result["data"] == {"1": [["ONLY-" + identifier, "test"]]}


def test_known_full_location_with_unknown_house_cannot_become_another_street(names):
    result = names.resolve("Москва Краснодарская 27")
    assert result["status"] == "not_found"
    assert result["fallback_rejected"] == "known_street_without_requested_house"
    assert not legacy_result_matches(
        "Москва Краснодарская 27", "Москва, улица Краснодонская, дом 27", whole_location=True
    )


def test_numbered_area_without_a_house_is_incomplete_and_cannot_fall_back_to_that_number(names):
    query = "Москва квартал 114А"
    assert parse_query(query).house is None
    assert names.resolve(query)["status"] == "incomplete_query"
    assert not legacy_result_matches(query, "Москва, квартал Тестовый, дом 114А")


def test_repair_preserves_addresses_codes_and_bundles(names, tmp_path):
    from tools.repair_search_words import repair

    with sqlite3.connect(names.path) as db:
        db.execute("CREATE TABLE street_houses(id TEXT PRIMARY KEY,data BLOB)")
        db.execute("INSERT INTO street_houses VALUES('frozen',x'010203')")
        db.execute("UPDATE streets SET words='broken index'")
        db.execute("INSERT INTO aliases VALUES('split','москва','мичурина','1а')")
        frozen = {
            table: db.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
            for table in ["buildings", "codes", "aliases", "street_houses", "metadata"]
        }
    output = tmp_path / "repaired.db"
    result = repair(Path(names.path), output)
    assert result["changed_street_text"] > 0
    with sqlite3.connect(output) as db:
        assert frozen == {table: db.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall() for table in frozen}
        assert db.execute("SELECT count(*) FROM search WHERE search MATCH 'набережная'").fetchone()[0] > 0
    assert Catalog(str(output)).resolve("Мичурина 1а")["building_id"] == "split"


@pytest.mark.parametrize(
    ("message", "words", "house"),
    [
        ("Ковровый Микрорайон 31", "ковровый", "31"),
        ("Москва, 4-й квартал 2", "москва 4", "2"),
        ("5 квартал 16 дом", "5", "16"),
        ("Давыдово 2й микрорайон 7", "давыдово 2", "7"),
        ("Московский 1-й микрорайон 30к1", "московский 1", "30к1"),
        ("Московский 1-ый микрорайон 30к1", "московский 1", "30к1"),
        ("северный квартал 3", "северный", "3"),
        ("Западный квартал 5к1", "западный", "5к1"),
        ("Капотня 5 квартал 20", "капотня 5", "20"),
        ("Самаркандский бульвар дом 4 квартал 137А", "самаркандский 137а", "4"),
    ],
)
def test_area_label_cannot_swallow_an_independent_house(message, words, house):
    query = parse_query(message)
    assert query.words == words
    assert query.house == house


@pytest.mark.parametrize("exact", [False, True])
def test_native_does_not_confidently_name_a_fuzzy_empty_house(names, exact):
    import hashlib

    from tools.benchmark_native_search import native_result

    with sqlite3.connect(names.path) as db:
        db.execute("DELETE FROM codes WHERE building_id='other-town'")
    sid = hashlib.sha256("тула\0тимошенко".encode()).hexdigest()[:32]
    hit = {"_id": sid, "_score": 10, "matched_queries": ["canonical", "exact"] if exact else ["canonical"]}
    query = "Тимошенко 2" if exact else "Тимашенко 2"
    result = native_result(names, query, [hit], 1)
    assert result["status"] == ("no_codes" if exact else "no_codes_ambiguous")
