"""Локальная матрица OpenSearch: независимое ранжирование, общий разбор ролей и точный дом.

Переписки и построчные результаты остаются в artifacts/private. Рабочий API не переключается.
"""

import argparse
import ast
import collections
import concurrent.futures
import copy
import hashlib
import itertools
import json
import re
import threading
import time
from pathlib import Path

import httpx
from rapidfuzz.distance import DamerauLevenshtein

from src.search.engine import MAPPING, Catalog, documents, search_body
from src.search.normalize import (
    STOP,
    address_words,
    building_kind_matches,
    legacy_house_matches,
    parse_query,
)
from tools import evaluate_search as replay
from tools.compare_postgres_catalog import select
from tools.quality_suite import address_fields, literal_words


def mapping(*, aliases=False):
    value = copy.deepcopy(MAPPING)
    analysis = value["settings"]["analysis"]
    analysis["filter"].update(
        address_stop={"type": "stop", "stopwords": STOP},
        city_aliases={
            "type": "synonym_graph",
            "synonyms": [
                "мск => москва",
                "спб => санкт петербург",
                "питер => санкт петербург",
                "санктпетербург => санкт петербург",
            ],
        },
    )
    analysis["analyzer"].update(
        plain={"type": "custom", "tokenizer": "standard", "filter": ["lowercase"]},
        query_plain={
            "type": "custom",
            "tokenizer": "standard",
            "filter": ["lowercase", "address_stop"],
        },
        query_stem={
            "type": "custom",
            "tokenizer": "standard",
            "filter": ["lowercase", "address_stop", "russian_stemmer"],
        },
        query_synonyms={
            "type": "custom",
            "tokenizer": "standard",
            "filter": ["lowercase", "city_aliases", "address_stop", "russian_stemmer"],
        },
    )
    value["mappings"]["properties"]["words"].update(
        search_analyzer="query_stem",
        fields={
            "plain": {
                "type": "text",
                "analyzer": "plain",
                "search_analyzer": "query_plain",
            },
            "synonyms": {
                "type": "text",
                "analyzer": "address",
                "search_analyzer": "query_synonyms",
            },
        },
    )
    for field in ("city", "street"):
        value["mappings"]["properties"][field] = copy.deepcopy(value["mappings"]["properties"]["words"])
    value["mappings"]["properties"]["coded_house"] = {"type": "keyword"}
    if aliases:
        analysis["char_filter"] = {
            "yo": {"type": "mapping", "mappings": ["ё => е", "Ё => Е"]},
            "avenue": {"type": "pattern_replace", "pattern": r"(?iuU)\bпр[.\s-]*кт\b", "replacement": "проспект"},
            "boulevard": {"type": "pattern_replace", "pattern": r"(?iuU)\bб[.-]р\b", "replacement": "бульвар"},
            "separators": {"type": "pattern_replace", "pattern": r"[^\p{L}\p{N}/]+", "replacement": " "},
        }
        for name, filters in {
            "clean": ["lowercase", "address_stop"],
            "clean_syn": ["lowercase", "city_aliases", "address_stop"],
            "clean_stem": ["lowercase", "city_aliases", "address_stop", "russian_stemmer"],
        }.items():
            analysis["analyzer"][name] = {
                "type": "custom",
                "tokenizer": "standard",
                "char_filter": list(analysis["char_filter"]),
                "filter": filters,
            }
            for field in ("words", "street", "city"):
                value["mappings"]["properties"][field]["fields"][name] = {
                    "type": "text",
                    "analyzer": "address" if name == "clean_stem" else "plain",
                    "search_analyzer": name,
                }
        value["mappings"]["properties"]["aliases"] = {
            "type": "nested",
            "properties": {
                "building_id": {"type": "keyword"},
                "house": {"type": "keyword"},
                "words": copy.deepcopy(value["mappings"]["properties"]["words"]),
                "street": copy.deepcopy(value["mappings"]["properties"]["street"]),
            },
        }
    return value


