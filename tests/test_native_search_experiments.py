import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.search.engine import Catalog
from tools import benchmark_native_search as experiment
from tools.benchmark_native_search import configurations, mapping, native_body, native_result
from tools.build_catalog import SCHEMA, index_streets


def test_alias_experiment_keeps_house_filter_inside_the_nested_record():
    variant = next(v for v in configurations("aliases") if v["name"] == "aliases1.words.clean_syn-AUTO-r0.8")
    body = native_body("мск, ул.Бианки 10А", variant)
    nested = body["query"]["bool"]["should"][1]["nested"]
    assert nested["query"]["bool"]["filter"] == [{"term": {"aliases.house": "10а"}}]
    assert body["query"]["bool"]["filter"] == [{"term": {"house": "10а"}}]
    assert nested["score_mode"] == "max"
    assert mapping(aliases=True)["mappings"]["properties"]["aliases"]["type"] == "nested"
    values = configurations("aliases")
    assert len(values) == len({v["name"] for v in values}) == 34


def test_source_alias_does_not_transfer_a_match_to_another_building_in_the_same_street(tmp_path):
    path = tmp_path / "aliases.db"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for identifier, house in [("target", "10а"), ("other-location", "10а"), ("other-house", "10б")]:
            db.execute(
                "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    None,
                    "Москва, ул. Тестовая, дом " + house,
                    "Москва",
                    "Тестовая",
                    house,
                    "тестовая",
                    "москва",
                    "77",
                    "москва",
                ),
            )
            db.execute("INSERT INTO codes VALUES(?,?,?,?)", (identifier, "1", "test", "ONLY-" + identifier))
        index_streets(db)
    street_id = hashlib.sha256("москва\0тестовая".encode()).hexdigest()[:32]
    hit = {
        "_id": street_id,
        "_score": 10,
        "inner_hits": {
            "aliases": {
                "hits": {
                    "hits": [
                        {
                            "_source": {"building_id": "target", "street": "старая"},
                            "matched_queries": ["alias_exact", "alias_inner"],
                        }
                    ]
                }
            }
        },
    }
    answer = native_result(Catalog(str(path)), "Старая 10А", [hit], 0.8)
    assert answer["status"] == "found"
    assert answer["building_id"] == "target"
    assert answer["data"] == {"1": [["ONLY-target", "test"]]}


def test_search_http_failure_is_recorded_without_becoming_not_found_or_aborting_other_profiles(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    catalog.write_bytes(b"frozen catalog test")
    digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
    case = {"query": "Тестовая 10", "expected_address": "Москва, улица Тестовая, дом 10", "cohort": "historical"}

    class FakeCatalog:
        def __init__(self, path):
            pass

        def resolve(self, query):
            return {"status": "found", "address": case["expected_address"], "data": {}}

    original_client = httpx.Client

    def endpoint(request):
        if request.url.path.endswith("/_settings"):
            return httpx.Response(200, json={"lab": {"settings": {"index": {"analysis": {}}}}})
        if request.method == "GET":
            return httpx.Response(
                200, json={"lab": {"mappings": {"_meta": {"catalog_sha256": digest, "aliases": True}}}}
            )
        if request.url.path.endswith("/_analyze"):
            tokens = {
                "мск,ул.Бианки": ["москва", "бианки"],
                "мск, пр-кт Солнцевский": ["москва", "солнцевский"],
                "б-р Северный": ["северный"],
            }[json.loads(request.content)["text"]]
            return httpx.Response(200, json={"tokens": [{"token": token} for token in tokens]})
        return httpx.Response(500, json={"error": {"type": "too_many_nested_clauses"}})

    monkeypatch.setattr(
        experiment.httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(endpoint), **kw)
    )
    monkeypatch.setattr(experiment, "Catalog", FakeCatalog)
    monkeypatch.setattr(experiment, "sample", lambda *a: [case])
    monkeypatch.setattr(experiment.replay, "initialize", lambda *a: None)
    monkeypatch.setattr(experiment.replay, "legacy_lookup", lambda *a: None)
    monkeypatch.setattr(experiment.replay, "expected_ids", {})
    output = tmp_path / "results"
    experiment.run(
        SimpleNamespace(
            output=output,
            catalog=catalog,
            url="http://lab",
            index="lab",
            stage="aliases",
            corpus=tmp_path,
            reviewed=tmp_path,
            limit=1,
            full_corpus=False,
            resume_from=None,
            variants="catalog,aliases1.words.clean_syn-AUTO-r1.0",
            legacy=tmp_path,
            workers=1,
        )
    )
    rows = [json.loads(line) for line in (output / "results.jsonl").read_text(encoding="utf8").splitlines()]
    by_name = {row["variant"]: row for row in rows}
    assert by_name["catalog"]["reference_retained"]
    failed = by_name["aliases1.words.clean_syn-AUTO-r1.0"]
    assert failed["status"] == "backend_error"
    assert failed["backend_error"]["http_status"] == 500
    assert not failed["reference_retained"]
    assert (output / "summary.json").exists()


def test_resume_rejects_changed_inputs_dependencies_and_incomplete_prefixes(tmp_path):
    source = Path(experiment.__file__).read_bytes()
    sources = {
        "tools/benchmark_native_search.py": hashlib.sha256(source).hexdigest(),
        "src/search/normalize.py": "fixed",
    }
    variants = [{"name": "catalog"}, {"name": "native"}]
    cases = [{"query": "Тестовая 10"}]
    frozen = {"sha256": "input", "variants": variants, "sources": sources, "catalog_sha256": "catalog"}
    (tmp_path / "frozen-input.json").write_text(json.dumps(frozen), encoding="utf8")
    (tmp_path / "runner_snapshot.py").write_bytes(source)
    (tmp_path / "index-definition-snapshot.json").write_text(
        json.dumps({"index": "lab", "_mapping": {}}), encoding="utf8"
    )
    rows = [{"case_id": 0, "query": cases[0]["query"], "variant": v["name"]} for v in variants]
    (tmp_path / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf8")
    args = (tmp_path, cases, variants, "input", sources, "catalog", "lab", {})
    assert experiment.resume_rows(*args) == rows
    with pytest.raises(ValueError, match="inputs"):
        experiment.resume_rows(tmp_path, cases, variants, "changed", sources, "catalog", "lab", {})
    with pytest.raises(ValueError, match="dependency"):
        experiment.resume_rows(
            tmp_path,
            cases,
            variants,
            "input",
            dict(sources, **{"src/search/normalize.py": "changed"}),
            "catalog",
            "lab",
            {},
        )
    (tmp_path / "results.jsonl").write_text(json.dumps(rows[0]) + "\n", encoding="utf8")
    with pytest.raises(ValueError, match="Incomplete"):
        experiment.resume_rows(*args)
