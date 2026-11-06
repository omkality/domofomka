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
        ("Свободы 75 к2", "свободы", "75к2", None),
        ("Куусинена 6 к 13", "куусинена", "6к13", None),
        ("Барклая 16 с 4", "барклая", "16с4", None),
        ("Болотниковская 2 к А", "болотниковская", "2ка", None),
        ("Дежнева17", "дежнева", "17", None),
        ("Свободы4одинцово", "свободы одинцово", "4", None),
        ("Южная 27дрожжино", "южная дрожжино", "27", None),
        ("Большая Черкизовская1к2", "большая черкизовская", "1к2", None),
        ("Волоколамское шоссе 1кА", "волоколамское", "1ка", None),
        ("Зеленоград, к902", "зеленоград", "к902", None),
        ("Зеленоград, корпус 119", "зеленоград", "к119", None),
        ("третья Парковая дом третий", "3 парковая", "3", None),
        ("МСК Перерва д. 62 корпус 1", "москва перерва", "62к1", None),
        ("Первая Парковая, дом 3", "1 парковая", "3", None),
        ("Восьмая Советская 2", "8 советская", "2", None),
        ("Третья Парковая 3", "3 парковая", "3", None),
        ("Двадцать третья улица 6", "23", "6", None),
        ("улица Третьего Интернационала 1", "3 интернационала", "1", None),
        ("Десятая Парковая дом 4", "10 парковая", "4", None),
        ("Двадцать первая улица 6", "21", "6", None),
        ("улица 1905 года 3", "1905 года", "3", None),
        ("Первомайская 7", "первомайская", "7", None),
        ("СПБ восьмая Советская 2 подъезд первый", "санкт петербург 8 советская", "2", "1"),
        ("Питер восьмая Советская 2", "санкт петербург 8 советская", "2", None),
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
    db.executemany(
        "INSERT INTO codes VALUES(?,?,?,?)",
        [("a", "2", "test", "CODE"), ("c", "1", "test", "PODOLSK"), ("g", "1", "test", "OTHER")],
    )
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
    assert catalog.resolve("Москва Парковая 3")["status"] == "not_found"
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


@pytest.mark.parametrize(("query", "street"), [("Ново Орловская", "Новоорловская"), ("По речная", "Поречная")])
def test_split_street_prefix_preserves_the_explicit_city_and_house(catalog, query, street):
    proper = {
        "id": "proper",
        "guid": "proper",
        "city": "Москва",
        "street": street,
        "house": "6",
        "display": f"Москва, улица {street}, дом 6",
        "region": "77",
        "lineage": "москва",
    }
    assert catalog.resolve(f"Москва {query} 6", [proper])["building_id"] == "proper"
    assert catalog.resolve(f"Подольск {query} 6", [proper])["status"] == "not_found"
    assert catalog.resolve(f"Москва {query} 7", [proper])["status"] == "not_found"


def test_misaligned_legacy_city_and_street_do_not_become_an_automatic_v2_answer(catalog):
    corrupt = {
        "id": "legacy:bad",
        "guid": None,
        "city": "Санкт-Петербург, Заречная ул.",
        "street": "Парнас",
        "house": "33",
        "display": "Санкт-Петербург, Заречная ул., улица Парнас, дом 33",
        "region": "78",
        "lineage": "",
    }
    assert catalog.resolve("Парнас Заречная 33", [corrupt])["status"] == "not_found"


def test_reused_regional_keys_follow_the_updated_normalizer(catalog):
    from tools.build_catalog import index_streets, normalize_keys

    with sqlite3.connect(catalog.path) as db:
        db.execute(
            "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "third",
                "third",
                "Москва, улица Третья Парковая, дом 3",
                "Москва",
                "Третья Парковая",
                "3",
                "третья парковая",
                "москва",
                "77",
                "москва",
            ),
        )
        db.execute("INSERT INTO codes VALUES('third','1','test','CODE')")
        db.execute("INSERT INTO aliases VALUES('third','москва','третья парковая','3')")
        normalize_keys(db)
        index_streets(db)
    assert catalog.resolve("Москва третья парковая 3")["building_id"] == "third"
    assert catalog.resolve("Москва 3-я парковая 3")["building_id"] == "third"


@pytest.mark.parametrize(
    "message", ["третий покровский проезд", "3-й Покровский проезд", "первая парковая", "улица 1905 года", "8 Марта"]
)
def test_street_number_is_not_a_missing_house(message, catalog):
    assert parse_query(message).house is None
    assert catalog.resolve(message)["status"] == "incomplete_query"


def test_one_coded_address_opens_without_listing_empty_houses(catalog):
    with sqlite3.connect(catalog.path) as db:
        db.execute("DELETE FROM codes WHERE building_id='c'")
    answer = catalog.resolve("Перерва 62к1")
    assert answer["status"] == "found"
    assert answer["building_id"] == "a"
    assert "candidates" not in answer


def test_only_coded_candidates_are_offered(catalog):
    with sqlite3.connect(catalog.path) as db:
        db.execute(
            "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "empty-city",
                "empty-city",
                "Город, Перерва, дом 62к1",
                "Город",
                "Перерва",
                "62к1",
                "перерва",
                "город",
                "",
                "",
            ),
        )
        db.execute("INSERT INTO streets VALUES('empty-street','город','перерва','город перерва')")
        db.execute("INSERT INTO search VALUES('empty-street','город перерва')")
    result = catalog.resolve("Перерва 62к1")
    assert result["status"] == "ambiguous"
    assert {c["building_id"] for c in result["candidates"]} == {"a", "c"}


def test_empty_house_choices_are_not_presented_as_a_menu(catalog):
    with sqlite3.connect(catalog.path) as db:
        db.execute("DELETE FROM codes WHERE building_id IN ('a','c')")
    result = catalog.resolve("Перерва 62к1")
    assert result["status"] == "no_codes_ambiguous"
    assert "candidates" not in result
    assert catalog.resolve("Москва Перерва 62к1")["status"] == "no_codes"


def test_code_availability_cannot_replace_an_exact_address_with_a_fuzzy_street(catalog):
    with sqlite3.connect(catalog.path) as db:
        db.execute(
            "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "literal",
                "literal",
                "Москва, улица Черемушкинская, дом 2",
                "Москва",
                "Черемушкинская",
                "2",
                "черемушкинская",
                "москва",
                "77",
                "москва",
            ),
        )
        db.execute("INSERT INTO streets VALUES('literal-street','москва','черемушкинская','москва черемушкинская')")
        db.execute("INSERT INTO search VALUES('literal-street','москва черемушкинская')")
    coded_nearby_name = {
        "id": "a",
        "guid": "a",
        "city": "Москва",
        "street": "Черемушкинскаяа",
        "house": "2",
        "display": "Москва, улица Черемушкинскаяа, дом 2",
        "region": "77",
        "lineage": "москва",
    }
    result = catalog.resolve("Москва Черемушкинская 2", [coded_nearby_name])
    assert result["status"] == "no_codes"
    assert result["building_id"] == "literal"
