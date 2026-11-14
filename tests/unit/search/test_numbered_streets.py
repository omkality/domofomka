import sqlite3

import pytest

from src.code_service.services.v2.normalize import parse_query, text
from tools.catalog.build_catalog import SCHEMA, index_streets
from tools.catalog.sqlite_catalog import SqliteCatalog

ORDINALS = [
    ("первая", "1"),
    ("вторая", "2"),
    ("третья", "3"),
    ("восьмая", "8"),
    ("десятая", "10"),
    ("двадцать первая", "21"),
    ("двадцать третья", "23"),
    ("тридцать первая", "31"),
    ("сотая", "100"),
]


@pytest.fixture
def streets(tmp_path):
    path = tmp_path / "numbered.db"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for _, number in ORDINALS:
            street = number + "-я Парковая"
            db.execute(
                "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    number,
                    "guid-" + number,
                    "Москва, улица " + street + ", дом 23к1",
                    "Москва",
                    street,
                    "23к1",
                    text(street),
                    "москва",
                    "77",
                    "москва",
                ),
            )
            db.execute(
                "INSERT INTO codes VALUES(?,?,?,?)",
                (number, "2", "test", "STREET-" + number),
            )
        index_streets(db)
    return SqliteCatalog(str(path))


@pytest.mark.parametrize(("ordinal", "number"), ORDINALS)
def test_words_and_numeric_suffixes_resolve_the_same_numbered_street(streets, ordinal, number):
    for spelling in [ordinal, number + "я", number + "-я", number + "-ая"]:
        query = "г. Москва, ул. " + spelling + " Парковая, д. 23 корпус 1, подъезд второй"
        parsed = parse_query(query)
        assert parsed.words == "москва " + number + " парковая"
        assert parsed.house == "23к1"
        assert parsed.entrance == "2"
        answer = streets.resolve(query)
        assert answer["building_id"] == number
        assert answer["data"] == {"2": [["STREET-" + number, "test"]]}


@pytest.mark.parametrize(
    ("query", "words", "house"),
    [
        ("Первый Покровский проезд 4", "1 покровский", "4"),
        ("Третий Покровский проезд 4", "3 покровский", "4"),
        ("3й Покровский проезд 4", "3 покровский", "4"),
        ("Третьего Интернационала 1", "3 интернационала", "1"),
        ("Двадцать третий проезд 6", "23", "6"),
        ("Восьмая улица Соколиной Горы 15", "8 соколиной горы", "15"),
        ("8-я Парковая, дом третий", "8 парковая", "3"),
        ("Первая Парковая", "1 парковая", None),
        ("Десятая Парковая подъезд 2", "10 парковая", None),
        ("улица 1905 года 3", "1905 года", "3"),
        ("улица 9 Мая 1", "9 мая", "1"),
        (
            "Россия, г. Москва, пр-кт Вернадского, д. 105к2",
            "москва вернадского",
            "105к2",
        ),
    ],
)
def test_street_number_keeps_its_role(query, words, house):
    parsed = parse_query(query)
    assert parsed.words == words
    assert parsed.house == house


@pytest.mark.parametrize("query", ["Москва Парковая 23к1", "Москва девятая Парковая 23к1"])
def test_missing_or_different_street_number_cannot_select_another_street(streets, query):
    assert streets.resolve(query)["status"] == "not_found"
