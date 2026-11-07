"""Лексические варианты для лаборатории; правила домов и адресные данные не меняются."""

import itertools
import re
import time
from functools import lru_cache

from src.search.engine import Catalog
from src.search.normalize import NAME_LABELS, ORDINALS, STOP, TENS, ordinals


def exact_text_match(tokens, row, aliases=()):
    return tokens in [
        set(row["street_key"].split()),
        set((row["city_key"] + " " + row["street_key"]).split()),
        *(set(alias.split()) for alias in aliases),
    ]


class ExactCatalog(Catalog):
    """Лабораторный контроль: приоритет полного имени, затем прежнее ранжирование."""

    def _resolve_candidates(self, query, variants, candidates):
        exact = [
            row
            for row in candidates
            if any(exact_text_match(set(variant.words.split()), row, row.get("_aliases", [])) for variant in variants)
        ]
        if exact:
            result = super()._resolve_candidates(query, variants, exact)
            if result["status"] != "not_found":
                return result
        return super()._resolve_candidates(query, variants, candidates)


def ordinal_rules() -> list[str]:
    forms = {}
    for root, number in ORDINALS.items():
        if root == "трет":
            values = ["третий", "третья", "третье", "третьего", "третьей", "третью", "третьем", "третьи", "третьих"]
        else:
            ending = "ой" if number in {2, 6, 7, 8, 40} else "ый"
            values = [
                root + suffix for suffix in {ending, "ая", "ое", "ого", "ой", "ую", "ом", "ые", "ых", "ым", "ыми"}
            ]
        for value in values:
            forms[value] = number
        if number < 10:
            for tens, value in TENS.items():
                for form in values:
                    forms[tens + " " + form] = value + number
    return [word + " => " + str(number) for word, number in sorted(forms.items())]


@lru_cache(maxsize=1)
def ordinal_replacement():
    values = dict(rule.split(" => ") for rule in ordinal_rules())
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(word) for word in sorted(values, key=lambda s: (-len(s), s))) + r")\b"
    )
    return pattern, values


def expanded_ordinals(value):
    pattern, values = ordinal_replacement()
    return pattern.sub(lambda match: values[match[0]], value)


def extend_analysis(definition: dict) -> None:
    analysis = definition["settings"]["analysis"]
    analysis["char_filter"]["numbered_suffix"] = {
        "type": "pattern_replace",
        "pattern": r"(?iuU)\b(\d+)[\s-]*(?:ая|я|й|ый|ой)\b",
        "replacement": "$1",
    }
    analysis["char_filter"]["quarter"] = {
        "type": "pattern_replace",
        "pattern": r"(?iuU)\bкв[.-]л\b",
        "replacement": "квартал",
    }
    analysis["filter"]["address_graph"] = {
        "type": "synonym_graph",
        "lenient": False,
        "synonyms": [*analysis["filter"]["city_aliases"]["synonyms"], *ordinal_rules()],
    }
    analysis["filter"]["address_graph_shared"] = {
        "type": "synonym_graph",
        "lenient": False,
        "synonyms": [
            *analysis["filter"]["city_aliases"]["synonyms"],
            *[rule for rule in ordinal_rules() if ordinals(rule.split(" => ")[0]) == rule.split(" => ")[1]],
        ],
    }
    analysis["filter"]["name_noise"] = {"type": "stop", "stopwords": sorted(set(STOP) - NAME_LABELS)}
    for analyzer, filters in {
        "ordinal_graph": ["lowercase", "address_graph", "address_stop"],
        "ordinal_graph_shared": ["lowercase", "address_graph_shared", "address_stop"],
        "ordinal_names": ["lowercase", "address_graph", "name_noise"],
        "ordinal_graph_stem": ["lowercase", "address_graph", "address_stop", "russian_stemmer"],
    }.items():
        analysis["analyzer"][analyzer] = {
            "type": "custom",
            "tokenizer": "standard",
            "char_filter": ["yo", "avenue", "boulevard", "quarter", "numbered_suffix", "separators"],
            "filter": filters,
        }


