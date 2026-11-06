import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from tools import benchmark_native_search as experiment
from tools.benchmark_native_search import configurations, native_body, search_variant


def guarded():
    return next(v for v in configurations("lexical") if v["name"] == "lexical.analyzer_guarded-AUTO-r1.0")


def test_number_alone_cannot_replace_the_alphabetic_street_name():
    body = native_body("3й порковский 1", guarded(), analyzed_tokens=["3", "порковский"])
    canonical = body["query"]["bool"]["must"][0]["dis_max"]["queries"][0]["bool"]
    alternatives = canonical["must"][0]["bool"]["should"]
    street = alternatives[0]["match"]["street.plain"]
    assert street["query"] == "порковский"
    assert street["analyzer"] == "whitespace"
    assert alternatives[1]["match"]["city.plain"]["fuzziness"] == 0
    assert "must" not in canonical["filter"][0]


def test_known_street_with_unknown_house_does_not_start_fuzzy_search():
    calls = []

    class EmptyCatalog:
        def load_candidates(self, *a: object):
            return []

    def search(body):
        calls.append(body)
        if body["_source"] == ["city", "street"]:
            return [{"_id": "street", "_score": 1, "_source": {"city": "москва", "street": "тестовая"}}]
        return []

    answer = search_variant(
        EmptyCatalog(), "Москва Тестовая 999к2", guarded(), search, lambda *a: ["москва", "тестовая"]
    )
    assert len(calls) == 3
    assert answer["status"] == "not_found"
    assert answer["fallback_rejected"] == "known_street_without_requested_house"


def test_long_multi_address_input_is_rejected_before_search_and_expansions_are_bounded():
    def unexpected_search(body):
        raise AssertionError("Complex input must not search")

    result = search_variant(None, "Тестовая 1", guarded(), unexpected_search, lambda *a: ["адрес"] * 25)
    assert result["status"] == "invalid_query"
    assert result["query_budget"]["reason"] == "too_many_terms"
    bodies = []

    class EmptyCatalog:
        def load_candidates(self, *a: object):
            return []

    def search(body):
        bodies.append(body)
        return []

    result = search_variant(EmptyCatalog(), "Тестовая 1", guarded(), search, lambda *a: ["слово"] * 24)
    assert result["query_budget"]["max_expansions"] == 8
    assert result["query_budget"]["terms"] == 24
    assert len(bodies) == 5


@pytest.mark.parametrize(
    ("legacy", "status"),
    [
        ("Москва, улица Тестовая, дом 2", "found"),
        ("Москва, улица Садовая Тестовая, дом 2", "not_found"),
        ("Москва, площадь Тестовая, дом 2", "not_found"),
    ],
)
def test_known_street_guard_only_accepts_legacy_with_the_same_complete_name_and_type(
    tmp_path, monkeypatch, legacy, status
):
    path = tmp_path / "catalog"
    path.write_bytes(b"catalog")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    class EmptyCatalog:
        def __init__(self, *a: object):
            pass

        def load_candidates(self, *a: object):
            return []

    original = httpx.Client

    def endpoint(request):
        if request.url.path == "/":
            return httpx.Response(200, json={"version": {"number": "test"}})
        if request.url.path.endswith("/_mapping"):
            return httpx.Response(
                200, json={"lab": {"mappings": {"_meta": {"catalog_sha256": digest, "aliases": True}}}}
            )
        if request.url.path.endswith("/_settings"):
            return httpx.Response(200, json={"lab": {"settings": {"index": {"analysis": {}}}}})
        body = json.loads(request.content)
        if request.url.path.endswith("/_analyze"):
            return httpx.Response(200, json={"tokens": [{"token": word} for word in body["text"].lower().split()]})
        hits = (
            [{"_id": "street", "_score": 1, "_source": {"city": "москва", "street": "тестовая"}}]
            if body.get("_source") == ["city", "street"]
            else []
        )
        return httpx.Response(200, json={"hits": {"hits": hits}})

    monkeypatch.setattr(
        experiment.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(endpoint), **kw)
    )
    monkeypatch.setattr(experiment, "Catalog", EmptyCatalog)
    monkeypatch.setattr(experiment, "analysis_probes", lambda *a: [])
    monkeypatch.setattr(experiment.search_lexical_profiles, "probes", lambda *a: [])
    monkeypatch.setattr(experiment.replay, "initialize", lambda *a: None)
    monkeypatch.setattr(experiment.replay, "legacy_lookup", lambda *a: legacy)
    monkeypatch.setattr(experiment.replay, "expected_ids", {})
    monkeypatch.setattr(
        experiment,
        "sample",
        lambda *a: [{"query": "Москва улица Тестовая 2", "expected_address": legacy, "cohort": "historical"}],
    )
    output = tmp_path / "result"
    experiment.run(
        SimpleNamespace(
            output=output,
            catalog=path,
            url="http://lab",
            index="lab",
            stage="lexical",
            corpus=tmp_path,
            reviewed=tmp_path,
            limit=1,
            full_corpus=False,
            resume_from=None,
            variants="lexical.python_guarded-AUTO-r1.0",
            legacy=path,
            workers=1,
            details=True,
        )
    )
    row = json.loads((output / "results.jsonl").read_text(encoding="utf8"))
    assert row["status"] == status
    if status == "found":
        assert row["engine"] == "v1"
    else:
        assert row["result"]["fallback_rejected"] == "known_street_without_requested_house"
