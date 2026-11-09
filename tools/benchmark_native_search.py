"""Локальная матрица OpenSearch: независимое ранжирование, общий разбор ролей и точный дом.

Переписки и построчные результаты остаются в artifacts/private. Рабочий API не переключается.
"""

import argparse
import collections
import concurrent.futures
import copy
import hashlib
import itertools
import json
import re
import threading
import time
from dataclasses import replace
from functools import partial
from pathlib import Path

import httpx
from rapidfuzz.distance import DamerauLevenshtein

from src.search.constraints import (
    complete_name_match,
    has_misaligned_source_fields,
    legacy_result_matches,
    legacy_source_matches,
    name_constraints_match,
    whole_location_match,
)
from src.search.engine import MAPPING, Catalog, documents, search_body
from src.search.normalize import STOP, address_words, building_kind_matches, parse_query
from tools import (
    evaluate_search as replay,
    search_city_roles,
    search_lexical_profiles,
    search_locality_profiles,
)
from tools.compare_postgres_catalog import select
from tools.quality_suite import address_fields, literal_words


def mapping(*, aliases=False, lexical=False, similarity=None, bm25_b=0.75, city_roles=False):
    value = copy.deepcopy(MAPPING)
    if similarity not in {None, "BM25", "boolean"}:
        raise ValueError("Unknown lab similarity")
    if not 0 <= bm25_b <= 1:
        raise ValueError("BM25 b must be between zero and one")
    if similarity:
        value["settings"]["similarity"] = {
            "default": {"type": similarity, **({"b": bm25_b, "k1": 1.2} if similarity == "BM25" else {})}
        }
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
        query_plain={"type": "custom", "tokenizer": "standard", "filter": ["lowercase", "address_stop"]},
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
            "plain": {"type": "text", "analyzer": "plain", "search_analyzer": "query_plain"},
            "synonyms": {"type": "text", "analyzer": "address", "search_analyzer": "query_synonyms"},
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
    if lexical:
        search_lexical_profiles.extend_analysis(value)
    if city_roles:
        if not aliases or not lexical:
            raise ValueError("City roles require the lexical alias schema")
        search_city_roles.extend_mapping(value)
    return value


def configurations(stage="initial"):
    if stage == "lexical":
        return search_lexical_profiles.profiles()
    values = [{"name": "catalog", "mode": "catalog"}, {"name": "hybrid", "mode": "hybrid"}]
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


def query_text(message, variant):
    source = variant.get("text_source")
    query = parse_query(message, preserve_lexical_ordinals=source in {"lexical", "expanded_words"})
    lexical_text = query.words if source == "words" else query.search_text
    if source == "names":
        lexical_text = query.name_words
    if source in {"unicode_words", "expanded_words"}:
        if source == "expanded_words":
            lexical_text = search_lexical_profiles.expanded_ordinals(lexical_text)
        lexical_text = address_words(lexical_text, preserve_unicode_letters=True)
    return query, lexical_text


def native_body(message, variant, *, analyzed_tokens=None, street_tokens=None):
    query, lexical_text = query_text(message, variant)
    match = {
        "query": lexical_text,
        "minimum_should_match": variant["minimum"],
        "fuzziness": variant["fuzziness"],
        "prefix_length": variant["prefix"],
        "max_expansions": variant["expansions"],
        "fuzzy_transpositions": True,
    }
    exact = {"query": lexical_text, "operator": "and", "fuzziness": 0, "boost": 4, "_name": "exact"}
    if variant.get("analyzer"):
        match["analyzer"] = exact["analyzer"] = variant["analyzer"]
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
    if variant["mode"] == "lexical":
        body["sort"] = [{"_score": "desc"}, {"id": "asc"}]
    if variant["mode"] in {"structured", "aliases", "lexical"}:
        street_field = variant["field"].replace("words", "street")
        lexical = (
            lexical_text
            if variant["mode"] in {"aliases", "lexical"}
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
                            "inner_hits": {
                                "size": 100,
                                "_source": ["aliases.building_id", "aliases.street", "aliases.words"],
                            },
                        }
                    },
                ],
            }
        }
    if variant.get("role_policy"):
        if analyzed_tokens is None:
            raise ValueError("Street role policy needs analyzer tokens")
        body = search_lexical_profiles.guard_street_roles(body, analyzed_tokens, street_tokens)
    return body


