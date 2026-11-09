import copy
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager

import pytest

from tools.experiments.benchmark_native_search import mapping, reviewed_conflict
from tools.experiments.search_city_roles import (
    city_field,
    city_probe,
    confirmation,
    constrain_result,
    extend_mapping,
    indexed_document,
    probe_places,
    role_locality_matches,
    source_choices,
    street_terms,
)


def test_city_schema_uses_keyword_aggregation_and_keeps_source_city_separate():
    definition = mapping(aliases=True, lexical=True)
    old = copy.deepcopy(definition)
    extend_mapping(definition)
    props = definition["mappings"]["properties"]
    assert props["city"]["fields"]["key"] == {"type": "keyword"}
    assert props["aliases"]["properties"]["city"]["fields"]["key"] == {"type": "keyword"}
    assert definition["settings"] == old["settings"]
    assert "fielddata" not in props["city"]


def test_source_city_is_derived_from_the_stored_boundary_without_changing_old_fields():
    original = {
        "id": "s",
        "city": "тестоград",
        "aliases": [{"building_id": "b", "words": "поселок примерово учебная", "street": "учебная", "house": "4"}],
    }
    updated = indexed_document(original)
    assert updated["aliases"][0]["city"] == "поселок примерово"
    assert "city" not in original["aliases"][0]
    reduced = copy.deepcopy(updated)
    del reduced["aliases"][0]["city"]
    assert reduced == original
    with pytest.raises(ValueError, match="boundary"):
        indexed_document({"aliases": [{"words": "тестоград учебная", "street": "другая"}]})


def test_city_field_needs_structural_evidence_and_preserves_unknown_spelling():
    known = {"москва", "тестоград", "санкт петербург"}.__contains__
    assert city_field("Тестогад, улица Учебная, дом 4", known) == "тестогад"
    assert city_field("ЇМосква, Учебная улица, 4", known) == "їмосква"
    assert city_field("г. Москва Учебное шоссе 4", known) == "москва"
    assert city_field("Москва, улица Учебная 4", known) == "москва"
    for query in ["Московская улица, 4", "Зеленоград, корпус 902", "Дом 4, улица Учебная", "Учебная, 4, Тестоград"]:
        assert city_field(query, known) is None
    assert city_field("Учебный, проезд 4к1", known) is None
    assert city_field("Тестогад, площадь Учебная, дом 4", known) == "тестогад"
    assert city_field("Рабочий посёлок Тестоград, улица Учебная, дом 4", known) == "тестоград"
    assert city_field("посёлок городского типа Тестоград, улица Учебная, дом 4", known) == "тестоград"
    assert city_field("поселок Совхоз им, Учебного, дом 4", known) is None


def test_official_settlement_element_is_a_locality_even_if_stored_after_its_parent():
    row = {
        "id": "address:synthetic",
        "city": "Родительский",
        "lineage": "родительский",
        "street": "Примерово",
        "display": "Родительский, пгт. Примерово, дом 4",
    }
    assert role_locality_matches("примерово", row)
    assert not role_locality_matches("примерово", dict(row, display="Родительский, улица Примерово, дом 4"))
    assert role_locality_matches(
        "совхоза имени учебного", dict(row, display="Родительский, п. Совхоза им. Учебного, дом 4")
    )
    assert not role_locality_matches("совхоза имени учебного", dict(row, display="Родительский, улица Учебного, дом 4"))


def test_review_can_require_the_same_known_uncoded_building():
    case = {"expected_status": "no_codes", "expected_building_id": "expected"}
    assert reviewed_conflict(case, {"status": "no_codes", "building_id": "other"}, None)
    assert reviewed_conflict(case, {"status": "not_found"}, None)


def test_city_token_removal_preserves_a_repeated_street_word_and_numeric_name():
    assert street_terms(["тестоград", "тестоград", "1"], ["тестоград"]) == ["тестоград", "1"]
    assert street_terms(["тестогад", "1"], ["тестогад"]) == ["1"]


def test_numbered_street_with_a_separate_city_does_not_need_a_city_word_inside_the_street():
    from tools.experiments.benchmark_native_search import configurations, native_body

    variant = next(v for v in configurations("lexical") if v["name"] == "locality.roles-sources-AUTO-r1.0")
    body = native_body(
        "Тестогад, микрорайон 1-й, дом 30к1", variant, analyzed_tokens=["тестогад", "1"], street_tokens=["1"]
    )
    block = body["query"]["bool"]["must"][0]["dis_max"]["queries"][0]["bool"]
    alternatives = block["must"][0]["bool"]["should"]
    assert len(alternatives) == 1
    assert alternatives[0]["match"]["street.plain"]["query"] == "1"
    assert alternatives[0]["match"]["street.plain"]["minimum_should_match"] == "100%"


