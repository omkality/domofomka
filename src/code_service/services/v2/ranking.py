"""Общие правила ранжирования и формирования ответа без обращений к БД."""

import base64
import hashlib
import json
import re
import zlib
from dataclasses import replace

from rapidfuzz.distance import DamerauLevenshtein
from rapidfuzz.fuzz import ratio
from rapidfuzz.process import extractOne

from src.code_service.services.v2.constraints import (
    complete_name_match,
    full_name_match,
    has_misaligned_source_fields,
    literal_name_tokens,
    name_constraints_match,
    street_name_keys,
    whole_location_match,
)
from src.code_service.services.v2.normalize import (
    Query,
    address_words,
    building_kind_matches,
    parse_query,
    query_name_words,
)
from src.infrastructure.telemetry import note


class Ranker:
    """Одинаковые ограничения адреса для файлового и PostgreSQL-читателя."""

    minimum_similarity: float
    ambiguity_margin: float

    @staticmethod
    def prepare_query(message: str) -> tuple[Query, list[Query], dict | None]:
        """Разобрать запрос и вернуть ранний ответ для неполного адреса."""
        query = parse_query(message)
        if not query.words and query.name_words:
            query = replace(query, words=query.name_words)
        note(
            normalized_query={
                "words": query.words,
                "house": query.house,
                "entrance": query.entrance,
                "kind": query.kind,
            }
        )
        if not message.strip():
            return query, [], {"status": "not_found", "data": {}, "engine": "v2"}
        if not query.house or not query.words:
            # Unsupported house formats and streets named like a type (e.g.
            # "Набережная") still get the unchanged v1 compatibility path.
            numeric_street = any(any(char.isdigit() for char in word) for word in query.words.split())
            if (query.house and any(c.isalpha() for c in message)) or (
                not query.house and any(c.isdigit() for c in message) and not numeric_street
            ):
                return query, [], {"status": "not_found", "data": {}, "engine": "v2"}
            return query, [], {"status": "incomplete_query", "data": {}, "engine": "v2"}
        variants = [query]
        if query.name_words and query.name_words != query.words:
            variants.append(replace(query, words=query.name_words))
        return query, variants, None

    @staticmethod
    def joined_pairs(query: Query) -> list[re.Match[str]]:
        """Найти соседние слова до обращения к словарю каталога."""
        return list(re.finditer(r"(?=(\b[а-яa-z]{2,}\s+[а-яa-z]{2,}\b))", query.search_text))

    def joined_variants(self, query: Query, candidates: list[dict], vocabulary: list[str]) -> list[Query]:
        """Проверить не более четырёх склеек слов по фактическому словарю."""
        pairs = self.joined_pairs(query)
        if not pairs:
            return []
        vocabulary = [
            *vocabulary,
            *(word for row in candidates for word in address_words(row["street"]).split()),
        ]
        variants = []
        for pair in pairs[:4]:
            joined = "".join(pair[1].split())
            if len(joined) < 6 or not extractOne(
                joined, vocabulary, scorer=DamerauLevenshtein.distance, score_cutoff=0 if len(joined) < 8 else 1
            ):
                continue
            alternate = address_words(query.search_text[: pair.start()] + joined + query.search_text[pair.end(1) :])
            # The original parsed identity is authoritative, including area numbers.
            variants.append(replace(query, words=alternate))
        return variants

    @staticmethod
    def similarity(query: str, document: str) -> float:
        """Рассчитать лексическую близость запроса и адресного кандидата."""
        tokens = document.split()
        if any(any(c.isdigit() for c in word) and word not in tokens for word in query.split()):
            return 0
        return min((max((ratio(w, t) / 100 for t in tokens), default=0) for w in query.split()), default=0)

    def score_candidates(self, query: Query, variants: list[Query], candidates: list[dict]) -> list[tuple[float, dict]]:
        """Оценить кандидатов, сохранив точные ограничения дома и названия."""
        scored = []
        for row in candidates:
            if row["house"] != query.house:
                continue
            if not name_constraints_match(query, row):
                continue
            if has_misaligned_source_fields(row):
                # Misaligned source columns are not a verified v2 address. Let
                # compatibility fallback preserve v1 until the row is reviewed.
                continue
            street_words = (address_words(row["street"]) or query_name_words(row["street"])).split()
            # A missing ordinal must not silently select another named street.
            if any(any(c.isdigit() for c in part) and part not in query.words.split() for part in street_words):
                continue
            if not any(
                (word == part if any(c.isdigit() for c in word) else ratio(word, part) / 100 >= self.minimum_similarity)
                for variant in variants
                for word in variant.words.split()
                for part in street_words
            ):
                continue
            if not building_kind_matches(query, row):
                continue
            score = max(
                self.similarity(variant.words, document)
                for variant in variants
                for document in [
                    address_words(row["city"] + " " + row["street"]),
                    query_name_words(row["city"] + " " + row["street"]),
                    *row.get("_aliases", []),
                ]
            )
            if score >= self.minimum_similarity:
                scored.append((score, row))
        scored.sort(key=lambda v: (-v[0], v[1]["id"]))
        return scored

    def shortlist_candidates(
        self, query: Query, scored: list[tuple[float, dict]], exact_location: bool
    ) -> list[dict] | dict:
        """Выбрать близкие адреса до проверки наличия кодов."""
        if not any(score == 1 for score, row in scored) and exact_location:
            return {
                "status": "not_found",
                "data": {},
                "engine": "v2",
                "fallback_rejected": "known_street_without_requested_house",
            }
        if not scored:
            return {"status": "not_found", "data": {}, "engine": "v2"}
        best = scored[0][0]
        # Code availability must not replace an exact address with a fuzzy one.
        cutoff = 1 if best == 1 else best - self.ambiguity_margin
        close = [row for score, row in scored if score >= cutoff]
        locations = [
            row
            for row in close
            if whole_location_match(query, row["city"], row["street"]) and complete_name_match(query, row)
        ]
        if locations:
            # A complete city/street must be respected before code availability.
            # Keep source aliases of that same full name, including administrative
            # locality spellings; a longer or different street cannot supply codes.
            tokens = literal_name_tokens(query.name_words or query.words)
            names = {
                key
                for row in locations
                for key in street_name_keys(row)
                if tokens - (literal_name_tokens(row["city"]) - key) == key
            }
            if names:
                close = [
                    row
                    for row in close
                    if any(
                        bool(street_name_keys(row, name) & names)
                        for name in [row["street"], *row.get("_alias_streets", [])]
                    )
                ]
        return close

    @staticmethod
    def choose_candidates(query: Query, variants: list[Query], close: list[dict], coded: set[str]) -> dict:
        """Сохранить точное название и выбрать дом либо запросить уточнение."""
        if len(close) > 1:
            if coded:
                close = [row for row in close if row["id"] in coded]
            else:
                return {"status": "no_codes_ambiguous", "data": {}, "engine": "v2"}
        exact = (
            [
                row
                for row in close
                if any(
                    full_name_match(variant, row) or complete_name_match(variant, row, row.get("_alias_names", []))
                    for variant in variants
                )
            ]
            if query.name_words == query.words
            else []
        )
        if exact:
            close = exact
        if len(close) > 1:
            return {
                "status": "ambiguous",
                "data": {},
                "engine": "v2",
                "candidates": [
                    {"building_id": r["id"], "address": r["display"], "locality_context": r["lineage"]}
                    for r in close[:100]
                ],
                "candidate_count": len(close),
            }
        return {"building": close[0]}

    @staticmethod
    def format_answer(building: dict, rows: list, entrance: str | None = None) -> dict:
        """Оформить прочитанные коды без дополнительного ввода-вывода."""
        data = {}
        for row in rows:
            data.setdefault(row["entrance"], []).append([row["code"], row["kind"]])
        return {
            "status": "found" if data else "no_codes",
            "address": building["display"],
            "building_id": building["id"],
            "fias_id": building["guid"],
            "data": data,
            "requested_entrance": entrance,
            "engine": "v2",
            "identity": (
                "gar"
                if building["guid"]
                else ("gar_address" if building["id"].startswith("address:") else "legacy_unverified")
            ),
        }

    @staticmethod
    def expand_houses(ids: list[str], house: str, rows: list[dict], packed_streets: dict) -> list[dict]:
        """Распаковать известные дома с прежними идентификаторами и порядком."""
        seen = {(r["display"].rsplit(", дом ", 1)[0], r["region"], r["lineage"]) for r in rows}
        for sid in ids:
            packed = packed_streets.get(sid)
            if not packed:
                continue
            bundles = json.loads(zlib.decompress(packed["data"]))
            for b in bundles:
                if house not in b["houses"]:
                    continue
                identity = (b["prefix"], b["region"], b["lineage"])
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(
                    {
                        "id": f"address:{sid}:"
                        + hashlib.sha256("\0".join(identity).encode()).hexdigest()[:20]
                        + ":"
                        + base64.urlsafe_b64encode(house.encode()).decode().rstrip("="),
                        "guid": None,
                        "display": b["prefix"] + ", дом " + house,
                        "city": b["city"],
                        "street": b["street"],
                        "house": house,
                        "city_key": packed["city_key"],
                        "street_key": packed["street_key"],
                        "region": b["region"],
                        "lineage": b["lineage"],
                    }
                )
        return rows