def build_index(
    catalog,
    url,
    index,
    *,
    aliases=False,
    lexical=False,
    source_index=None,
    similarity=None,
    bm25_b=0.75,
    city_roles=False,
):
    if not index.startswith("domofomka-native-lab-"):
        raise ValueError("Only isolated experiment indexes are allowed")
    with httpx.Client(base_url=url, trust_env=False, timeout=120) as client:
        if client.head("/" + index).status_code != 404:
            raise FileExistsError("Preserve the previous experiment index")
        index_mapping = mapping(
            aliases=aliases, lexical=lexical, similarity=similarity, bm25_b=bm25_b, city_roles=city_roles
        )
        with catalog.open("rb") as stream:
            revision = hashlib.file_digest(stream, "sha256").hexdigest()
        index_mapping["mappings"]["_meta"] = {"catalog_sha256": revision, "aliases": aliases}
        if city_roles:
            index_mapping["mappings"]["_meta"]["city_roles"] = 1
        client.put("/" + index, json=index_mapping).raise_for_status()
        if aliases and not all(probe["passed"] for probe in analysis_probes(client, index)):
            raise ValueError("Analyzer probes failed before bulk indexing")
        if lexical and not all(probe["passed"] for probe in search_lexical_profiles.probes(client, index)):
            raise ValueError("Ordinal graph probes failed before bulk indexing")
        if source_index:
            if city_roles:
                facts = search_city_roles.clone_documents(
                    client, source_index, index, index_mapping, index_mapping["mappings"]["_meta"]
                )
                return dict(facts, index=index, source_index=source_index)
            total = search_lexical_profiles.clone_documents(
                client, source_index, index, index_mapping, index_mapping["mappings"]["_meta"]
            )
            return {"documents": total, "index": index, "source_index": source_index}
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
                    if city_roles:
                        by_street[row["canonical_city"], row["canonical_street"]][-1]["city"] = row["city_key"]

        def flush():
            payload = "".join(
                json.dumps({"index": {"_index": index, "_id": doc["id"]}})
                + "\n"
                + json.dumps(doc, ensure_ascii=False)
                + "\n"
                for doc in batch
            )
            response = client.post("/_bulk", content=payload.encode(), headers={"Content-Type": "application/x-ndjson"})
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