def test_city_fuzziness_is_applied_to_city_fields_and_aggregated_by_full_place():
    body = city_probe("тестогад", fuzziness="AUTO", limit=32)
    assert body["size"] == 0
    assert body["aggs"]["canonical"]["aggs"]["places"]["terms"] == {"field": "city.key", "size": 32}
    options = body["aggs"]["canonical"]["filter"]["match"]["city.plain"]
    assert options["operator"] == "and"
    assert options["fuzziness"] == "AUTO"
    assert "street" not in str(body)


def test_probe_cannot_hide_truncation_or_turn_a_failure_into_a_negative_result():
    result = {
        "timed_out": False,
        "_shards": {"failed": 0},
        "aggregations": {
            "canonical": {"places": {"buckets": [{"key": "тестоград"}], "sum_other_doc_count": 0}},
            "source": {"matched": {"places": {"buckets": [{"key": "поселок примерово"}], "sum_other_doc_count": 3}}},
        },
    }
    assert probe_places(result) == ({"тестоград", "поселок примерово"}, True)
    with pytest.raises(RuntimeError, match="incomplete"):
        probe_places(dict(result, timed_out=True))


def test_corrected_city_always_requires_confirmation_and_keeps_the_building_id():
    answer = {"status": "found", "building_id": "gar:test", "address": "Тестоград, улица Учебная, дом 4", "data": {}}
    choice = confirmation(answer)
    assert choice["status"] == "ambiguous"
    assert choice["candidates"] == [{"building_id": "gar:test", "address": answer["address"]}]
    assert choice["clarification_reason"] == "locality_spelling"
    assert answer["status"] == "found"
    missing = confirmation({"status": "not_found", "data": {}})
    assert missing["locality_confirmation_required"]


def test_catalog_fallback_cannot_reintroduce_a_different_city_after_the_probe():
    class FakeCatalog:
        def building(self, key):
            return {"id": key, "city": "Тестоград" if key == "right" else "Другой", "lineage": "", "street": "Учебная"}

        @contextmanager
        def connect(self) -> Generator[sqlite3.Connection, None, None]:
            with sqlite3.connect(":memory:") as db:
                db.execute("CREATE TABLE aliases(building_id TEXT,city_key TEXT,street_key TEXT)")
                yield db

    catalog = FakeCatalog()
    answer = {
        "status": "ambiguous",
        "candidates": [{"building_id": key, "address": key + ", улица Учебная, дом 4"} for key in ["wrong", "right"]],
    }
    assert constrain_result(answer, catalog, {"тестоград"})["candidates"] == [answer["candidates"][1]]
    assert constrain_result(answer, catalog, set())["status"] == "not_found"
    assert (
        confirmation({"status": "no_codes", "building_id": "right", "address": "Тестоград, Учебная 4"})["status"]
        == "no_codes_ambiguous"
    )


def test_partial_name_keeps_separate_source_choices_only_with_a_shared_building():
    native = {"status": "found", "building_id": "main", "address": "Тестоград, улица Учебная, дом 4"}
    baseline = {
        "status": "ambiguous",
        "candidates": [
            {"building_id": "main", "address": native["address"]},
            {"building_id": "source", "address": "Тестоград, улица Учебная (старый источник), дом 4"},
        ],
    }
    answer = source_choices(native, baseline)
    assert answer["status"] == "ambiguous"
    assert {row["building_id"] for row in answer["candidates"]} == {"main", "source"}
    assert source_choices(dict(native, building_id="different"), baseline) == dict(native, building_id="different")
    bounded = source_choices(native, baseline, limit=1)
    assert bounded["candidate_truncated"]
    assert len(bounded["candidates"]) == 1


def test_review_can_forbid_an_observed_wrong_city_without_inventing_the_right_one():
    case = {"allowed_statuses": ["not_found", "ambiguous"], "forbidden_city": "Другой"}
    assert not reviewed_conflict(case, {"status": "not_found"}, None)
    assert reviewed_conflict(case, {"status": "found", "address": "Тестоград, улица Учебная, дом 4"}, None)
    assert reviewed_conflict(
        case, {"status": "ambiguous", "candidates": [{"address": "Другой, улица Учебная, дом 4"}]}, None
    )


def test_fuzzy_code_match_does_not_silently_override_an_existing_uncoded_name():
    native = {"status": "found", "building_id": "other", "address": "Другой, улица Похожая, дом 4"}
    answer = source_choices(native, {"status": "no_codes_ambiguous", "data": {}})
    assert answer["status"] == "ambiguous"
    assert answer["clarification_reason"] == "street_spelling"
    assert "locality_confirmation_required" not in answer
