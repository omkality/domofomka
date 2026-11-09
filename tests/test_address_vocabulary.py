import pytest

from src.code_service.v2.normalize import parse_query, text
from tools.evaluation.evaluate_search import source_fingerprints


@pytest.mark.parametrize(
    ("query", "words", "house"),
    [
        ("Москва восьмая Парковая 23к1", ["8", "парковая"], "23к1"),
        ("Москва двадцать первая Парковая 4с2", ["21", "парковая"], "4с2"),
        ("третья Парковая 2а", ["3", "парковая"], "2а"),
        ("мск Первомайская 12", ["москва", "первомайская"], "12"),
        ("спб Пятницкая 10", ["санкт", "петербург", "пятницкая"], "10"),
    ],
)
def test_language_resources_preserve_number_roles_and_names(query, words, house):
    parsed = parse_query(query)
    assert parsed.house == house
    assert set(words) <= set(parsed.words.split())


def test_aliases_only_replace_whole_words():
    assert text("омский Первомайская") == "омский первомайская"
    assert text("мск") == "москва"


def test_evaluation_fingerprints_include_language_and_versioned_code():
    fingerprints = source_fingerprints()
    for filename in [
        "src/code_service/v1/russian.json",
        "src/code_service/v2/russian.json",
        "src/code_service/v1/matching.py",
        "src/code_service/v2/normalize.py",
        "tools/evaluation/evaluate_search.py",
    ]:
        assert filename in fingerprints
