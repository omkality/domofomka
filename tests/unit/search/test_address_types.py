import sqlite3

import pytest

from src.code_service.services.v2.normalize import parse_query
from tools.catalog.build_catalog import SCHEMA, index_streets
from tools.catalog.sqlite_catalog import SqliteCatalog


@pytest.fixture
def typed_addresses(tmp_path):
    path = tmp_path / "types.db"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for identifier, kind, street in [
            ("quarter", "квартал", "Северный"),
            ("passage", "проезд", "Северный"),
            ("street", "улица", "Северный Квартал"),
        ]:
            db.execute(
                "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    "guid-" + identifier,
                    f"Тестовый, {kind} {street}, дом 20",
                    "Тестовый",
                    street,
                    "20",
                    "северный",
                    "тестовый",
                    "77",
                    "тестовый",
                ),
            )
            db.execute("INSERT INTO codes VALUES(?,?,?,?)", (identifier, "1", "test", "ONLY-" + identifier))
        index_streets(db)
    return SqliteCatalog(str(path))


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Тестовый квартал Северный 20", "quarter"),
        ("Тестовый кв-л Северный 20", "quarter"),
        ("Тестовый Северный проезд 20", "passage"),
        ("Тестовый улица Северный Квартал 20", "street"),
    ],
)
def test_explicit_address_type_cannot_select_codes_from_another_type(typed_addresses, query, expected):
    result = typed_addresses.resolve(query)
    assert result["status"] == "found"
    assert result["building_id"] == expected
    assert result["data"] == {"1": [["ONLY-" + expected, "test"]]}


def test_empty_quarter_does_not_borrow_codes_from_a_same_named_passage(typed_addresses):
    with sqlite3.connect(typed_addresses.path) as db:
        db.execute("DELETE FROM codes WHERE building_id='quarter'")
    result = typed_addresses.resolve("Тестовый квартал Северный 20")
    assert result["status"] == "no_codes"
    assert result["building_id"] == "quarter"
    assert result["data"] == {}


def test_quarter_can_follow_a_microdistrict_without_overriding_a_named_street():
    assert parse_query("микрорайон Купелинка квартал Северный 20").kind == "квартал"
    assert parse_query("микрорайон Купелинка улица Северный Квартал 20").kind == "ул"
