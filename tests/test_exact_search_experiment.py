import hashlib
import sqlite3

import pytest

from src.search.engine import Catalog
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


def test_exact_street_without_codes_does_not_borrow_codes_from_a_longer_name(names):
    path, hits = names
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM codes WHERE building_id='exact'")
    for result in [
        ExactCatalog(str(path)).resolve("Москва Самотечная 11"),
        native_result(Catalog(str(path)), "Москва Самотечная 11", hits, 1, exact_tokens={"москва", "самотечная"}),
    ]:
        assert result["status"] == "no_codes"
        assert result["building_id"] == "exact"
        assert result["data"] == {}


def test_bm25_score_does_not_choose_between_equally_matching_coded_cities(names):
    path, hits = names
    result = native_result(Catalog(str(path)), "Самотечная 11", hits, 1, exact_tokens={"самотечная"})
    assert result["status"] == "ambiguous"
    assert {c["building_id"] for c in result["candidates"]} == {"exact", "other-city"}


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