def profiles() -> list[dict]:
    values = [{"name": "catalog", "mode": "catalog"}, {"name": "hybrid", "mode": "hybrid"}]
    pipelines = [
        ("python", "words", "query_plain", "words.plain"),
        ("parser", "structural", "clean_syn", "words.plain"),
        ("spelling_without_ordinals", "lexical", "clean_syn", "words.plain"),
        ("analyzer", "lexical", "ordinal_graph", "words.plain"),
        ("mixed", "words", "ordinal_graph", "words.plain"),
        ("analyzer_stem", "lexical", "ordinal_graph_stem", "words"),
    ]
    for (name, source, analyzer, field), fuzziness, ratio in itertools.product(pipelines, ["1", "AUTO"], [1.0, 0.8]):
        values.append(
            {
                "name": f"lexical.{name}-{fuzziness}-r{ratio}",
                "mode": "lexical",
                "aliases": True,
                "text_source": source,
                "analyzer": analyzer,
                "field": field,
                "fuzziness": fuzziness,
                "prefix": 0,
                "expansions": 50,
                "minimum": "100%",
                "ratio": ratio,
            }
        )
    values.append({"name": "catalog_exact", "mode": "catalog_exact"})
    for label, source, analyzer in [
        ("python_exact", "words", "query_plain"),
        ("analyzer_exact", "lexical", "ordinal_graph"),
    ]:
        values.append(
            {
                "name": f"lexical.{label}-AUTO-r1.0",
                "mode": "lexical",
                "aliases": True,
                "text_source": source,
                "analyzer": analyzer,
                "field": "words.plain",
                "decision": "exact_tokens",
                "fuzziness": "AUTO",
                "prefix": 0,
                "expansions": 50,
                "minimum": "100%",
                "ratio": 1.0,
            }
        )
    for label, source, analyzer in [
        ("python_unicode", "unicode_words", "query_plain"),
        ("python_expanded", "expanded_words", "query_plain"),
        ("analyzer_shared", "lexical", "ordinal_graph_shared"),
    ]:
        values.append(
            {
                "name": f"lexical.{label}-AUTO-r1.0",
                "mode": "lexical",
                "aliases": True,
                "text_source": source,
                "analyzer": analyzer,
                "field": "words.plain",
                "decision": "exact_tokens",
                "fuzziness": "AUTO",
                "prefix": 0,
                "expansions": 50,
                "minimum": "100%",
                "ratio": 1.0,
            }
        )
    for label, source, analyzer in [
        ("python_guarded", "expanded_words", "query_plain"),
        ("analyzer_guarded", "lexical", "ordinal_graph"),
    ]:
        values.append(
            {
                "name": f"lexical.{label}-AUTO-r1.0",
                "mode": "lexical",
                "aliases": True,
                "text_source": source,
                "analyzer": analyzer,
                "field": "words.plain",
                "decision": "exact_tokens",
                "role_policy": "street",
                "max_terms": 24,
                "clause_budget": 768,
                "fuzziness": "AUTO",
                "prefix": 0,
                "expansions": 50,
                "minimum": "100%",
                "ratio": 1.0,
            }
        )
    guarded = next(value for value in values if value["name"] == "lexical.analyzer_guarded-AUTO-r1.0")
    for known_guard, ratio in itertools.product([True, False], [1.0, 0.9, 0.8]):
        policy = "strict" if known_guard else "roles"
        values.append(
            dict(guarded, name=f"scoring.{policy}-AUTO-r{ratio}", ratio=ratio, known_street_guard=known_guard)
        )
    verified = dict(
        guarded,
        name="verified.names-AUTO-r1.0",
        known_street_guard=False,
        scoped_street_guard=True,
        preserve_names=True,
        catalog_fallback=True,
    )
    values.append(verified)
    values.append(dict(verified, name="verified.coverage-AUTO-r1.0", literal_coverage=True))
    return values


