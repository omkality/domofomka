import hashlib
import sqlite3

import pytest

from src.search.engine import Catalog
from src.search.normalize import address_words, parse_query
from tools.benchmark_native_search import configurations, mapping, native_body, native_result
from tools.build_catalog import SCHEMA, index_streets
from tools.search_lexical_profiles import expanded_ordinals, ordinal_rules


def test_unicode_control_preserves_the_typo_inside_a_word_without_changing_default_cleanup():
    assert address_words("Конëнкова") == "кон нкова"
    assert address_words("Конëнкова", preserve_unicode_letters=True) == "конëнкова"
    assert address_words("улица__Конёнкова", preserve_unicode_letters=True) == "коненкова"


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [("восьмыми парковая", "8 парковая"), ("двадцать третьих парковая", "23 парковая"), ("первых мая", "1 мая")],
)
def test_python_control_uses_the_same_expanded_ordinal_vocabulary_as_the_analyzer(spelling, expected):
    assert expanded_ordinals(spelling) == expected
    number = expected.split()[0]
    assert spelling.rsplit(" ", 1)[0] + " => " + number in ordinal_rules()


@pytest.mark.parametrize(
    ("message", "spelling", "words", "house"),
    [
        ("третья парковая 10к1", "третья", "3 парковая", "10к1"),
        ("двадцать третья парковая 10", "двадцать третья", "23 парковая", "10"),
        ("восьмая парковая 14", "восьмая", "8 парковая", "14"),
        ("8-я парковая 14", "8-я", "8 парковая", "14"),
        ("8-я парковая дом третий", "8-я", "8 парковая", "3"),
        ("первая третья улица 10", "первая третья", "1 3", "10"),
    ],
)
def test_spelling_for_analyzers_preserves_house_and_street_number_roles(message, spelling, words, house):
    normal = parse_query(message)
    lexical = parse_query(message, preserve_lexical_ordinals=True)
    assert spelling in lexical.search_text
    assert (lexical.raw, lexical.words, lexical.house, lexical.kind, lexical.entrance) == (
        normal.raw,
        words,
        house,
        normal.kind,
        normal.entrance,
    )
    assert normal.words == words
    assert normal.house == house


def test_python_and_analyzer_profiles_use_the_same_index_fields_and_house_filter():
    values = {v["name"]: v for v in configurations("lexical")}
    python = native_body("мск восьмая парковая 14к2", values["lexical.python-AUTO-r1.0"])
    analyzer = native_body("мск восьмая парковая 14к2", values["lexical.analyzer-AUTO-r1.0"])
    for value in [python, analyzer]:
        assert value["query"]["bool"]["filter"] == [{"term": {"house": "14к2"}}]

    def match(value):
        return value["query"]["bool"]["should"][0]["bool"]["must"][0]["match"]["words.plain"]

    assert match(python)["query"] == "москва 8 парковая"
    assert match(analyzer)["query"].strip() == "мск восьмая парковая"
    assert match(python)["fuzziness"] == match(analyzer)["fuzziness"] == "AUTO"
    assert mapping(aliases=True)["mappings"] == mapping(aliases=True, lexical=True)["mappings"]


def test_street_numbers_come_from_the_selected_analyzer_but_cannot_match_a_neighbor(tmp_path):
    path = tmp_path / "numbers.db"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for number in ["8", "9"]:
            db.execute(
                "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    number,
                    "guid-" + number,
                    f"Москва, ул. {number}-я Парковая, дом 14",
                    "Москва",
                    number + "-я Парковая",
                    "14",
                    number + " парковая",
                    "москва",
                    "77",
                    "москва",
                ),
            )
            db.execute("INSERT INTO codes VALUES(?,?,?,?)", (number, "1", "test", "ONLY-" + number))
        index_streets(db)
    hits = [
        {"_id": hashlib.sha256(("москва\0" + n + " парковая").encode()).hexdigest()[:32], "_score": 10}
        for n in ["8", "9"]
    ]
    catalog = Catalog(str(path))
    assert native_result(catalog, "восьмым парковая 14", hits, 0.8, query_numbers=set())["status"] == "not_found"
    result = native_result(catalog, "восьмым парковая 14", hits, 0.8, query_numbers={"8"})
    assert result["building_id"] == "8"
    assert result["data"] == {"1": [["ONLY-8", "test"]]}
