import pytest

from tools import audit_search_code_coverage as audit
from tools.audit_search_code_coverage import code_values, offered_codes


def test_retained_address_can_still_lose_a_code():
    legacy = {"data": {"1": [["a", "old"], ["b", "old"], ["c", "old"]]}}
    result = {"status": "found", "engine": "v2", "building_id": "one"}
    actual = offered_codes(result, legacy, lambda _: {("1", "old", "a"), ("1", "old", "b")})
    assert code_values(legacy) - actual == {("1", "old", "c")}


def test_separate_choices_preserve_coverage_without_merging_identities():
    result = {"status": "ambiguous", "candidates": [{"building_id": "one"}, {"building_id": "two"}]}
    codes = {"one": {("1", "old", "a")}, "two": {("2", "old", "b")}}
    assert offered_codes(result, {}, codes.__getitem__) == {("1", "old", "a"), ("2", "old", "b")}


def test_v1_snapshot_uses_real_codes_even_when_replay_omits_them():
    legacy = {"data": {"1": [["a", "old"], ["a", "old"]]}}
    result = {"status": "found", "engine": "v1", "data": {}}
    assert offered_codes(result, legacy, lambda _: set()) == {("1", "old", "a")}


def test_no_codes_is_not_a_delivered_code_even_if_identity_is_known():
    assert offered_codes({"status": "no_codes", "building_id": "one"}, {}, lambda _: {("1", "old", "a")}) == set()


def test_frozen_v2_baseline_detects_replaced_codes_even_when_the_count_is_unchanged(monkeypatch):
    monkeypatch.setattr(
        audit,
        "worker_context",
        {
            "codes": {"old": {("1", "test", "a"), ("1", "test", "b")}, "new": {("1", "test", "a"), ("1", "test", "c")}},
            "legacy": "unused",
            "index": "unused",
            "baseline": "before/catalog",
        },
    )
    monkeypatch.setattr(audit, "lookup", lambda *_: {"data": {}})
    rows = [
        {
            "case_id": 0,
            "variant": variant,
            "status": "found",
            "reference_retained": True,
            "result": {"status": "found", "engine": "v2", "building_id": bid},
        }
        for variant, bid in [("before/catalog", "old"), ("after/catalog", "new")]
    ]
    before, after = audit.compare(("synthetic query", rows))
    assert before["offered_unique_codes"] == after["offered_unique_codes"] == 2
    assert after["baseline_missing_unique_codes"] == after["baseline_extra_unique_codes"] == 1
    assert not after["legacy_has_codes"]
    with pytest.raises(ValueError, match="Baseline variant is missing"):
        audit.compare(("synthetic query", rows[1:]))
