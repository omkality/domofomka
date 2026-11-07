import hashlib
import sqlite3

import pytest

from src.search.constraints import complete_name_match
from src.search.engine import Catalog
from src.search.normalize import parse_query
from tools.benchmark_native_search import configurations, native_result, search_variant
from tools.build_catalog import SCHEMA, index_streets
from tools.compact_catalog import compact
from tools.refresh_aliases import refresh
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
    if canonical_coded:
        assert catalog_result["status"] == "ambiguous"
        assert {v["building_id"] for v in catalog_result["candidates"]} == {"exact", "source"}
    else:
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


def test_catalog_keeps_codes_from_a_verified_parent_locality_alias_as_a_separate_choice(names):
    path, _ = names
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "satellite",
                "guid-satellite",
                "Щербинка, улица Самотечная, дом 11",
                "Щербинка",
                "Самотечная",
                "11",
                "самотечная",
                "щербинка",
                "77",
                "москва щербинка",
            ),
        )
        db.execute("INSERT INTO aliases VALUES('satellite','москва щербинка','самотечная','11')")
        db.execute("INSERT INTO codes VALUES('satellite','2','test','ONLY-satellite')")
        index_streets(db)
        refresh(db)
    catalog = Catalog(str(path))
    result = catalog.resolve("Москва Самотечная 11")
    assert result["status"] == "ambiguous"
    assert {v["building_id"] for v in result["candidates"]} == {"exact", "satellite"}
    assert catalog.answer(catalog.building("satellite"))["data"] == {"2": [["ONLY-satellite", "test"]]}
    assert catalog.resolve("Щербинка Самотечная 11")["building_id"] == "satellite"
    assert catalog.resolve("Тула Самотечная 11")["building_id"] == "other-city"


@pytest.mark.parametrize(
    ("query", "matches"),
    [
        ("Москва Самотечная 11", True),
        ("Москва Щербинка Самотечная 11", True),
        ("Москва Большая Самотечная 11", False),
        ("Тула Самотечная 11", False),
        ("Москва Самотечная Н.А. 11", False),
    ],
)
def test_complete_name_uses_paired_source_locality_and_street_without_weakening_the_name(query, matches):
    row = {
        "city": "Щербинка",
        "street": "Самотечная",
        "display": "Щербинка, улица Самотечная, дом 11",
        "_alias_names": [{"words": "москва щербинка самотечная", "street": "самотечная"}],
    }
    assert complete_name_match(parse_query(query), row, row["_alias_names"]) is matches


@pytest.mark.parametrize("label", ["ул.", "бульвар", "набережная", "пр-кт"])
def test_native_and_catalog_leave_misaligned_source_columns_to_the_legacy_fallback(names, label):
    path, _ = names
    city = f"Москва, Самотечная {label}"
    city_key = "москва самотечная " + label
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE buildings SET guid=NULL,city=?,city_key=?,street='Парнас',street_key='парнас',display=? "
            "WHERE id='longer'",
            (city, city_key, f"{city}, улица Парнас, дом 11"),
        )
        index_streets(db)
    hit = {"_id": hashlib.sha256((city_key + "\0парнас").encode()).hexdigest()[:32], "_score": 100}
    catalog = Catalog(str(path))
    query = "Москва Самотечная Парнас 11"
    result = native_result(catalog, query, [hit], 1, exact_tokens={"москва", "самотечная", "парнас"})
    assert result["status"] == "not_found"
    assert catalog.resolve(query)["status"] == "not_found"


def test_known_empty_packed_gar_address_is_not_mistaken_for_misaligned_legacy_fields(names, tmp_path):
    path, _ = names
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "known-empty",
                "guid-known-empty",
                "деревня Улица, улица Зеленая, дом 9",
                "деревня Улица",
                "Зеленая",
                "9",
                "зеленая",
                "деревня улица",
                "77",
                "деревня улица",
            ),
        )
        index_streets(db)
    serving = tmp_path / "packed.db"
    compact(path, serving)
    catalog = Catalog(str(serving))
    result = catalog.resolve("деревня Улица улица Зеленая 9")
    assert result["status"] == "no_codes"
    assert result["building_id"].startswith("address:")
    assert catalog.building(result["building_id"])["city"] == "деревня Улица"


def test_a_split_source_alias_does_not_skip_joined_word_candidates_in_another_matching_locality(names):
    path, _ = names
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE buildings SET street='Малая Мичурина',street_key='малая мичурина',"
            "display='Москва, улица Малая Мичурина, дом 11' WHERE id='exact'"
        )
        db.execute(
            "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "satellite",
                "guid-satellite",
                "Щербинка, улица Малая Мичурина, дом 11",
                "Щербинка",
                "Малая Мичурина",
                "11",
                "малая мичурина",
                "щербинка",
                "77",
                "москва щербинка",
            ),
        )
        db.execute("INSERT INTO aliases VALUES('satellite','москва щербинка','малая мичу рина','11')")
        db.execute("INSERT INTO codes VALUES('satellite','2','test','ONLY-satellite')")
        index_streets(db)
        refresh(db)
    result = Catalog(str(path)).resolve("Москва Малая Мичу рина 11")
    assert result["status"] == "ambiguous"
    assert {item["building_id"] for item in result["candidates"]} == {"exact", "satellite"}
