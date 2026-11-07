import hashlib
import sqlite3

import pytest

from src.search.constraints import complete_name_match
from src.search.engine import Catalog
from src.search.normalize import parse_query
from tools.benchmark_native_search import configurations, native_result, search_variant
from tools.build_catalog import SCHEMA, index_streets
from tools.search_lexical_profiles import ExactCatalog


@pytest.fixture
def names(tmp_path):
    path = tmp_path / "names.db"
    values = [
        ("exact", "Москва", "москва", "Самотёчная", "самотечная"),
        ("longer", "Москва", "москва", "Садовая-Самотёчная", "садовая самотечная"),
        ("other-city", "Тула", "тула", "Самотёчная", "самотечная"),
    ]
    hits = []
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for bid, city, city_key, street, street_key in values:
            db.execute(
                "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    bid,
                    "guid-" + bid,
                    f"{city}, ул. {street}, дом 11",
                    city,
                    street,
                    "11",
                    street_key,
                    city_key,
                    "77",
                    city_key,
                ),
            )
            db.execute("INSERT INTO codes VALUES(?,?,?,?)", (bid, "1", "test", "ONLY-" + bid))
            hits.append(
                {
                    "_id": hashlib.sha256((city_key + "\0" + street_key).encode()).hexdigest()[:32],
                    "_score": 100 if bid == "exact" else 90 if bid == "longer" else 5,
                }
            )
        index_streets(db)
    return path, hits


def test_full_street_name_is_preferred_without_discarding_another_matching_city(names):
    path, _ = names
    catalog = ExactCatalog(str(path))
    result = catalog.resolve("Москва Самотёчная 11")
    assert result["building_id"] == "exact"
    ambiguous = catalog.resolve("Самотёчная 11")
    assert ambiguous["status"] == "ambiguous"
    assert {c["building_id"] for c in ambiguous["candidates"]} == {"exact", "other-city"}


@pytest.mark.parametrize("coverage", [False, True])
def test_exact_street_without_codes_does_not_borrow_codes_from_a_longer_name(names, coverage):
    path, hits = names
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM codes WHERE building_id='exact'")
    for result in [
        Catalog(str(path)).resolve("Москва Самотечная 11"),
        ExactCatalog(str(path)).resolve("Москва Самотечная 11"),
        native_result(
            Catalog(str(path)),
            "Москва Самотечная 11",
            hits,
            1,
            exact_tokens={"москва", "самотечная"},
            require_exact=True,
            literal_coverage=coverage,
        ),
    ]:
        assert result["status"] == "no_codes"
        assert result["building_id"] == "exact"
        assert result["data"] == {}


def test_bm25_score_does_not_choose_between_equally_matching_coded_cities(names):
    path, hits = names
    result = native_result(Catalog(str(path)), "Самотечная 11", hits, 1, exact_tokens={"самотечная"})
    assert result["status"] == "ambiguous"
    assert {c["building_id"] for c in result["candidates"]} == {"exact", "other-city"}


def test_catalog_without_a_city_keeps_coded_partial_names_available(names):
    path, _ = names
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM codes WHERE building_id IN ('exact','other-city')")
    result = Catalog(str(path)).resolve("Самотечная 11")
    assert result["status"] == "found"
    assert result["building_id"] == "longer"


def test_exact_policy_reads_non_coded_hits_before_deciding_and_preserves_fuzzy_fallback(names):
    path, hits = names
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM codes WHERE building_id='exact'")
    calls = []

    def search(body):
        coded = any("coded_house" in c.get("term", {}) for c in body["query"]["bool"]["filter"])
        calls.append(coded)
        return [hits[1]] if coded else hits[:2]

    variant = next(v for v in configurations("lexical") if v["name"] == "lexical.analyzer_exact-AUTO-r1.0")
    result = search_variant(
        Catalog(str(path)), "Москва Самотечная 11", variant, search, lambda *a: ["москва", "самотечная"]
    )
    assert calls == [False, True]
    assert result["status"] == "no_codes"
    assert result["building_id"] == "exact"
    query = "Москва Самотечна 11"
    assert ExactCatalog(str(path)).resolve(query) == Catalog(str(path)).resolve(query)