def configurations(stage="initial"):
    values = [
        {"name": "catalog", "mode": "catalog"},
        {"name": "hybrid", "mode": "hybrid"},
    ]
    for field, fuzziness, prefix in itertools.product(
        ["words", "words.plain", "words.synonyms"], ["1", "AUTO", "AUTO:4,7"], [0, 1]
    ):
        values.append(
            {
                "name": f"{field}-{fuzziness}-p{prefix}",
                "mode": "native",
                "field": field,
                "fuzziness": fuzziness,
                "prefix": prefix,
                "expansions": 50,
                "minimum": "100%",
                "ratio": 0.95,
            }
        )
    for expansions, minimum in [(25, "100%"), (100, "100%"), (50, "80%")]:
        for field in ["words.plain", "words.synonyms"]:
            values.append(
                {
                    "name": f"{field}-AUTO-e{expansions}-m{minimum}",
                    "mode": "native",
                    "field": field,
                    "fuzziness": "AUTO",
                    "prefix": 0,
                    "expansions": expansions,
                    "minimum": minimum,
                    "ratio": 0.95,
                }
            )
    if stage == "structured":
        values = values[:2] + [v for v in values if v["name"] == "words.plain-1-p0"]
        for field, fuzziness, ratio in itertools.product(
            ["words", "words.plain", "words.synonyms"], ["1", "AUTO"], [1.0, 0.8, 0.0]
        ):
            values.append(
                {
                    "name": f"structured.{field}-{fuzziness}-r{ratio}",
                    "mode": "structured",
                    "field": field,
                    "fuzziness": fuzziness,
                    "prefix": 0,
                    "expansions": 50,
                    "minimum": "100%",
                    "ratio": ratio,
                }
            )
    if stage == "aliases":
        values = configurations("structured")[:2]
        for aliases, field, fuzziness, ratio in itertools.product(
            [False, True],
            ["words.plain", "words.clean", "words.clean_syn", "words.clean_stem"],
            ["1", "AUTO"],
            [1.0, 0.8],
        ):
            values.append(
                {
                    "name": f"aliases{int(aliases)}.{field}-{fuzziness}-r{ratio}",
                    "mode": "aliases",
                    "aliases": aliases,
                    "field": field,
                    "fuzziness": fuzziness,
                    "prefix": 0,
                    "expansions": 50,
                    "minimum": "100%",
                    "ratio": ratio,
                }
            )
    return values


def native_body(message, variant):
    query = parse_query(message)
    match = {
        "query": query.search_text,
        "minimum_should_match": variant["minimum"],
        "fuzziness": variant["fuzziness"],
        "prefix_length": variant["prefix"],
        "max_expansions": variant["expansions"],
        "fuzzy_transpositions": True,
    }
    exact = {
        "query": query.search_text,
        "operator": "and",
        "fuzziness": 0,
        "boost": 4,
        "_name": "exact",
    }
    body = {
        "size": 100,
        "_source": False,
        "query": {
            "bool": {
                "filter": [{"term": {"house": query.house or ""}}],
                "must": [{"match": {variant["field"]: match}}],
                "should": [{"match": {variant["field"]: exact}}],
            }
        },
    }
    if variant["mode"] in {"structured", "aliases"}:
        street_field = variant["field"].replace("words", "street")
        lexical = (
            query.search_text
            if variant["mode"] == "aliases"
            else " ".join(word for word in query.words.split() if not word.isdigit())
        )
        street_match = dict(match, query=lexical, minimum_should_match=1)
        body["query"]["bool"]["must"].append({"match": {street_field: street_match}})
        body["query"]["bool"]["should"].append({"match": {street_field: dict(street_match, fuzziness=0, boost=4)}})
    if variant.get("aliases"):
        canonical = body["query"]
        canonical["bool"]["_name"] = "canonical"
        nested = copy.deepcopy(canonical["bool"])
        nested["_name"] = "alias_inner"
        nested["filter"] = [{"term": {"aliases.house": query.house or ""}}]
        for clause in [*nested["must"], *nested["should"]]:
            field, options = next(iter(clause["match"].items()))
            if "_name" in options:
                options["_name"] = "alias_" + options["_name"]
            clause["match"] = {"aliases." + field: options}
        body["query"] = {
            "bool": {
                "filter": [{"term": {"house": query.house or ""}}],
                "minimum_should_match": 1,
                "should": [
                    canonical,
                    {
                        "nested": {
                            "path": "aliases",
                            "score_mode": "max",
                            "query": {"bool": nested},
                            "inner_hits": {"size": 100, "_source": ["aliases.building_id", "aliases.street"]},
                        }
                    },
                ],
            }
        }
    return body