def native_result(
    catalog,
    message,
    hits,
    ratio,
    *,
    query_numbers=None,
    exact_tokens=None,
    require_exact=False,
    literal_coverage=False,
    locality_hint=None,
    locality_places=None,
    city_roles=False,
):
    query = parse_query(message)
    if not query.words and query.name_words:
        query = replace(query, words=query.name_words)
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

    def numeric(word):
        return any(char.isdigit() for char in word)

    query_numbers = {word for word in query.words.split() if numeric(word)} if query_numbers is None else query_numbers
    for row in rows:
        if row["house"] != query.house:
            continue
        sid = hashlib.sha256((row["city_key"] + "\0" + row["street_key"]).encode()).hexdigest()[:32]
        hit = hit_by_id.get(sid, {})
        alias_hits = hit.get("inner_hits", {}).get("aliases", {}).get("hits", {})
        matching = [a["_source"] for a in alias_hits.get("hits", []) if a["_source"]["building_id"] == row["id"]]
        match_locality = (
            search_city_roles.role_locality_matches if city_roles else search_locality_profiles.locality_matches
        )
        if not match_locality(locality_hint, row, matching):
            continue
        if locality_places is not None and not any(match_locality(place, row, matching) for place in locality_places):
            continue
        if not name_constraints_match(query, row, [alias["street"] for alias in matching]):
            continue
        if alias_hits and "canonical" not in hit.get("matched_queries", []):
            if not matching:
                continue
            street = matching[0]["street"]
            document = matching[0].get("words", row["city"] + " " + street)
        else:
            street = row["street"]
            document = row["city"] + " " + street
        street_numbers = {word for word in address_words(street).split() if numeric(word)}
        all_numbers = {word for word in address_words(document).split() if numeric(word)}
        if not street_numbers <= query_numbers or not query_numbers <= all_numbers:
            continue
        if not building_kind_matches(query, row):
            continue
        if has_misaligned_source_fields(row):
            continue
        if sid in scores:
            eligible.append((sid, row))
    if any(sid in exact for sid, _ in eligible):
        eligible = [(sid, row) for sid, row in eligible if sid in exact]
    if not eligible:
        return {"status": "not_found", "data": {}}
    best = max(scores[sid] for sid, _ in eligible)
    precise = []
    coded_ids = None
    if exact_tokens is not None:
        for sid, row in eligible:
            aliases = [
                inner["_source"]
                for inner in hit_by_id[sid].get("inner_hits", {}).get("aliases", {}).get("hits", {}).get("hits", [])
                if inner["_source"]["building_id"] == row["id"]
            ]
            alias_texts = [value for alias in aliases for value in [alias["street"], alias.get("words", "")]]
            complete = (
                complete_name_match(query, row, aliases)
                if literal_coverage
                else search_lexical_profiles.exact_text_match(exact_tokens, row, alias_texts)
            )
            if complete:
                precise.append(row)
    if literal_coverage:
        with catalog.connect() as db:
            coded_ids = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT building_id FROM codes WHERE building_id IN ("
                    + ",".join("?" for _ in eligible)
                    + ")",
                    [row["id"] for _, row in eligible],
                )
            }
        if precise and not any(whole_location_match(query, row["city"], row["street"]) for row in precise):
            # A different city/street decomposition elsewhere in GAR is not
            # evidence that this otherwise complete street query names that city.
            require_exact = False
        # Without a complete location, an empty exact street in another city
        # cannot hide literal coded candidates. A fully scoped name stays protected.
        if not require_exact and not any(row["id"] in coded_ids for row in precise):
            precise = []
    if require_exact and not precise:
        return {"status": "not_found", "data": {}, "fallback_rejected": "known_street_without_requested_house"}
    close = precise or [row for sid, row in eligible if literal_coverage or scores[sid] >= best * ratio]
    if len(close) > 1:
        if coded_ids is None:
            with catalog.connect() as db:
                coded_ids = {
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT building_id FROM codes WHERE building_id IN ("
                        + ",".join("?" for _ in close)
                        + ")",
                        [row["id"] for row in close],
                    )
                }
        coded_ids &= {row["id"] for row in close}
        if coded_ids:
            close = [row for row in close if row["id"] in coded_ids]
        else:
            return {"status": "no_codes_ambiguous", "data": {}}
    if len(close) == 1:
        answer = catalog.answer(close[0], query.entrance)
        if (
            answer["status"] == "no_codes"
            and not precise
            and (literal_coverage or not any(sid in exact for sid, _ in eligible))
        ):
            return {"status": "no_codes_ambiguous", "data": {}}
        return answer
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
            dict(row, cohort=("historical" if row["evidence"] == "bot_address_response" else "correction_hypothesis"))
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
    if case.get("expected_status") and result["status"] != case["expected_status"]:
        return True
    if case.get("allowed_statuses") and result["status"] not in case["allowed_statuses"]:
        return True
    options = [result] if result["status"] in {"found", "no_codes"} else result.get("candidates", [])
    if case.get("expected_building_id") and not any(
        option.get("building_id") == case["expected_building_id"] for option in options
    ):
        return True
    for option in options:
        if option.get("building_id"):
            building = catalog.building(option["building_id"])
            with catalog.connect() as db:
                aliases = [
                    r[0] for r in db.execute("SELECT street_key FROM aliases WHERE building_id=?", (building["id"],))
                ]
        else:
            building = address_fields(option["address"])
            aliases = []
        if case.get("expected_street") and literal_words(case["expected_street"]) not in [
            literal_words(re.sub(r"\([^()]*\)", "", street)) for street in [building["street"], *aliases]
        ]:
            return True
        if case.get("expected_house") and building["house"].casefold().replace("ё", "е") != case["expected_house"]:
            return True
        if case.get("expected_city") and not literal_words(case["expected_city"]) <= literal_words(
            building["city"] + " " + building.get("lineage", "")
        ):
            return True
        if case.get("forbidden_city") and literal_words(case["forbidden_city"]) <= literal_words(
            building["city"] + " " + building.get("lineage", "")
        ):
            return True
        if (
            case.get("expected_street_type")
            and address_fields(option["address"])["kind"] != case["expected_street_type"]
        ):
            return True
    return False