@pytest.mark.parametrize("unrelated_scope", [False, True])
def test_coverage_without_a_city_does_not_hide_coded_partial_names_behind_empty_exact_names(names, unrelated_scope):
    path, hits = names
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM codes WHERE building_id IN ('exact','other-city')")
    result = native_result(
        Catalog(str(path)),
        "Самотечная 11",
        hits,
        1,
        exact_tokens={"самотечная"},
        literal_coverage=True,
        require_exact=unrelated_scope,
    )
    assert result["status"] == "found"
    assert result["building_id"] == "longer"


@pytest.mark.parametrize("canonical_coded", [False, True])
def test_coverage_preserves_full_street_with_administrative_locality_variant_without_merging_ids(
    names, canonical_coded
):
    path, hits = names
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "source",
                None,
                "город Москва, Центральный район, улица Самотечная, дом 11",
                "город Москва, Центральный район",
                "Самотечная",
                "11",
                "самотечная",
                "город москва центральный район",
                "77",
                "москва центральный район",
            ),
        )
        db.execute("INSERT INTO codes VALUES('source','1','test','ONLY-source')")
        if not canonical_coded:
            db.execute("DELETE FROM codes WHERE building_id='exact'")
        index_streets(db)
    hits = hits + [
        {"_id": hashlib.sha256("город москва центральный район\0самотечная".encode()).hexdigest()[:32], "_score": 1}
    ]
    result = native_result(
        Catalog(str(path)),
        "Москва Самотечная 11",
        hits,
        1,
        exact_tokens={"москва", "самотечная"},
        require_exact=True,
        literal_coverage=True,
    )
    if canonical_coded:
        assert result["status"] == "ambiguous"
        assert {c["building_id"] for c in result["candidates"]} == {"exact", "source"}
    else:
        assert result["status"] == "found"
        assert result["building_id"] == "source"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM buildings").fetchone()[0] == 4
    catalog_result = Catalog(str(path)).resolve("Москва Самотечная 11")
    if not canonical_coded:
        assert catalog_result["building_id"] == "source"


@pytest.mark.parametrize(
    ("short", "full"),
    [
        ("пр-д", "проезд"),
        ("б-р", "бульвар"),
        ("наб.", "набережная"),
        ("пл.", "площадь"),
        ("пр-кт", "проспект"),
        ("ш.", "шоссе"),
        ("ул.", "улица"),
    ],
)
def test_complete_name_precision_uses_the_same_address_element_type_for_gar_abbreviations(short, full):
    row = {"city": "Москва", "street": "Тестовая", "display": f"Москва, {short} Тестовая, дом 11"}
    assert complete_name_match(parse_query(f"Москва {full} Тестовая 11"), row)
    row.update(street="Тестовая Б.", display=f"Москва, {short} Тестовая Б., дом 11")
    assert not complete_name_match(parse_query(f"Москва {full} Тестовая 11"), row)


def test_scoped_empty_street_does_not_borrow_a_name_with_an_unmentioned_type_word(names):
    path, _ = names
    with sqlite3.connect(path) as db:
        db.execute("UPDATE buildings SET display='Москва, проезд Самотечная, дом 11' WHERE id='exact'")
        db.execute(
            "UPDATE buildings SET street='Самотечная Проезд',street_key='самотечная проезд',"
            "display='Москва, улица Самотечная Проезд, дом 11' WHERE id='longer'"
        )
        db.execute("DELETE FROM codes WHERE building_id='exact'")
        index_streets(db)
    result = Catalog(str(path)).resolve("Москва Самотечная 11")
    assert result["status"] == "no_codes"
    assert result["building_id"] == "exact"


def test_scoped_name_keeps_coded_source_with_the_type_stored_inside_the_street_name(names):
    path, _ = names
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE buildings SET display='Москва, жилой массив Тестовый, дом 11',street='Тестовый',"
            "street_key='тестовый' WHERE id='exact'"
        )
        db.execute(
            "UPDATE buildings SET city='Москва, Центральный район',city_key='москва центральный район',"
            "display='Москва, Центральный район, улица жилой массив Тестовый, дом 11',"
            "street='жилой массив Тестовый',street_key='жилой массив тестовый' WHERE id='longer'"
        )
        index_streets(db)
    result = Catalog(str(path)).resolve("Москва жилой массив Тестовый 11")
    assert result["status"] == "ambiguous"
    assert {v["building_id"] for v in result["candidates"]} == {"exact", "longer"}