def build_index(catalog, url, index, *, aliases=False):
    if not index.startswith("domofomka-native-lab-"):
        raise ValueError("Only isolated experiment indexes are allowed")
    with httpx.Client(base_url=url, trust_env=False, timeout=120) as client:
        if client.head("/" + index).status_code != 404:
            raise FileExistsError("Preserve the previous experiment index")
        index_mapping = mapping(aliases=aliases)
        with catalog.open("rb") as stream:
            revision = hashlib.file_digest(stream, "sha256").hexdigest()
        index_mapping["mappings"]["_meta"] = {"catalog_sha256": revision, "aliases": aliases}
        client.put("/" + index, json=index_mapping).raise_for_status()
        if aliases and not all(probe["passed"] for probe in analysis_probes(client, index)):
            raise ValueError("Analyzer probes failed before bulk indexing")
        batch, total = [], 0
        by_street = collections.defaultdict(list)
        if aliases:
            with Catalog(str(catalog)).connect() as db:
                for row in db.execute(
                    "SELECT a.*,b.city_key AS canonical_city,b.street_key AS canonical_street "
                    "FROM aliases a JOIN buildings b ON b.id=a.building_id"
                ):
                    by_street[row["canonical_city"], row["canonical_street"]].append(
                        {
                            "building_id": row["building_id"],
                            "house": row["house"],
                            "words": row["city_key"] + " " + row["street_key"],
                            "street": row["street_key"],
                        }
                    )

        def flush():
            payload = "".join(
                json.dumps({"index": {"_index": index, "_id": doc["id"]}})
                + "\n"
                + json.dumps(doc, ensure_ascii=False)
                + "\n"
                for doc in batch
            )
            response = client.post(
                "/_bulk",
                content=payload.encode(),
                headers={"Content-Type": "application/x-ndjson"},
            )
            response.raise_for_status()
            if response.json().get("errors"):
                raise RuntimeError("Experiment bulk indexing failed")

        for document in documents(str(catalog), structured=True):
            if aliases:
                document["aliases"] = by_street.pop((document["city"], document["street"]), [])
            batch.append(document)
            total += 1
            if len(batch) == 2000:
                flush()
                batch.clear()
            if total % 100000 == 0:
                print(json.dumps({"indexed": total}), flush=True)
        if batch:
            flush()
        client.post("/" + index + "/_refresh").raise_for_status()
        assert client.get("/" + index + "/_count").json()["count"] == total
        assert not by_street
    return {"documents": total, "index": index}


def analysis_probes(client, index):
    probes = []
    for query, expected in [
        ("мск,ул.Бианки", ["москва", "бианки"]),
        ("мск, пр-кт Солнцевский", ["москва", "солнцевский"]),
        ("б-р Северный", ["северный"]),
    ]:
        response = client.post("/" + index + "/_analyze", json={"analyzer": "clean_syn", "text": query})
        response.raise_for_status()
        tokens = [token["token"] for token in response.json()["tokens"]]
        probes.append({"text": query, "expected": expected, "actual": tokens, "passed": tokens == expected})
    return probes