def guard_street_roles(body, tokens):
    """Покрытие всего запроса — фильтр; числовой номер не заменяет слово улицы."""
    alphabetic = [token for token in tokens if not token.isdigit()]
    if not alphabetic:
        return body
    text = " ".join(alphabetic)

    def branch(value, nested=False):
        block = value["bool"]
        full, street = block["must"]
        full_options = next(iter(full["match"].values()))
        if full_options["fuzziness"] == "0":
            full_options["_name"] = "alias_exact" if nested else "exact"
        field, options = next(iter(street["match"].items()))
        options.update(query=text, analyzer="whitespace")
        block["filter"].append(full)
        alternatives = [street]
        if any(token.isdigit() for token in tokens):
            city = "aliases.words.plain" if nested else "city.plain"
            alternatives.append(
                {"match": {city: {"query": text, "operator": "and", "fuzziness": 0, "analyzer": "whitespace"}}}
            )
        block["must"] = [{"bool": {"should": alternatives, "minimum_should_match": 1}}]
        block["should"] = [{"match": {field: dict(options, fuzziness=0, boost=4)}}]

    branches = body["query"]["bool"].pop("should")
    body["query"]["bool"].pop("minimum_should_match", None)
    branch(branches[0])
    branch(branches[1]["nested"]["query"], nested=True)
    body["query"]["bool"]["must"] = [{"dis_max": {"queries": branches, "tie_breaker": 0}}]
    return body


def street_probe_body(body):
    def strip(value):
        if isinstance(value, dict):
            if "filter" in value:
                value["filter"] = [
                    clause
                    for clause in value["filter"]
                    if not {"house", "aliases.house"}.intersection(clause.get("term", {}))
                ]
            for child in value.values():
                strip(child)
        elif isinstance(value, list):
            for child in value:
                strip(child)

    strip(body)
    body["_source"] = ["city", "street"]
    return body


def known_street(hits, tokens):
    for hit in hits:
        source = hit.get("_source", {})
        aliases = [
            value
            for child in hit.get("inner_hits", {}).get("aliases", {}).get("hits", {}).get("hits", [])
            for value in [child["_source"]["street"], child["_source"].get("words", "")]
        ]
        if source and exact_text_match(
            set(tokens), {"city_key": source["city"], "street_key": source["street"]}, aliases
        ):
            return True
    return False


def probes(client, index):
    values = []
    for query, expected in [
        ("мск восьмая парковая", ["москва", "8", "парковая"]),
        ("третья парковая", ["3", "парковая"]),
        ("двадцать третья парковая", ["23", "парковая"]),
        ("спб,ул.8-я Парковая", ["санкт", "петербург", "8", "парковая"]),
        ("восьмым парковая", ["8", "парковая"]),
    ]:
        response = client.post("/" + index + "/_analyze", json={"analyzer": "ordinal_graph", "text": query})
        response.raise_for_status()
        actual = [token["token"] for token in response.json()["tokens"]]
        values.append({"text": query, "expected": expected, "actual": actual, "passed": expected == actual})
    return values


def clone_documents(client, source: str, target: str, definition: dict, expected_meta: dict) -> int:
    if not source.startswith("domofomka-native-lab-") or not target.startswith("domofomka-native-lab-"):
        raise ValueError("Only isolated lab indexes may be cloned")
    response = client.get("/" + source + "/_mapping")
    response.raise_for_status()
    previous = response.json()[source]["mappings"]
    if previous.get("_meta") != expected_meta or previous["properties"] != definition["mappings"]["properties"]:
        raise ValueError("Source index does not have the same catalog and indexed fields")
    response = client.post(
        "/_reindex",
        params={"wait_for_completion": "false"},
        json={"source": {"index": source, "size": 500}, "dest": {"index": target}},
    )
    response.raise_for_status()
    task = response.json()["task"]
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        response = client.get("/_tasks/" + task)
        response.raise_for_status()
        result = response.json()
        if result["completed"]:
            if result.get("error") or result["response"].get("failures"):
                raise RuntimeError("Lab clone failed; preserve index and task for inspection")
            client.post("/" + target + "/_refresh").raise_for_status()
            count = client.get("/" + target + "/_count").json()["count"]
            original_count = client.get("/" + source + "/_count").json()["count"]
            if count != original_count or count != result["response"]["created"]:
                raise ValueError("Cloned document count differs")
            return count
        time.sleep(1)
    raise TimeoutError("Lab clone task did not finish; original index is unchanged")
