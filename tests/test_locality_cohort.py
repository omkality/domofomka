import json

import pytest

from tools.audit_query_localities import LocalityMatcher
from tools.build_locality_cohort import select_cases, stratify


def event(city, query, dialog="dialog"):
    return {
        "dialog": dialog,
        "query": query,
        "date": 1,
        "next_reply": "no_results",
        "mentions": [
            {
                "key": city.lower(),
                "name": city,
                "category": "city",
                "regions": ["00"],
                "evidence": "unmarked_city",
                "street_name_overlap": False,
            }
        ],
    }


def test_selection_covers_rare_places_without_weighting_by_major_city_traffic():
    rows = [event("Москва", f"Москва Тестовая {n}") for n in range(30)]
    rows += [event("Пронск", "Пронск Тестовая 1", "rare-dialog")]
    cases, report = select_cases(rows, excluded=set(), per_place=3)
    assert sum(c["source_locality"]["name"] == "Москва" for c in cases) == 3
    assert sum(c["source_locality"]["name"] == "Пронск" for c in cases) == 1
    assert report["eligible_places"] == 2
    assert select_cases(reversed(rows), excluded=set(), per_place=3)[0] == cases


def test_selection_preserves_house_identity_and_excludes_existing_queries():
    rows = [
        event("Москва", q)
        for q in ["Москва Тестовая 2-4", "Москва Тестовая 24", "Москва Тестовая 2/4", "Москва Тестовая 2 4"]
    ]
    cases, _ = select_cases(rows, excluded={"москва тестовая 24"}, per_place=8)
    assert {c["query"] for c in cases} == {rows[i]["query"] for i in [0, 2, 3]}
    assert all(not c.get("expected_address") for c in cases)


def test_selection_keeps_city_only_followup_and_does_not_invent_intent():
    cases, _ = select_cases(
        [event("Пронск", "Пронск"), event("Пронск", "Пронск Тестовая 1")], excluded=set(), per_place=2
    )
    assert {c["query"] for c in cases} == {"Пронск", "Пронск Тестовая 1"}
    assert all(c["cohort"] == "observed_unanswered_locality" for c in cases)


def test_selection_separates_multiple_places_and_street_like_mentions():
    ambiguous = event("Волжский", "Волжский бульвар 5")
    ambiguous["mentions"][0]["evidence"] = "street_or_locality"
    multiple = event("Москва", "Москва или Пронск 5")
    multiple["mentions"].append(event("Пронск", "unused")["mentions"][0])
    cases, report = select_cases([ambiguous, multiple], excluded=set(), per_place=2)
    assert cases == []
    assert report["skipped:street_context"] == 1
    assert report["skipped:multiple_names"] == 1


def test_strata_do_not_get_a_city_from_the_old_bot_answer(tmp_path):
    matcher = LocalityMatcher([{"name": "Москва", "kind": "г", "region": "77", "level": 1}])
    frozen = {
        "cases": [{"query": "Тестовая 5", "expected_address": "Москва, улица Тестовая, дом 5"}],
        "variants": [{"name": "catalog"}],
    }
    row = {
        "case_id": 0,
        "variant": "catalog",
        "query": "Тестовая 5",
        "cohort": "historical",
        "status": "found",
        "reference_retained": True,
        "backend_error": None,
        "literal_conflict": None,
    }
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps(row) + "\n")
    summary = stratify(frozen, results, matcher, [])
    assert summary["cases_without_locality_mention"] == 1
    assert summary["by_place"] == []


def test_stratification_rejects_missing_result_rows(tmp_path):
    results = tmp_path / "results.jsonl"
    results.write_text("")
    with pytest.raises(AssertionError):
        stratify(
            {"cases": [{"query": "Тестовая 5"}], "variants": [{"name": "catalog"}]}, results, LocalityMatcher([]), []
        )