def native_result(catalog, message, hits, ratio):
    query = parse_query(message)
    if not query.house or not query.words:
        return {"status": "incomplete_query", "data": {}}
    scores = {hit["_id"]: hit["_score"] for hit in hits}
    hit_by_id = {hit["_id"]: hit for hit in hits}
    exact = {
        hit["_id"]
        for hit in hits
        if "exact" in hit.get("matched_queries", [])
        or any(
            "alias_exact" in inner.get("matched_queries", [])
            for inner in hit.get("inner_hits", {}).get("aliases", {}).get("hits", {}).get("hits", [])
        )
    }
    rows = catalog.load_candidates(list(scores), query.house)
    eligible = []
    query_numbers = {word for word in query.words.split() if word.isdigit()}
    for row in rows:
        if row["house"] != query.house:
            continue
        sid = hashlib.sha256((row["city_key"] + "\0" + row["street_key"]).encode()).hexdigest()[:32]
        hit = hit_by_id.get(sid, {})
        alias_hits = hit.get("inner_hits", {}).get("aliases", {}).get("hits", {})
        if alias_hits and "canonical" not in hit.get("matched_queries", []):
            matching = [a["_source"] for a in alias_hits["hits"] if a["_source"]["building_id"] == row["id"]]
            if not matching:
                continue
            street = matching[0]["street"]
        else:
            street = row["street"]
        street_numbers = {word for word in address_words(street).split() if word.isdigit()}
        all_numbers = {word for word in address_words(row["city"] + " " + row["street"]).split() if word.isdigit()}
        if not street_numbers <= query_numbers or not query_numbers <= all_numbers:
            continue
        if not building_kind_matches(query, row):
            continue
        if not row["guid"] and re.search(r"\b(?:улица|проспект|шоссе|переулок|проезд)\b", row["city"], re.I):
            continue
        if sid in scores:
            eligible.append((sid, row))
    if any(sid in exact for sid, _ in eligible):
        eligible = [(sid, row) for sid, row in eligible if sid in exact]
    if not eligible:
        return {"status": "not_found", "data": {}}
    best = max(scores[sid] for sid, _ in eligible)
    close = [row for sid, row in eligible if scores[sid] >= best * ratio]
    if len(close) > 1:
        with catalog.connect() as db:
            coded = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT building_id FROM codes WHERE building_id IN ("
                    + ",".join("?" for _ in close)
                    + ")",
                    [row["id"] for row in close],
                )
            }
        if coded:
            close = [row for row in close if row["id"] in coded]
        else:
            return {"status": "no_codes_ambiguous", "data": {}}
    if len(close) == 1:
        return catalog.answer(close[0], query.entrance)
    return {
        "status": "ambiguous",
        "data": {},
        "candidates": [{"building_id": row["id"], "address": row["display"]} for row in close[:100]],
    }


def sample(corpus, reviewed, limit, full=False):
    records, seen = [], set()
    success = [
        json.loads(line) for line in (corpus / "successful-queries.jsonl").read_text(encoding="utf8").splitlines()
    ]
    success.sort(key=lambda row: hashlib.sha256(row["query"].encode()).hexdigest())
    for row in success:
        if row["query"] not in seen:
            records.append(dict(row, cohort="historical"))
            seen.add(row["query"])
        if len(records) >= limit:
            break
    if full:
        records = [
            dict(
                row,
                cohort=("historical" if row["evidence"] == "bot_address_response" else "correction_hypothesis"),
            )
            for row in select(corpus, 0)
        ]
    seen.clear()
    for line in (corpus / "correction-hypotheses.jsonl").read_text(encoding="utf8").splitlines():
        row = json.loads(line)
        a, b = parse_query(row["query"]), parse_query(row["correction"])
        x, y = a.words.split(), b.words.split()
        differences = [(u, v) for u, v in zip(x, y, strict=False) if u != v]
        if a.house and a.house == b.house and len(x) == len(y) and len(differences) == 1:
            u, v = differences[0]
            if (
                min(len(u), len(v)) >= 4
                and u.isalpha()
                and v.isalpha()
                and DamerauLevenshtein.distance(u, v) == 1
                and row["query"] not in seen
            ):
                if not full:
                    records.append(dict(row, cohort="possible_typo"))
                seen.add(row["query"])
    for line in reviewed.read_text(encoding="utf8").splitlines():
        row = json.loads(line)
        if row["review_status"] not in {"unreviewed", "unresolved_intent"}:
            records.append(dict(row, cohort="reviewed"))
    return records


def reviewed_conflict(case, result, catalog):
    options = [result] if result["status"] in {"found", "no_codes"} else result.get("candidates", [])
    for option in options:
        if option.get("building_id"):
            building = catalog.building(option["building_id"])
            with catalog.connect() as db:
                aliases = [
                    r[0]
                    for r in db.execute(
                        "SELECT street_key FROM aliases WHERE building_id=?",
                        (building["id"],),
                    )
                ]
        else:
            building = address_fields(option["address"])
            aliases = []
        if literal_words(case["expected_street"]) not in [
            literal_words(re.sub(r"\([^()]*\)", "", street)) for street in [building["street"], *aliases]
        ]:
            return True
        if building["house"].casefold().replace("ё", "е") != case["expected_house"]:
            return True
        if case.get("expected_city") and not literal_words(case["expected_city"]) <= literal_words(
            building["city"] + " " + building.get("lineage", "")
        ):
            return True
        if (
            case.get("expected_street_type")
            and address_fields(option["address"])["kind"] != case["expected_street_type"]
        ):
            return True
    return False


