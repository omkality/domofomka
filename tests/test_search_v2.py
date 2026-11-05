import sqlite3
from pathlib import Path

import pytest

from src.search.engine import Catalog, search_body
from src.search.normalize import parse_query, text
from tools.build_catalog import SCHEMA


@pytest.mark.parametrize(
    ("value", "words", "house", "entrance"),
    [
        ("Перерва, 62к1, подъезд 1", "перерва", "62к1", "1"),
        ("МСК Перерва д. 62 корпус 1", "москва перерва", "62к1", None),
        ("Первая Парковая, дом 3", "1 парковая", "3", None),
        ("Восьмая Советская 2", "8 советская", "2", None),
        ("Десятая Парковая дом 4", "10 парковая", "4", None),
        ("Двадцать первая улица 6", "21", "6", None),
        ("улица 1905 года 3", "1905 года", "3", None),
        ("Первомайская 7", "первомайская", "7", None),
        ("СПБ восьмая Советская 2 подъезд первый", "санкт петербург 8 советская", "2", "1"),
        ("Москва улица Острякова 9", "москва острякова", "9", None),
        ("Строгинский 23", "строгинский", "23", None),
        ("Истринская 4", "истринская", "4", None),
        ("Строителей дом 5 строение 1", "строителей", "5с1", None),
    ],
)
def test_human_query(value, words, house, entrance):
    query = parse_query(value)
    assert (query.words, query.house, query.entrance) == (words, house, entrance)


@pytest.fixture
def catalog(tmp_path: Path):
    path = tmp_path / "catalog.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    buildings = [
        ("a", "Москва", "Перерва", "62к1"),
        ("b", "Москва", "Перерва", "62к2"),
        ("c", "Подольск", "Перерва", "62к1"),
        ("d", "Москва", "1-я Парковая", "3"),
        ("e", "Москва", "2-я Парковая", "3"),
        ("f", "Москва", "Коломенская", "2"),
        ("g", "Москва", "Коломенская", "2а"),
    ]
    for id, city, street, house in buildings:
        db.execute(
            "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (id, id, f"{city}, {street}, дом {house}", city, street, house, text(street), text(city), "77", text(city)),
        )
        db.execute("INSERT INTO streets VALUES(?,?,?,?)", (id, text(city), text(street), text(city + " " + street)))
        db.execute("INSERT INTO search VALUES(?,?)", (id, text(city + " " + street)))
    db.executemany("INSERT INTO codes VALUES(?,?,?,?)", [("a", "2", "test", "CODE"), ("g", "1", "test", "OTHER")])
    db.commit()
    db.close()
    return Catalog(str(path))


def test_ambiguous_city_requires_choice(catalog):
    assert catalog.resolve("Перерва 62к1")["status"] == "ambiguous"
    assert catalog.resolve("мск Перерва 62к1")["building_id"] == "a"


def test_no_wrong_house_or_neighbor_code(catalog):
    assert catalog.resolve("Москва Перерва 62к3")["status"] == "not_found"
    result = catalog.resolve("Москва Коломенская 2")
    assert result["status"] == "no_codes"
    assert not result["data"]
    assert result["building_id"] == "f"


def test_numbers_are_not_fuzzy(catalog):
    assert catalog.resolve("Москва первая парковая 3")["building_id"] == "d"
    assert catalog.resolve("Москва вторая парковая 3")["building_id"] == "e"
    assert catalog.resolve("Москва десятая парковая 3")["status"] == "not_found"


def test_opensearch_exact_building_filter():
    body = search_body("Мск Перерва 62к1 подъезд 5")
    assert body["query"]["bool"]["filter"] == [{"term": {"house": "62к1"}}]


def test_top_n_index_candidates_cannot_hide_exact_catalog_match(catalog):
    wrong_city = catalog.building("c")
    assert catalog.resolve("мск Перерва 62к1", [wrong_city])["building_id"] == "a"


def test_locality_words_cannot_replace_the_street_name(catalog):
    unrelated = {
        "id": "wrong",
        "guid": "wrong",
        "city": "Московская область Люберцы",
        "street": "Авиаторов",
        "house": "15",
        "display": "Московская область Люберцы, ул. Авиаторов, дом 15",
        "region": "50",
        "lineage": "люберцы",
    }
    assert catalog.resolve("Люберцы Московская улица 15", [unrelated])["status"] == "not_found"


def test_street_type_can_also_be_part_of_an_official_name(catalog):
    proper = {
        "id": "proper",
        "guid": "proper",
        "city": "Москва",
        "street": "Большая Набережная",
        "house": "1",
        "display": "Москва, улица Большая Набережная, дом 1",
        "region": "77",
        "lineage": "москва",
    }
    result = catalog.resolve("Большая набережная 1", [proper])
    assert result["building_id"] == "proper"


def test_source_city_alias_retrieves_coded_identity_without_a_primary_street_document(catalog):
    from tools.refresh_aliases import refresh

    with sqlite3.connect(catalog.path) as db:
        db.execute("UPDATE buildings SET city='Московская',city_key='московская' WHERE id='a'")
        db.execute("DELETE FROM search WHERE id='a'")
        db.execute("INSERT INTO aliases VALUES('a','люберцы','перерва','62к1')")
        refresh(db)
    answer = catalog.resolve("Люберцы Перерва 62к1")
    assert answer["building_id"] == "a"
    assert answer["status"] == "found"
    assert catalog.resolve("Подольск Перерва 62к1")["building_id"] == "c"


def test_split_street_prefix_preserves_the_explicit_city_and_house(catalog):
    proper = {
        "id": "proper",
        "guid": "proper",
        "city": "Москва",
        "street": "Новоорловская",
        "house": "6",
        "display": "Москва, улица Новоорловская, дом 6",
        "region": "77",
        "lineage": "москва",
    }
    assert catalog.resolve("Москва Ново Орловская 6", [proper])["building_id"] == "proper"
    assert catalog.resolve("Подольск Ново Орловская 6", [proper])["status"] == "not_found"
    assert catalog.resolve("Москва Ново Орловская 7", [proper])["status"] == "not_found"