def search_variant(catalog, message, variant, search, analyze=None, city_query=None):
    parsed = parse_query(message)
    locality = (
        search_locality_profiles.locality_hint(
            message,
            partial(search_locality_profiles.known_locality, catalog),
            comma_prefix=variant["locality_policy"] in {"structured", "scoped"},
            require_street_field=variant["locality_policy"] == "scoped",
        )
        if variant.get("locality_policy")
        else None
    )
    if variant.get("preserve_names") and not parsed.words and parsed.name_words:
        variant = dict(variant, text_source="names", analyzer="ordinal_names")
    numbers = None
    tokens = None
    if variant["mode"] == "lexical":
        if analyze is None:
            raise ValueError("Lexical profiles require actual analyzer tokens for number constraints")
        tokens = analyze(query_text(message, variant)[1], variant["analyzer"])
        numbers = {token for token in tokens if any(char.isdigit() for char in token)}
    guarded = bool(variant.get("role_policy"))
    budget = None
    if guarded:
        if not tokens:
            return {"status": "incomplete_query", "data": {}}
        if len(tokens) > variant["max_terms"]:
            return {
                "status": "invalid_query",
                "data": {},
                "query_budget": {"terms": len(tokens), "reason": "too_many_terms"},
            }
        variant = dict(
            variant, expansions=min(variant["expansions"], max(1, variant["clause_budget"] // (4 * len(tokens))))
        )
        budget = {
            "terms": len(tokens),
            "max_expansions": variant["expansions"],
            "clause_budget": variant["clause_budget"],
        }
    places, locality_probe, city_confirmation, street_tokens = None, None, False, None
    if variant.get("city_roles"):
        field = search_city_roles.city_field(message, partial(search_locality_profiles.known_locality, catalog))
        if field:
            street_tokens = search_city_roles.street_terms(tokens, analyze(field, variant["analyzer"]))
            if search_locality_profiles.known_locality(catalog, field):
                locality = field
            else:
                if city_query is None:
                    raise ValueError("City-role profile requires city aggregation")
                places, truncated = search_city_roles.probe_places(city_query(search_city_roles.city_probe(field)))
                city_confirmation = True
                locality_probe = {"places": len(places), "truncated": truncated}
    body = (
        search_body(message)
        if variant["mode"] == "hybrid"
        else native_body(message, variant, analyzed_tokens=tokens, street_tokens=street_tokens)
    )
    if places is not None:
        body["query"]["bool"]["filter"].append(search_city_roles.city_filter(places))
    known = False
    literal_hits = False
    scoped = bool(variant.get("scoped_street_guard") and catalog.has_exact_location(parsed))
    if variant["mode"] in {"structured", "aliases", "lexical"}:
        exact_variant = dict(variant, fuzziness="0", minimum="100%")
        exact_body = native_body(message, exact_variant, analyzed_tokens=tokens, street_tokens=street_tokens)
        if places is not None:
            exact_body["query"]["bool"]["filter"].append(search_city_roles.city_filter(places))
        if guarded and variant.get("known_street_guard", True):
            probe = search_lexical_profiles.street_probe_body(copy.deepcopy(exact_body))
            known = search_lexical_profiles.known_street(search(probe), tokens)
        coded_body = copy.deepcopy(exact_body)
        coded_body["query"]["bool"]["filter"].append({"term": {"coded_house": parse_query(message).house or ""}})
        if variant.get("decision") == "exact_tokens":
            hits = search(exact_body) + search(coded_body)
            literal_hits = bool(hits)
            if not hits and not known:
                coded_fuzzy = copy.deepcopy(body)
                coded_fuzzy["query"]["bool"]["filter"].append(
                    {"term": {"coded_house": parse_query(message).house or ""}}
                )
                hits = (search(coded_fuzzy) or search(body)) if guarded else search(body) + search(coded_fuzzy)
            hits = list({hit["_id"]: hit for hit in hits}.values())
        else:
            hits = search(coded_body) or search(exact_body) or search(body)
    else:
        hits = search(body)
    if variant["mode"] == "hybrid":
        return catalog.resolve(
            message, catalog.load_candidates([hit["_id"] for hit in hits], parse_query(message).house)
        )
    result = native_result(
        catalog,
        message,
        hits,
        variant["ratio"],
        query_numbers=numbers,
        exact_tokens=set(tokens) if variant.get("decision") == "exact_tokens" else None,
        require_exact=known or scoped,
        literal_coverage=bool(variant.get("literal_coverage") and literal_hits),
        locality_hint=locality,
        locality_places=places,
        city_roles=bool(variant.get("city_roles")),
    )
    if guarded:
        result["query_budget"] = budget
        if (known or scoped) and result["status"] == "not_found":
            result["fallback_rejected"] = "known_street_without_requested_house"
    if (
        variant.get("catalog_fallback")
        and result["status"] in {"not_found", "incomplete_query", "no_codes_ambiguous"}
        and parsed.house
    ):
        # Catalog applies the same exact-house, name and scoped-location checks.
        result = catalog.resolve(message)
    if variant.get("source_choices") and result["status"] in {"found", "ambiguous"}:
        result = search_city_roles.source_choices(result, catalog.resolve(message))
    if variant.get("city_roles") and locality:
        result = search_city_roles.constrain_result(result, catalog, {locality})
    if city_confirmation:
        result = search_city_roles.constrain_result(result, catalog, places)
        result = search_city_roles.confirmation(result)
        result["locality_probe"] = locality_probe
    return result


def legacy_exact_match(message, address, variant, analyze):
    fields = address_fields(address)
    row = dict(fields, display=address, guid=None)
    if not building_kind_matches(parse_query(message), row):
        return False
    tokens = set(analyze(query_text(message, variant)[1], variant["analyzer"]))
    parts = [fields["street"], fields["city"] + " " + fields["street"]]
    if variant["text_source"] == "expanded_words":
        parts = [
            address_words(search_lexical_profiles.expanded_ordinals(part.casefold()), preserve_unicode_letters=True)
            for part in parts
        ]
    return tokens in [set(analyze(part, variant["analyzer"])) for part in parts]


def resume_rows(
    path,
    cases,
    variants,
    fingerprint,
    sources,
    catalog_revision,
    index,
    index_mapping,
    *,
    index_analysis=None,
    index_similarity=None,
    engine_version=None,
    max_query_length=None,
):
    previous = json.loads((path / "frozen-input.json").read_text(encoding="utf8"))
    if previous["sha256"] != fingerprint or previous["variants"] != variants:
        raise ValueError("Cannot resume with different inputs or configurations")
    if previous["catalog_sha256"] != catalog_revision:
        raise ValueError("Cannot resume with a different catalog")
    for name, digest in sources.items():
        if previous["sources"].get(name) != digest:
            raise ValueError("Cannot resume after changing a search dependency")
    snapshot = (path / "runner_snapshot.py").read_bytes()
    if hashlib.sha256(snapshot).hexdigest() != previous["sources"]["tools/benchmark_native_search.py"]:
        raise ValueError("Previous runner snapshot does not match its source fingerprint")
    if index_analysis is not None and previous.get("index_analysis") != index_analysis:
        raise ValueError("Cannot resume with different analyzers")
    if index_similarity is not None and previous.get("index_similarity") != index_similarity:
        raise ValueError("Cannot resume with different similarity")
    if engine_version is not None and previous.get("engine_version") != engine_version:
        raise ValueError("Cannot resume with a different search engine version")
    if max_query_length is not None and previous.get("max_query_length") != max_query_length:
        raise ValueError("Cannot resume with different query limits")
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
    sources["tools/search_lexical_profiles.py"] = hashlib.sha256(
        Path(search_lexical_profiles.__file__).read_bytes()
    ).hexdigest()
    with args.catalog.open("rb") as stream:
        catalog_revision = hashlib.file_digest(stream, "sha256").hexdigest()
    with httpx.Client(base_url=args.url, trust_env=False, timeout=15) as client:
        index_info = client.get("/" + args.index + "/_mapping")
        index_info.raise_for_status()
        settings_response = client.get("/" + args.index + "/_settings")
        settings_response.raise_for_status()
        index_settings = settings_response.json()[args.index]["settings"]["index"].get("analysis", {})
        index_similarity = settings_response.json()[args.index]["settings"]["index"].get("similarity", {})
        engine_response = client.get("/")
        engine_response.raise_for_status()
        engine_version = engine_response.json().get("version", {}).get("number")
        metadata = index_info.json()[args.index]["mappings"].get("_meta", {})
        expected_metadata = {"catalog_sha256": catalog_revision, "aliases": True}
        if args.stage in {"aliases", "lexical"} and metadata not in [
            expected_metadata,
            dict(expected_metadata, city_roles=1),
        ]:
            raise ValueError("Experiment index does not match the frozen catalog / alias schema")
        probes = analysis_probes(client, args.index) if args.stage in {"aliases", "lexical"} else []
        if args.stage == "lexical":
            probes += search_lexical_profiles.probes(client, args.index)
    cases = (
        [json.loads(line) for line in args.case_file.read_text(encoding="utf8").splitlines()]
        if getattr(args, "case_file", None)
        else sample(args.corpus, args.reviewed, args.limit, args.full_corpus)
    )
    variants = configurations(args.stage)
    if args.variants:
        chosen = set(args.variants.split(","))
        variants = [variant for variant in variants if variant["name"] in chosen]
        if chosen != {variant["name"] for variant in variants}:
            raise ValueError("Unknown experiment variant")
    elif metadata.get("city_roles") != 1:
        variants = [variant for variant in variants if not variant.get("city_roles")]
    if any(variant.get("city_roles") for variant in variants) and metadata.get("city_roles") != 1:
        raise ValueError("City-role profile requires the city-role index schema")
    replay.initialize(str(args.legacy), str(args.catalog))
    catalog = Catalog(str(args.catalog))
    exact_catalog = search_lexical_profiles.ExactCatalog(str(args.catalog))
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
            index_analysis=index_settings,
            index_similarity=index_similarity,
            engine_version=engine_version,
            max_query_length=getattr(args, "max_query_length", 500),
        )
        if args.resume_from
        else []
    )
    completed = len(prefix) // len(variants)
    (args.output / "runner_snapshot.py").write_bytes(Path(__file__).read_bytes())
    (args.output / "index-definition-snapshot.json").write_text(
        json.dumps(
            {
                "index": args.index,
                "_mapping": index_info.json()[args.index],
                "analysis": index_settings,
                "similarity": index_similarity,
                "engine_version": engine_version,
            },
            ensure_ascii=False,
        ),
        encoding="utf8",
    )
    for filename, digest in sources.items():
        data = Path(filename).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Source changed while freezing experiment inputs")
        destination = args.output / "sources" / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
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
                "index_analysis": index_settings,
                "index_similarity": index_similarity,
                "engine_version": engine_version,
                "analyzer_probes": probes,
                "max_query_length": getattr(args, "max_query_length", 500),
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
        expected = parse_query(reference) if reference else None
        expected_ids = replay.expected_ids.get((expected.words, expected.house), set()) if expected else set()
        outcomes = []
        analysis_cache = {}

        def analyze(text, analyzer):
            key = (text, analyzer)
            if key not in analysis_cache:
                response = local.client.post("/" + args.index + "/_analyze", json={"analyzer": analyzer, "text": text})
                response.raise_for_status()
                analysis_cache[key] = [token["token"] for token in response.json()["tokens"]]
            return analysis_cache[key]

        rotated = variants[index % len(variants) :] + variants[: index % len(variants)]
        for variant in rotated:
            started = time.perf_counter()
            backend_error = None
            if len(case["query"]) > getattr(args, "max_query_length", 500):
                result = {"status": "invalid_query", "data": {}, "engine": "v2"}
            elif variant["mode"] == "catalog":
                result = catalog.resolve(case["query"])
            elif variant["mode"] == "catalog_exact":
                result = exact_catalog.resolve(case["query"])
            else:

                def search(body):
                    response = local.client.post("/" + args.index + "/_search", json=body)
                    response.raise_for_status()
                    return response.json()["hits"]["hits"]

                def city_query(body):
                    response = local.client.post("/" + args.index + "/_search", json=body)
                    response.raise_for_status()
                    return response.json()

                try:
                    result = search_variant(catalog, case["query"], variant, search, analyze, city_query)
                except httpx.HTTPError as exc:
                    backend_error = {"type": type(exc).__name__}
                    if isinstance(exc, httpx.HTTPStatusError):
                        backend_error["http_status"] = exc.response.status_code
                        try:
                            backend_error["response"] = exc.response.json()
                        except ValueError:
                            backend_error["response"] = exc.response.text[:2000]
                    result = {"status": "backend_error", "data": {}}
            recoverable = result["status"] == "incomplete_query" and bool(parse_query(case["query"]).house)
            if (
                (result["status"] == "not_found" or recoverable)
                and not result.get("locality_confirmation_required")
                and old
                and legacy_result_matches(case["query"], old)
            ):
                allowed = True
                whole_location = False
                if result.get("fallback_rejected") == "known_street_without_requested_house":
                    if variant.get("scoped_street_guard") or variant["mode"] == "catalog":
                        whole_location = True
                        allowed = legacy_result_matches(case["query"], old, whole_location=True)
                    else:
                        allowed = legacy_exact_match(case["query"], old, variant, analyze)
                if allowed:
                    validated = replay.legacy_lookup(
                        case["query"],
                        accept_source=partial(legacy_source_matches, case["query"], whole_location=whole_location),
                    )
                    if validated:
                        result = {
                            "status": "found",
                            "engine": "v1",
                            "address": validated,
                            "data": {},
                            "fallback_code_scope": ("whole_location" if whole_location else "source_identity"),
                        }
            elapsed = (time.perf_counter() - started) * 1000
            options = [result] if result["status"] == "found" else result.get("candidates", [])
            retained = (
                any(
                    option.get("building_id") in expected_ids
                    or (
                        option.get("address")
                        and (parse_query(option["address"]).words, parse_query(option["address"]).house)
                        == (expected.words, expected.house)
                    )
                    for option in options
                )
                if expected
                else None
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
                    **(
                        {
                            "result": {
                                key: result[key]
                                for key in (
                                    "status",
                                    "engine",
                                    "building_id",
                                    "address",
                                    "candidates",
                                    "fallback_reason",
                                    "fallback_rejected",
                                    "fallback_code_scope",
                                    "clarification_reason",
                                    "query_budget",
                                    "locality_confirmation_required",
                                    "locality_probe",
                                    "candidate_truncated",
                                )
                                if key in result
                            },
                            "analysis": [
                                {"text": text, "analyzer": analyzer, "tokens": tokens}
                                for (text, analyzer), tokens in analysis_cache.items()
                            ],
                        }
                        if getattr(args, "details", False)
                        else {}
                    ),
                }
            )
        return outcomes

    def record(stream, rows):
        for row in rows:
            name, cohort = row["variant"], row["cohort"]
            counts = counters[name]
            counts[cohort + ":total"] += 1
            counts[cohort + ":" + row["status"]] += 1
            counts[cohort + ":reference_retained"] += bool(row["reference_retained"])
            counts[cohort + ":without_reference"] += row["reference_retained"] is None
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
                print(json.dumps({"cases": i + 1, "total": len(cases), "variants": len(variants)}), flush=True)
    summary = {
        "stage": args.stage,
        "index_similarity": index_similarity,
        "engine_version": engine_version,
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
    current_sources["tools/search_lexical_profiles.py"] = hashlib.sha256(
        Path(search_lexical_profiles.__file__).read_bytes()
    ).hexdigest()
    if current_sources != sources:
        raise RuntimeError("Source changed during experiment; discard results and rerun on frozen source")
    with httpx.Client(base_url=args.url, trust_env=False, timeout=15) as client:
        current_settings = client.get("/" + args.index + "/_settings")
        current_settings.raise_for_status()
        if current_settings.json()[args.index]["settings"]["index"].get("analysis", {}) != index_settings:
            raise RuntimeError("Index analysis changed during experiment")
        if current_settings.json()[args.index]["settings"]["index"].get("similarity", {}) != index_similarity:
            raise RuntimeError("Index similarity changed during experiment")
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({"complete": True, "cases": len(cases), "variants": len(variants)}), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["index", "run"])
    p.add_argument("--catalog", type=Path, default=Path("artifacts/search/serving.house-identity.db"))
    p.add_argument("--legacy", type=Path, default=Path("codes.db"))
    p.add_argument("--corpus", type=Path, default=Path("artifacts/private/audit"))
    p.add_argument("--reviewed", type=Path, default=Path("artifacts/private/quality/history-review.jsonl"))
    p.add_argument("--output", type=Path, default=Path("artifacts/private/native-search-20261106"))
    p.add_argument("--url", default="http://127.0.0.1:19200")
    p.add_argument("--index", default="domofomka-native-lab-20261106-house-v2")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--stage", choices=["initial", "structured", "aliases", "lexical"], default="initial")
    p.add_argument("--source-index")
    p.add_argument("--city-roles", action="store_true")
    p.add_argument("--similarity", choices=["BM25", "boolean"])
    p.add_argument("--bm25-b", type=float, default=0.75)
    p.add_argument("--case-file", type=Path)
    p.add_argument("--details", action="store_true")
    p.add_argument("--max-query-length", type=int, default=500)
    p.add_argument("--full-corpus", action="store_true")
    p.add_argument("--variants")
    p.add_argument("--resume-from", type=Path)
    args = p.parse_args()
    if args.command == "index":
        print(
            json.dumps(
                build_index(
                    args.catalog,
                    args.url,
                    args.index,
                    aliases=args.stage in {"aliases", "lexical"},
                    lexical=args.stage == "lexical",
                    source_index=args.source_index,
                    similarity=args.similarity,
                    bm25_b=args.bm25_b,
                    city_roles=args.city_roles,
                )
            ),
            flush=True,
        )
    else:
        run(args)