def search_variant(catalog, message, variant, search):
    body = search_body(message) if variant["mode"] == "hybrid" else native_body(message, variant)
    if variant["mode"] in {"structured", "aliases"}:
        exact_variant = dict(variant, fuzziness="0", minimum="100%")
        exact_body = native_body(message, exact_variant)
        coded_body = copy.deepcopy(exact_body)
        coded_body["query"]["bool"]["filter"].append({"term": {"coded_house": parse_query(message).house or ""}})
        hits = search(coded_body) or search(exact_body) or search(body)
    else:
        hits = search(body)
    if variant["mode"] == "hybrid":
        return catalog.resolve(
            message, catalog.load_candidates([hit["_id"] for hit in hits], parse_query(message).house)
        )
    return native_result(catalog, message, hits, variant["ratio"])


def evaluation_ast(source):
    functions = {"mapping", "configurations", "native_body", "native_result", "sample", "reviewed_conflict"}
    nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in functions]
    if len(nodes) != len(functions):
        raise ValueError("Missing frozen experiment functions")
    return ast.dump(ast.Module(body=nodes, type_ignores=[]), include_attributes=False)


def resume_rows(path, cases, variants, fingerprint, sources, catalog_revision, index, index_mapping):
    previous = json.loads((path / "frozen-input.json").read_text(encoding="utf8"))
    if previous["sha256"] != fingerprint or previous["variants"] != variants:
        raise ValueError("Cannot resume with different inputs or configurations")
    if previous["catalog_sha256"] != catalog_revision:
        raise ValueError("Cannot resume with a different catalog")
    for name, digest in sources.items():
        if name != "tools/benchmark_native_search.py" and previous["sources"].get(name) != digest:
            raise ValueError("Cannot resume after changing a search dependency")
    snapshot = (path / "runner_snapshot.py").read_bytes()
    if hashlib.sha256(snapshot).hexdigest() != previous["sources"]["tools/benchmark_native_search.py"]:
        raise ValueError("Previous runner snapshot does not match its source fingerprint")
    if evaluation_ast(snapshot) != evaluation_ast(Path(__file__).read_bytes()):
        raise ValueError("Cannot reuse results after changing search or scoring functions")
    previous_helpers = {n.name: n for n in ast.parse(snapshot).body if isinstance(n, ast.FunctionDef)}
    if "search_variant" in previous_helpers:
        current_helpers = {
            n.name: n for n in ast.parse(Path(__file__).read_bytes()).body if isinstance(n, ast.FunctionDef)
        }
        if ast.dump(previous_helpers["search_variant"]) != ast.dump(current_helpers["search_variant"]):
            raise ValueError("Cannot reuse results after changing the query execution strategy")
    index_snapshot = json.loads((path / "index-definition-snapshot.json").read_text(encoding="utf8"))
    if index_snapshot["index"] != index or index_snapshot["_mapping"] != index_mapping:
        raise ValueError("Cannot resume against a different index definition")
    rows = [json.loads(line) for line in (path / "results.jsonl").read_text(encoding="utf8").splitlines()]
    if len(rows) % len(variants):
        raise ValueError("Incomplete profile group in previous results")
    names = {v["name"] for v in variants}
    for start in range(0, len(rows), len(variants)):
        case_id = start // len(variants)
        batch = rows[start : start + len(variants)]
        if {r["variant"] for r in batch} != names or any(
            r["case_id"] != case_id or r["query"] != cases[case_id]["query"] for r in batch
        ):
            raise ValueError("Previous results are not a complete ordered prefix")
    return rows


