import pytest

from tools.benchmark_native_search import configurations, mapping, search_variant


def test_scoring_comparison_keeps_documents_analyzers_and_filters_identical():
    baseline = mapping(aliases=True, lexical=True)
    for similarity, b in [("BM25", 0), ("BM25", 0.2), ("BM25", 0.75), ("boolean", 0.75)]:
        definition = mapping(aliases=True, lexical=True, similarity=similarity, bm25_b=b)
        assert definition["mappings"] == baseline["mappings"]
        assert definition["settings"]["analysis"] == baseline["settings"]["analysis"]
        config = definition["settings"]["similarity"]["default"]
        assert config == ({"type": "BM25", "b": b, "k1": 1.2} if similarity == "BM25" else {"type": "boolean"})


@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_invalid_length_normalization_is_rejected_before_index_creation(value):
    with pytest.raises(ValueError, match="between zero and one"):
        mapping(similarity="BM25", bm25_b=value)


def test_disabling_global_known_street_policy_does_not_disable_house_or_street_role_filters():
    profile = next(v for v in configurations("lexical") if v["name"] == "scoring.roles-AUTO-r1.0")
    bodies = []

    class EmptyCatalog:
        def load_candidates(self, *args: object):
            return []

    def search(body):
        bodies.append(body)
        return []

    answer = search_variant(EmptyCatalog(), "3й порковский 1", profile, search, lambda *a: ["3", "порковский"])
    assert answer["status"] == "not_found"
    assert "fallback_rejected" not in answer
    assert len(bodies) == 4
    for body in bodies:
        assert body["_source"] is False
        assert {"term": {"house": "1"}} in body["query"]["bool"]["filter"]
        canonical = body["query"]["bool"]["must"][0]["dis_max"]["queries"][0]["bool"]
        assert canonical["must"][0]["bool"]["should"][0]["match"]["street.plain"]["query"] == "порковский"
