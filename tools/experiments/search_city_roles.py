"""Лаборатория OpenSearch: отдельное поле города, его опечатки и подтверждение выбора."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Sequence

import httpx

from src.code_service.services.v2.constraints import literal_name_tokens
from src.code_service.services.v2.localities import URBAN
from src.code_service.services.v2.normalize import address_words, parse_query, text
from tools.catalog.sqlite_catalog import SqliteCatalog
from tools.experiments.search_locality_profiles import LOCALITY_PREFIX, locality_matches

FIELD_TYPE = re.compile(
    r"\b(?:улица|ул|проспект|просп|пр-кт|проезд|пр-д|переулок|пер|шоссе|ш|"
    r"бульвар|б-р|набережная|наб|аллея|ал|площадь|пл|тупик|туп|микрорайон|мкрн?|квартал|кв-л)\b",
    re.I,
)


def extend_mapping(definition: dict) -> None:
    """Добавить поля, используемые только выбранным экспериментальным профилем."""
    props = definition["mappings"]["properties"]
    props["city"]["fields"]["key"] = {"type": "keyword"}
    props["aliases"]["properties"]["city"] = copy.deepcopy(props["city"])


def indexed_document(original: dict) -> dict:
    """Подготовить один документ с отдельными адресными полями."""
    document = copy.deepcopy(original)
    for alias in document.get("aliases", []):
        words, street = alias["words"], alias["street"]
        if street and not words.endswith(" " + street):
            raise ValueError("Source city/street boundary is inconsistent")
        alias["city"] = words[: -len(street)].strip() if street else words.strip()
    return document


def clone_documents(client: httpx.Client, source: str, target: str, definition: dict, expected_meta: dict) -> dict:
    """Перенести одинаковые документы в новый индекс сравнения."""
    if not source.startswith("domofomka-native-lab-") or not target.startswith("domofomka-native-lab-"):
        raise ValueError("Only isolated lab indexes may be cloned")
    response = client.get("/" + source + "/_mapping")
    response.raise_for_status()
    previous = response.json()[source]["mappings"]
    base = copy.deepcopy(definition["mappings"]["properties"])
    del base["city"]["fields"]["key"]
    del base["aliases"]["properties"]["city"]
    expected = {k: v for k, v in expected_meta.items() if k != "city_roles"}
    if previous.get("_meta") != expected or previous["properties"] != base:
        raise ValueError("City-role clone requires the same catalog and original fields")
    count, fingerprints, scroll = 0, [], None
    try:
        response = client.post("/" + source + "/_search", params={"scroll": "2m"}, json={"size": 500, "sort": ["_doc"]})
        while True:
            response.raise_for_status()
            page = response.json()
            if page.get("timed_out") or page.get("_shards", {}).get("failed", 0):
                raise RuntimeError("Incomplete city-role clone source")
            scroll = page.get("_scroll_id", scroll)
            hits = page["hits"]["hits"]
            if not hits:
                break
            payload = []
            for hit in hits:
                assert hit["_id"] == hit["_source"]["id"]
                original = hit["_source"]
                fingerprints.append(
                    (
                        hit["_id"],
                        hashlib.sha256(json.dumps(original, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                    )
                )
                payload.extend(
                    [
                        json.dumps({"index": {"_index": target, "_id": hit["_id"]}}),
                        json.dumps(indexed_document(original), ensure_ascii=False),
                    ]
                )
            result = client.post(
                "/_bulk", content=("\n".join(payload) + "\n").encode(), headers={"Content-Type": "application/x-ndjson"}
            )
            result.raise_for_status()
            if result.json().get("errors"):
                raise RuntimeError("City-role clone bulk failed")
            count += len(hits)
            response = client.post("/_search/scroll", json={"scroll": "2m", "scroll_id": scroll})
    finally:
        if scroll:
            client.request("DELETE", "/_search/scroll", json={"scroll_id": [scroll]}).raise_for_status()
    client.post("/" + target + "/_refresh").raise_for_status()
    for index in (source, target):
        response = client.get("/" + index + "/_count")
        response.raise_for_status()
        if response.json()["count"] != count:
            raise ValueError("City-role document count differs")
    return {
        "documents": count,
        "source_documents_sha256": hashlib.sha256(json.dumps(sorted(fingerprints)).encode()).hexdigest(),
    }


def city_field(message: str, known: Callable[[str], bool]) -> str | None:
    """Получить поле местности для выбранного экспериментального документа."""
    first, separator, rest = message.strip().partition(",")
    marker = LOCALITY_PREFIX.match(first)
    urban = URBAN.fullmatch(first.strip())
    if marker or urban:
        field = urban[1] if urban else first[marker.end() :]
        street = FIELD_TYPE.search(field)
        value = address_words(
            field[: street.start()] if street else parse_query(field).search_text, preserve_unicode_letters=True
        )
        tokens = value.split()
        if tokens and tokens[-1] in {"им", "имени"} and not known(value):
            return None
        # Если тип улицы стоит после имени, известное имя города отделяет его.
        return next(
            (" ".join(tokens[:n]) for n in range(len(tokens), 0, -1) if known(" ".join(tokens[:n]))), value or None
        )
    if separator and not FIELD_TYPE.search(first) and FIELD_TYPE.search(rest):
        segment = parse_query(rest).search_text.split(",", 1)[0]
        name = FIELD_TYPE.sub(" ", segment)
        name = re.sub(r"\b(?:дом|корпус|корп|строение|стр|подъезд)\b|(?<!\w)д\.(?=\s|$)", " ", name)
        tokens = text(name, preserve_unicode_letters=True).split()
        if not tokens or (tokens == ["д"] and not re.search(r"\b(?:микрорайон|мкрн?|квартал)\b", segment)):
            return None
        value = address_words(first, preserve_unicode_letters=True)
        if any(char.isalpha() for char in value):
            return value
    return None


def role_locality_matches(hint: str | None, row: dict, aliases: Sequence[str] = ()) -> bool:
    """Проверить совпадение явно выделенной роли населённого пункта."""
    if locality_matches(hint, row, aliases):
        return True
    required = literal_name_tokens(hint)
    fields = [row["city"] + " " + row.get("lineage", "")]
    own_field = row.get("display", "").removeprefix(row["city"] + ", ").rsplit(", дом ", 1)[0]
    if LOCALITY_PREFIX.match(own_field) or URBAN.fullmatch(own_field):
        fields.append(own_field)
    for alias in aliases:
        words, street = alias.get("words", ""), alias["street"]
        if street and words.endswith(street):
            fields.append(words[: -len(street)].strip())
    return bool(required) and any(required <= literal_name_tokens(field) for field in fields)


def street_terms(tokens: list[str], city_tokens: list[str]) -> list[str]:
    """Получить значимые слова поля улицы для запроса сравнения."""
    remaining, result = Counter(city_tokens), []
    for token in tokens:
        if remaining[token]:
            remaining[token] -= 1
        else:
            result.append(token)
    return result


def city_probe(value: str, *, fuzziness: str | int = "AUTO", limit: int = 64) -> dict:
    """Найти допустимые варианты местности в экспериментальном индексе."""
    options = {
        "query": value,
        "analyzer": "ordinal_graph",
        "operator": "and",
        "fuzziness": fuzziness,
        "prefix_length": 0,
        "max_expansions": 50,
    }
    canonical = {"match": {"city.plain": options}}
    source = {"match": {"aliases.city.plain": options}}
    return {
        "size": 0,
        "query": {
            "bool": {"should": [canonical, {"nested": {"path": "aliases", "query": source}}], "minimum_should_match": 1}
        },
        "aggs": {
            "canonical": {"filter": canonical, "aggs": {"places": {"terms": {"field": "city.key", "size": limit}}}},
            "source": {
                "nested": {"path": "aliases"},
                "aggs": {
                    "matched": {
                        "filter": source,
                        "aggs": {"places": {"terms": {"field": "aliases.city.key", "size": limit}}},
                    }
                },
            },
        },
    }


def probe_places(value: dict) -> tuple[set[str], bool]:
    """Получить кандидатов местности с фиксированными ограничениями."""
    if value.get("timed_out") or value.get("terminated_early") or value.get("_shards", {}).get("failed", 0):
        raise RuntimeError("City aggregation is incomplete")
    aggs = value["aggregations"]
    groups = [aggs["canonical"]["places"], aggs["source"]["matched"]["places"]]
    return {r["key"] for group in groups for r in group["buckets"]}, any(
        group.get("sum_other_doc_count", 0) for group in groups
    )


def city_filter(places: set[str]) -> dict:
    """Построить точный фильтр подтверждённой местности."""
    if not places:
        return {"match_none": {}}
    return {
        "bool": {
            "should": [
                {"terms": {"city.key": sorted(places)}},
                {"nested": {"path": "aliases", "query": {"terms": {"aliases.city.key": sorted(places)}}}},
            ],
            "minimum_should_match": 1,
        }
    }


def confirmation(result: dict, reason: str = "locality_spelling") -> dict:
    """Отметить необходимость подтверждения исправленной местности пользователем."""
    flag = {"locality_confirmation_required": True} if reason == "locality_spelling" else {}
    answer = dict(result, **flag)
    if answer["status"] == "found":
        if not answer.get("building_id"):
            raise ValueError("Locality confirmation needs a building identity")
        return {
            "status": "ambiguous",
            "engine": "v2",
            "data": {},
            "candidates": [{"building_id": answer["building_id"], "address": answer["address"]}],
            "clarification_reason": reason,
            **flag,
        }
    if answer["status"] == "no_codes":
        return {"status": "no_codes_ambiguous", "engine": "v2", "data": {}, "locality_confirmation_required": True}
    return answer


def constrain_result(result: dict, catalog: SqliteCatalog, places: set[str]) -> dict:
    """Удалить результаты, противоречащие ограничениям исходного запроса."""
    if result["status"] not in {"found", "no_codes", "ambiguous"}:
        return result
    options = [result] if result["status"] in {"found", "no_codes"} else result["candidates"]
    safe = []
    for option in options:
        row = catalog.building(option["building_id"])
        if row is None:
            raise ValueError("City confirmation candidate is missing")
        with catalog.connect() as db:
            aliases = [
                {"street": r[1], "words": r[0] + " " + r[1]}
                for r in db.execute(
                    r"""
                SELECT
                  city_key,
                  street_key
                FROM
                  aliases
                WHERE
                  building_id = ?
                """,
                    (row["id"],),
                )
            ]
        if any(role_locality_matches(place, row, aliases) for place in places):
            safe.append(option)
    if not safe:
        return {"status": "not_found", "data": {}}
    return dict(result, candidates=safe) if result["status"] == "ambiguous" else result


def source_choices(result: dict, baseline: dict, limit: int = 100) -> dict:
    """Собрать варианты по подтверждённым исходным адресам."""
    if result["status"] == "found" and baseline["status"] in {"no_codes", "no_codes_ambiguous"}:
        return confirmation(result, reason="street_spelling")
    if result["status"] not in {"found", "ambiguous"} or baseline["status"] != "ambiguous":
        return result
    native = [result] if result["status"] == "found" else result["candidates"]
    ids = {row["building_id"] for row in native}
    if not ids.intersection(row["building_id"] for row in baseline["candidates"]):
        return result
    extra = [row for row in baseline["candidates"] if row["building_id"] not in ids]
    if not extra:
        return result
    candidates = [{"building_id": row["building_id"], "address": row["address"]} for row in native] + extra
    return {
        "status": "ambiguous",
        "engine": "v2",
        "data": {},
        "candidates": candidates[:limit],
        "candidate_count": len(candidates),
        "candidate_truncated": len(candidates) > limit,
        "clarification_reason": "source_variants",
    }