def run(args):
    if args.output.exists():
        raise FileExistsError("Use a fresh experiment directory")
    args.output.mkdir(parents=True)
    sources = replay.source_fingerprints()
    sources["tools/benchmark_native_search.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with args.catalog.open("rb") as stream:
        catalog_revision = hashlib.file_digest(stream, "sha256").hexdigest()
    with httpx.Client(base_url=args.url, trust_env=False, timeout=15) as client:
        index_info = client.get("/" + args.index + "/_mapping")
        index_info.raise_for_status()
        metadata = index_info.json()[args.index]["mappings"].get("_meta", {})
        if args.stage == "aliases" and metadata != {"catalog_sha256": catalog_revision, "aliases": True}:
            raise ValueError("Experiment index does not match the frozen catalog / alias schema")
        probes = analysis_probes(client, args.index) if args.stage == "aliases" else []
    cases = sample(args.corpus, args.reviewed, args.limit, args.full_corpus)
    variants = configurations(args.stage)
    if args.variants:
        chosen = set(args.variants.split(","))
        variants = [variant for variant in variants if variant["name"] in chosen]
        if chosen != {variant["name"] for variant in variants}:
            raise ValueError("Unknown experiment variant")
    replay.initialize(str(args.legacy), str(args.catalog))
    catalog = Catalog(str(args.catalog))
    local = threading.local()
    counters = collections.defaultdict(collections.Counter)
    times = collections.defaultdict(list)
    fingerprint = hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    prefix = (
        resume_rows(
            args.resume_from,
            cases,
            variants,
            fingerprint,
            sources,
            catalog_revision,
            args.index,
            index_info.json()[args.index],
        )
        if args.resume_from
        else []
    )
    completed = len(prefix) // len(variants)
    (args.output / "runner_snapshot.py").write_bytes(Path(__file__).read_bytes())
    (args.output / "frozen-input.json").write_text(
        json.dumps(
            {
                "cases": cases,
                "variants": variants,
                "sha256": fingerprint,
                "sources": sources,
                "catalog_sha256": catalog_revision,
                "index_metadata": metadata,
                "index": args.index,
                "index_mapping": index_info.json()[args.index],
                "analyzer_probes": probes,
                "resumed_cases": completed,
                "resumed_prefix_sha256": (
                    hashlib.sha256((args.resume_from / "results.jsonl").read_bytes()).hexdigest()
                    if args.resume_from
                    else None
                ),
            },
            ensure_ascii=False,
        ),
        encoding="utf8",
    )

    def evaluate(index_case):
        index, case = index_case
        if not hasattr(local, "client"):
            local.client = httpx.Client(base_url=args.url, trust_env=False, timeout=15)
        old = replay.legacy_lookup(case["query"])
        reference = (
            old
            or (replay.legacy_lookup(case["correction"]) if case.get("correction") else None)
            or case.get("expected_address")
            or case.get("historical_suggestion_not_truth")
        )
        expected = parse_query(reference)
        expected_ids = replay.expected_ids.get((expected.words, expected.house), set())
        outcomes = []
        rotated = variants[index % len(variants) :] + variants[: index % len(variants)]
        for variant in rotated:
            started = time.perf_counter()
            backend_error = None
            if variant["mode"] == "catalog":
                result = catalog.resolve(case["query"])
            else:

                def search(body):
                    response = local.client.post("/" + args.index + "/_search", json=body)
                    response.raise_for_status()
                    return response.json()["hits"]["hits"]

                try:
                    result = search_variant(catalog, case["query"], variant, search)
                except httpx.HTTPError as exc:
                    backend_error = {"type": type(exc).__name__}
                    if isinstance(exc, httpx.HTTPStatusError):
                        backend_error["http_status"] = exc.response.status_code
                        try:
                            backend_error["response"] = exc.response.json()
                        except ValueError:
                            backend_error["response"] = exc.response.text[:2000]
                    result = {"status": "backend_error", "data": {}}
            if result["status"] == "not_found" and old and legacy_house_matches(case["query"], old):
                result = {"status": "found", "engine": "v1", "address": old, "data": {}}
            elapsed = (time.perf_counter() - started) * 1000
            options = [result] if result["status"] == "found" else result.get("candidates", [])
            retained = any(
                option.get("building_id") in expected_ids
                or (
                    option.get("address")
                    and (
                        parse_query(option["address"]).words,
                        parse_query(option["address"]).house,
                    )
                    == (expected.words, expected.house)
                )
                for option in options
            )
            conflict = reviewed_conflict(case, result, catalog) if case["cohort"] == "reviewed" else None
            outcomes.append(
                {
                    "variant": variant["name"],
                    "case_id": index,
                    "cohort": case["cohort"],
                    "query": case["query"],
                    "status": result["status"],
                    "engine": result.get("engine", "v2"),
                    "address": result.get("address"),
                    "candidate_count": len(result.get("candidates", [])),
                    "reference_retained": retained,
                    "literal_conflict": conflict,
                    "milliseconds": round(elapsed, 3),
                    "backend_error": backend_error,
                }
            )
        return outcomes

    def record(stream, rows):
        for row in rows:
            name, cohort = row["variant"], row["cohort"]
            counts = counters[name]
            counts[cohort + ":total"] += 1
            counts[cohort + ":" + row["status"]] += 1
            counts[cohort + ":reference_retained"] += row["reference_retained"]
            counts[cohort + ":automatic_literal_conflict"] += bool(row["literal_conflict"] and row["status"] == "found")
            counts[cohort + ":choice_literal_conflict"] += bool(
                row["literal_conflict"] and row["status"] == "ambiguous"
            )
            times[name].append(row["milliseconds"])
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool,
        (args.output / "results.jsonl").open("w", encoding="utf8") as stream,
    ):
        record(stream, prefix)
        for i, outcomes in enumerate(
            pool.map(evaluate, itertools.islice(enumerate(cases), completed, None)), completed
        ):
            record(stream, outcomes)
            if i % 50 == 0:
                print(
                    json.dumps({"cases": i + 1, "total": len(cases), "variants": len(variants)}),
                    flush=True,
                )
    summary = {
        "stage": args.stage,
        "catalog_reader": "SQLite build artifact; PostgreSQL parity is a separate check",
        "cases": len(cases),
        "variants": len(variants),
        "input_sha256": fingerprint,
        "resumed_cases": completed,
        "analyzer_probes": probes,
        "backend_error_policy": "Count separately; do not silently call another ranker or treat failure as not_found",
        "results": [
            {
                "variant": name,
                "counts": dict(counts),
                "p50_ms": sorted(times[name])[len(times[name]) // 2],
                "p95_ms": sorted(times[name])[int(len(times[name]) * 0.95)],
            }
            for name, counts in counters.items()
        ],
        "limitations": [
            "Исторические ответы и исправления — предварительная разметка.",
            "Ручная разметка проверяет буквальные ограничения, а не неуказанный город.",
            "Разбор ролей номера дома и улицы общий; fuzzy-ранжирование native не использует RapidFuzz.",
            "Задержки включают локальный HTTP и чтение каталога; не являются задержками VDS или VK.",
        ],
    }
    current_sources = replay.source_fingerprints()
    current_sources["tools/benchmark_native_search.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if current_sources != sources:
        raise RuntimeError("Source changed during experiment; discard results and rerun on frozen source")
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    print(
        json.dumps({"complete": True, "cases": len(cases), "variants": len(variants)}),
        flush=True,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["index", "run"])
    p.add_argument(
        "--catalog",
        type=Path,
        default=Path("artifacts/search/serving.house-identity.db"),
    )
    p.add_argument("--legacy", type=Path, default=Path("codes.db"))
    p.add_argument("--corpus", type=Path, default=Path("artifacts/private/audit"))
    p.add_argument(
        "--reviewed",
        type=Path,
        default=Path("artifacts/private/quality/history-review.jsonl"),
    )
    p.add_argument("--output", type=Path, default=Path("artifacts/private/native-search-20261106"))
    p.add_argument("--url", default="http://127.0.0.1:19200")
    p.add_argument("--index", default="domofomka-native-lab-20261106-house-v2")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--stage", choices=["initial", "structured", "aliases"], default="initial")
    p.add_argument("--full-corpus", action="store_true")
    p.add_argument("--variants")
    p.add_argument("--resume-from", type=Path)
    args = p.parse_args()
    if args.command == "index":
        print(json.dumps(build_index(args.catalog, args.url, args.index, aliases=args.stage == "aliases")), flush=True)
    else:
        run(args)
