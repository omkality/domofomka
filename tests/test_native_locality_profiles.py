import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from tools.benchmark_native_search import native_result, reviewed_conflict
from tools.search_locality_profiles import known_locality, locality_hint, locality_matches


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("П. Имени Мастера 11", "имени мастера"),
        ("посёлок Примерово, 11", "примерово"),
        ("г. Москва, улица Тестовая 12к1", "москва"),
        ("город Образцово улица Учебная 6", "образцово"),
        ("д. 3", None),
        ("Тестоград, микрорайон Д, дом 2", None),
    ],
)
def test_marked_locality_is_a_role_not_a_fuzzy_city_guess(query, expected):
    assert locality_hint(query, lambda value: False) == expected


def test_comma_prefix_is_protected_only_when_it_is_a_complete_known_locality():
    def known(value):
        return value in {"тестоград", "москва", "санкт петербург"}

    assert locality_hint("Тестоград, микрорайон Д, дом 2", known, comma_prefix=True) == "тестоград"
    assert locality_hint("СПб, Невский 1", known, comma_prefix=True) == "санкт петербург"
    assert locality_hint("Тестогад, микрорайон Д, дом 2", known, comma_prefix=True) is None
    assert locality_hint("Демонстрационная, 4, Тестовск", known, comma_prefix=True) is None
    assert locality_hint("демонстрационная 1а", known, comma_prefix=True) is None


def test_scoped_policy_requires_separate_locality_and_street_fields():
    def known(value):
        return True

    for query in ["Тестовая улица, 16", "Примерная, 9", "Тестоград, корпус 902", "Учебная, 4, Примерск"]:
        assert locality_hint(query, known, comma_prefix=True, require_street_field=True) is None
    assert (
        locality_hint("Тестоград, микрорайон Д, дом 2", known, comma_prefix=True, require_street_field=True)
        == "тестоград"
    )
    assert (
        locality_hint("СПб, улица Невский 1", known, comma_prefix=True, require_street_field=True) == "санкт петербург"
    )


def test_locality_cannot_be_explained_by_another_citys_street():
    row = {"city": "Примерск", "lineage": "примерск примерскская", "street": "имени С.П. Мастерова"}
    assert not locality_matches("имени мастера", row)
    assert not locality_matches("тестоград", {"city": "Примерск", "lineage": "московская", "street": "Тестоград"})
    assert locality_matches("образцово", {"city": "Образцово", "lineage": "московская"})


def test_official_ancestry_and_matching_source_locality_remain_valid():
    assert locality_matches("москва", {"city": "Примерово", "lineage": "москва сосенское примерово"})
    row = {"city": "Москва", "lineage": "москва", "street": "Липовый Парк"}
    aliases = [{"street": "липовый парк", "words": "поселок примерово липовый парк"}]
    assert locality_matches("примерово", row, aliases)
    assert not locality_matches("примерово", row, [{"street": "примерово", "words": "москва примерово"}])


def test_unreconciled_source_can_store_an_explicit_settlement_in_the_street_field():
    row = {
        "id": "legacy:synthetic",
        "city": "Московская область, городской округ Тестовск",
        "lineage": "московская тестовск",
        "street": "рабочий Примерово",
        "display": "Московская область, городской округ Тестовск, поселок рабочий Примерово, дом 45",
    }
    assert locality_matches("примерово", row)
    assert not locality_matches("примерово", dict(row, id="gar:synthetic", street="Примерово"))
    assert not locality_matches(
        "примерово", dict(row, street="Примерово", display=row["city"] + ", улица Примерово, дом 45")
    )


def test_native_hit_with_another_explicit_locality_is_rejected_before_answer():
    row = {
        "id": "gar:synthetic",
        "guid": "synthetic",
        "city": "Примерск",
        "city_key": "примерск",
        "street": "имени С.П. Мастерова",
        "street_key": "имени с п мастерова",
        "house": "11",
        "display": "Примерск, ул. имени С.П. Мастерова, дом 11",
        "lineage": "примерск",
    }

    class FakeCatalog:
        def load_candidates(self, ids, house):
            return [row]

        def answer(self, candidate, entrance):
            return {"status": "found", "building_id": candidate["id"]}

    hit = {"_id": hashlib.sha256((row["city_key"] + "\0" + row["street_key"]).encode()).hexdigest()[:32], "_score": 10}
    assert native_result(FakeCatalog(), "П. Имени Мастера 11", [hit], 1)["status"] == "found"
    assert (
        native_result(FakeCatalog(), "П. Имени Мастера 11", [hit], 1, locality_hint="имени мастера")["status"]
        == "not_found"
    )


def test_reviewed_partial_locality_constraint_does_not_require_an_invented_street():
    case = {"expected_city": "Образцово", "expected_house": "6"}
    wrong = {"status": "found", "address": "Примерск, улица Тестовая, дом 6"}
    assert reviewed_conflict(case, wrong, None)
    assert not reviewed_conflict(case, {"status": "not_found"}, None)
    assert not reviewed_conflict(case, {"status": "found", "address": "Образцово, улица Тестовая, дом 6"}, None)


def test_review_can_require_an_incomplete_query_without_inventing_an_address():
    case = {"expected_status": "incomplete_query"}
    assert reviewed_conflict(case, {"status": "found", "address": "Образцово, улица Тестовая, дом 14"}, None)
    assert not reviewed_conflict(case, {"status": "incomplete_query"}, None)


def test_known_prefix_uses_only_locality_fields_and_invalidates_on_catalog_change(tmp_path):
    path = tmp_path / "catalog.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE streets(city_key TEXT,street_key TEXT)")
        db.execute("INSERT INTO streets VALUES('тестоград','ленина')")
    catalog = SimpleNamespace(path=str(path))
    assert known_locality(catalog, "тестоград")
    assert not known_locality(catalog, "ленина")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO streets VALUES('образцово','учебная')")
    assert known_locality(catalog, "образцово")
