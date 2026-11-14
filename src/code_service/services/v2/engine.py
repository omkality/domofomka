"""Rank candidates, but preserve explicit house and locality constraints."""

import base64
import binascii
import json
import sqlite3
import zlib
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from rapidfuzz.fuzz import ratio
from rapidfuzz.process import extract

from src.code_service.services.v2.constraints import (
    complete_name_match,
    has_misaligned_source_fields,
    locality_prefix_match,
    whole_location_match,
)
from src.code_service.services.v2.normalize import (
    Query,
    building_kind_matches,
    parse_query,
)
from src.code_service.services.v2.ranking import Ranker
from src.infrastructure.telemetry import measured


class Catalog(Ranker):
    """Общий алгоритм кандидатов, точных ограничений и ранжирования адресов."""

    def __init__(
        self, path: str, minimum_similarity: float = 0.86, ambiguity_margin: float = 0.06, *, candidate_limit: int = 100
    ):
        """Задать каталог, пороги ранжирования и предел числа кандидатов."""
        self.path = path
        self.minimum_similarity = minimum_similarity
        self.ambiguity_margin = ambiguity_margin
        self.candidate_limit = candidate_limit

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Открыть файловый каталог SQLite только для чтения."""
        db = sqlite3.connect("file:" + Path(self.path).resolve().as_posix() + "?mode=ro", uri=True, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def _compact(self, db: sqlite3.Connection) -> bool:
        """Проверить компактный формат каталога и его версию."""
        return db.execute(r"""
            SELECT
              value
            FROM
              metadata
            WHERE
              key = 'format'
            """).fetchone() is not None

    def _expand(self, db: sqlite3.Connection, ids: list[str], house: str, include_known: bool = True) -> list[dict]:
        """Распаковать ограниченный набор домов выбранных улиц."""
        if not ids:
            return []
        rows = [
            dict(r)
            for r in db.execute(
                r"""
            SELECT DISTINCT b.*
            FROM streets t
            JOIN buildings b ON b.city_key = t.city_key
            AND b.street_key = t.street_key
            WHERE t.id IN (
            """ + ",".join("?" for _ in ids) + ") AND b.house=?",
                [*ids, house],
            )
        ]
        if not include_known:
            return rows
        packed_streets = {
            r[0]: r
            for r in db.execute(
                r"""
            SELECT t.id,
                   h.data,
                   t.city_key,
                   t.street_key
            FROM streets t
            JOIN street_houses h ON h.id = t.id
            WHERE t.id IN (
            """ + ",".join("?" for _ in ids) + ")",
                ids,
            )
        }
        return self.expand_houses(ids, house, rows, packed_streets)

    @measured("search.v2.coded_candidates")
    def coded_candidates(self, query: Query, limit: int | None = None) -> list[dict]:
        """Получить ограниченный набор кандидатов улиц с кодами."""
        limit = self.candidate_limit if limit is None else limit
        words = query.words.split()
        if not words or not query.house:
            return []
        match = " AND ".join('"' + w + '"*' for w in words)
        clause = "(" + " OR ".join("instr(' '||t.street_key||' ',' '||?||' ')>0" for _ in words) + ")"
        with self.connect() as db:
            if self._compact(db):
                ids = [
                    r[0]
                    for r in db.execute(
                        r"""
                    SELECT s.id
                    FROM SEARCH s
                    JOIN streets t ON t.id = s.id
                    WHERE SEARCH MATCH ?
                        AND t.has_codes = 1
                        AND
                    """ + clause + " ORDER BY t.id LIMIT ?",
                        (match, *words, limit * 10),
                    )
                ]
                return self._expand(db, ids, query.house, include_known=False)
            return [
                dict(r)
                for r in db.execute(
                    r"""
                SELECT DISTINCT b.*
                FROM SEARCH s
                JOIN streets t ON t.id = s.id
                JOIN buildings b ON b.street_key = t.street_key
                AND b.city_key = t.city_key
                WHERE SEARCH MATCH ?
                    AND b.house = ?
                    AND
                """ + clause + " AND EXISTS(SELECT 1 FROM codes c WHERE c.building_id=b.id) ORDER BY b.id LIMIT ?",
                    (match, query.house, *words, limit),
                )
            ]

    @measured("search.v2.candidates")
    def candidates(self, query: Query, limit: int | None = None) -> list[dict]:
        """Получить кандидатов по поисковым словам запроса."""
        limit = self.candidate_limit if limit is None else limit
        words = query.words.split()
        if not words or not query.house:
            return []
        with self.connect() as db:
            compact = self._compact(db)

            def fetch(match: str, terms: list[str]) -> list[dict]:
                """Прочитать ограниченный набор записей текущей поисковой операции."""
                clause = "(" + " OR ".join("instr(' '||t.street_key||' ',' '||?||' ')>0" for _ in terms) + ")"
                if compact:
                    sql = r"""
                        SELECT s.id
                        FROM SEARCH s
                        JOIN streets t ON t.id = s.id
                        WHERE SEARCH MATCH ?
                            AND
                        """ + clause + " ORDER BY t.has_codes DESC,t.id LIMIT ?"
                    ids = [r[0] for r in db.execute(sql, (match, *terms, limit * 10))]
                    return self._expand(db, ids, query.house)
                sql = r"""
                    SELECT DISTINCT b.*
                    FROM SEARCH s
                    JOIN streets t ON t.id = s.id
                    JOIN buildings b ON b.street_key = t.street_key
                    AND b.city_key = t.city_key
                    WHERE SEARCH MATCH ?
                        AND b.house = ?
                        AND
                    """ + clause + " ORDER BY EXISTS(SELECT 1 FROM codes c WHERE c.building_id=b.id) DESC,b.id LIMIT ?"
                return [dict(r) for r in db.execute(sql, (match, query.house, *terms, limit))]

            match = " AND ".join('"' + w + '"*' for w in words)
            rows = fetch(match, words)
            if rows:
                return rows
            vocabulary = street_vocabulary(self.path, Path(self.path).stat().st_mtime_ns)
            groups = []
            terms = []
            for word in words:
                alternatives = (
                    [word]
                    if any(c.isdigit() for c in word)
                    else [r[0] for r in extract(word, vocabulary, scorer=ratio, limit=3, score_cutoff=80)]
                )
                if not alternatives:
                    return []
                terms.extend(alternatives)
                groups.append("(" + " OR ".join('"' + w + '"' for w in alternatives) + ")")
            return fetch(" AND ".join(groups), list(dict.fromkeys(terms)))

    def load_candidates(self, ids: list[str], house: str) -> list[dict]:
        """Прочитать данные отобранных кандидатов без полного перебора каталога."""
        if not ids:
            return []
        with self.connect() as db:
            if self._compact(db):
                return self._expand(db, ids, house)
            return [
                dict(r)
                for r in db.execute(
                    r"""
                SELECT DISTINCT b.*
                FROM streets t
                JOIN buildings b ON b.city_key = t.city_key
                AND b.street_key = t.street_key
                WHERE t.id IN (
                """ + ",".join("?" for _ in ids) + ") AND b.house=?",
                    [*ids, house],
                )
            ]

    def has_exact_location(self, query: Query) -> bool:
        """Проверить существование точной местности для ограничений запроса."""
        words = query.words.split()
        if len(words) < 2:
            return False
        match = " AND ".join('"' + word + '"' for word in words)
        with self.connect() as db:
            rows = db.execute(
                r"""
                SELECT
                  t.city_key,
                  t.street_key
                FROM
                  SEARCH AS s
                INNER JOIN
                  streets AS t
                  ON t.id = s.id
                WHERE
                  SEARCH MATCH ?
                ORDER BY
                  t.id
                LIMIT
                  1000
                """,
                (match,),
            )
            return any(whole_location_match(query, row[0], row[1]) for row in rows)

    def vocabulary_terms(self) -> list[str]:
        """Получить близкие словарные формы без изменения числовых ограничений."""
        return street_vocabulary(self.path, Path(self.path).stat().st_mtime_ns)

    def has_exact_locality_word(self, word: str) -> bool:
        """Проверить точное слово местности в словаре каталога."""
        with self.connect() as db:
            return (
                db.execute(
                    r"""
                SELECT
                  1
                FROM
                  SEARCH AS s
                INNER JOIN
                  streets AS t
                  ON t.id = s.id
                WHERE
                  SEARCH MATCH ?
                  AND instr(' ' || t.city_key || ' ', ' ' || ? || ' ') > 0
                LIMIT
                  1
                """,
                    ('"' + word.replace('"', '""') + '"', word),
                ).fetchone()
                is not None
            )

    def locality_prefix_choices(self, query: Query, candidates: list[dict]) -> dict | None:
        """Получить варианты местности по допустимому префиксу с подтверждением."""
        options = []
        known = {}
        for row in candidates:
            if (
                row["house"] != query.house
                or has_misaligned_source_fields(row)
                or not building_kind_matches(query, row)
            ):
                continue
            prefixes = locality_prefix_match(query, row)
            for word in prefixes:
                if word not in known:
                    known[word] = self.has_exact_locality_word(word)
            if prefixes and not any(known[word] for word in prefixes):
                options.append(row)
        if not options:
            return None
        with self.connect() as db:
            coded = {
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT building_id FROM codes WHERE building_id IN ("
                    + ",".join("?" for _ in options)
                    + ")",
                    [row["id"] for row in options],
                )
            }
        options = sorted((row for row in options if row["id"] in coded), key=lambda row: row["id"])
        if not options:
            return None
        return {
            "status": "ambiguous",
            "engine": "v2",
            "data": {},
            "clarification_reason": "locality_prefix",
            "candidates": [
                {"building_id": row["id"], "address": row["display"], "locality_context": row["lineage"]}
                for row in options[:100]
            ],
            "candidate_count": len(options),
        }

    def joined_variants(self, query: Query, candidates: list[dict]) -> list[Query]:
        """Подготовить варианты слитного написания по словарю файла."""
        if not self.joined_pairs(query):
            return []
        return super().joined_variants(query, candidates, self.vocabulary_terms())

    @measured("search.v2.alias_candidates")
    def alias_candidates(self, query: Query) -> list[dict]:
        """Получить дома по исходным вариантам названия и точному номеру."""
        with self.connect() as db:
            if not db.execute(r"""
                SELECT
                  1
                FROM
                  sqlite_master
                WHERE
                  name = 'alias_search'
                """).fetchone():
                return []
            match = " AND ".join('"' + w + '"*' for w in query.words.split())
            rows = db.execute(
                r"""
                SELECT
                  b.*,
                  s.words AS alias_words,
                  (
                    SELECT
                      a.street_key
                    FROM
                      aliases AS a
                    WHERE
                      a.building_id = b.id
                      AND a.house = s.house
                      AND a.city_key || ' ' || a.street_key = s.words
                    LIMIT
                      1
                  ) AS alias_street
                FROM
                  alias_search AS s
                INNER JOIN
                  buildings AS b
                  ON b.id = s.building_id
                WHERE
                  alias_search MATCH ?
                  AND s.house = ?
                LIMIT
                  ?
                """,
                (match, query.house, self.candidate_limit),
            )
            result = {}
            for row in rows:
                item = result.setdefault(row["id"], dict(row))
                item.setdefault("_aliases", []).append(row["alias_words"])
                if row["alias_street"]:
                    item.setdefault("_alias_streets", []).append(row["alias_street"])
                    item.setdefault("_alias_names", []).append(
                        {"words": row["alias_words"], "street": row["alias_street"]}
                    )
            return list(result.values())

    def building(self, id: str) -> dict | None:
        """Прочитать дом, его подъезды и коды по идентификатору."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM buildings WHERE id=?", (id,)).fetchone()
            if row:
                return dict(row)
            redirects = db.execute(r"""
                SELECT
                  value
                FROM
                  metadata
                WHERE
                  key = 'building_redirects'
                """).fetchone()
            if redirects:
                target = json.loads(redirects[0]).get(id)
                if isinstance(target, str) and target != id:
                    row = db.execute("SELECT * FROM buildings WHERE id=?", (target,)).fetchone()
                    if row:
                        return dict(row)
            if id.startswith("address:") and self._compact(db):
                parts = id.split(":", 3)
                if len(parts) != 4:
                    return None
                try:
                    house = base64.urlsafe_b64decode(parts[3] + "=" * ((-len(parts[3])) % 4)).decode()
                except (ValueError, UnicodeError, binascii.Error):
                    return None
                return next((r for r in self._expand(db, [parts[1]], house) if r["id"] == id), None)
        return None

    @measured("search.v2.read_codes")
    def answer(self, building: dict, entrance: str | None = None) -> dict:
        """Собрать результат выбранного адреса и доступных кодов."""
        with self.connect() as db:
            rows = db.execute(
                r"""
                SELECT
                  entrance,
                  kind,
                  code
                FROM
                  codes
                WHERE
                  building_id = ?
                ORDER BY
                  entrance,
                  kind,
                  code
                """,
                (building["id"],),
            ).fetchall()
        return self.format_answer(building, rows, entrance)

    def resolve(self, message: str, candidates: list[dict] | None = None) -> dict:
        """Найти адрес с сохранением точных ограничений дома и местности."""
        query, variants, early = self.prepare_query(message)
        if early is not None:
            return early
        cheap = [
            row for variant in variants for row in [*self.coded_candidates(variant), *self.alias_candidates(variant)]
        ]
        cheap_by_id = {r["id"]: r for r in cheap}
        fast = self._resolve_candidates(
            query, variants, list({r["id"]: r for r in [*(candidates or []), *cheap]}.values())
        )
        selected = (
            [fast["building_id"]]
            if fast["status"] == "found"
            else [r["building_id"] for r in fast.get("candidates", [])]
        )
        if selected and all((row := cheap_by_id.get(bid)) and complete_name_match(query, row) for bid in selected):
            # Only a complete canonical name permits this shortcut. A source
            # alias may explain split words; joined candidates must be read too.
            return fast
        variants.extend(self.joined_variants(query, candidates or []))
        indexed = [row for variant in variants for row in [*self.candidates(variant), *self.alias_candidates(variant)]]
        all_candidates = list({r["id"]: r for r in [*(candidates or []), *indexed, *cheap]}.values())
        result = self._resolve_candidates(query, variants, all_candidates)
        if result["status"] == "not_found" and not result.get("fallback_rejected"):
            return self.locality_prefix_choices(query, all_candidates) or result
        return result

    @measured("search.v2.rank")
    def _resolve_candidates(self, query: Query, variants: list[Query], candidates: list[dict]) -> dict:
        """Ранжировать файловые результаты и прочитать выбранные коды."""
        scored = self.score_candidates(query, variants, candidates)
        exact = self.has_exact_location(query) if not any(score == 1 for score, _ in scored) else False
        close = self.shortlist_candidates(query, scored, exact)
        if isinstance(close, dict):
            return close
        coded = self.coded_building_ids([row["id"] for row in close]) if len(close) > 1 else set()
        selected = self.choose_candidates(query, variants, close, coded)
        if "building" not in selected:
            return selected
        answer = self.answer(selected["building"], query.entrance)
        if answer["status"] == "no_codes" and scored[0][0] < 1:
            return {"status": "no_codes_ambiguous", "data": {}, "engine": "v2"}
        return answer

    def coded_building_ids(self, ids: list[str]) -> set[str]:
        """Получить наличие кодов у ограниченного списка домов."""
        if not ids:
            return set()
        with self.connect() as db:
            return {
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT building_id FROM codes WHERE building_id IN (" + ",".join("?" for _ in ids) + ")",
                    ids,
                )
            }


MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "refresh_interval": "-1",
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "address": {"type": "custom", "tokenizer": "standard", "filter": ["lowercase", "russian_stemmer"]}
            },
            "filter": {"russian_stemmer": {"type": "stemmer", "language": "russian"}},
        },
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "id": {"type": "keyword"},
            "words": {"type": "text", "analyzer": "address"},
            "house": {"type": "keyword"},
        },
    },
}


def search_body(message: str, limit: int = 100, *, fuzziness: str = "AUTO", prefix_length: int = 1) -> dict:
    """Построить лексический запрос OpenSearch для сравнения кандидатов."""
    query = parse_query(message)
    return {
        "size": limit,
        "query": {
            "bool": {
                "filter": [{"term": {"house": query.house or ""}}],
                "must": [
                    {
                        "match": {
                            "words": {
                                "query": query.words,
                                "operator": "and",
                                "fuzziness": fuzziness,
                                "prefix_length": prefix_length,
                            }
                        }
                    }
                ],
            }
        },
    }


@lru_cache(maxsize=2)
def street_vocabulary(path: str, revision: int) -> list[str]:
    """Собрать словарь слов из подготовленных документов улиц."""
    with Catalog(path).connect() as db:
        return [r[0] for r in db.execute("SELECT term FROM vocabulary")]


def documents(path: str, *, structured: bool = False) -> Iterator[dict]:
    """Прочитать подготовленные поисковые документы каталога."""
    catalog = Catalog(path)
    with catalog.connect() as db:
        compact = catalog._compact(db)
        for row in db.execute("SELECT * FROM streets"):
            if compact:
                packed = db.execute(
                    r"""
                    SELECT
                      data
                    FROM
                      street_houses
                    WHERE
                      id = ?
                    """,
                    (row["id"],),
                ).fetchone()[0]
                houses = sorted({house for b in json.loads(zlib.decompress(packed)) for house in b["houses"]})
            else:
                houses = [
                    r[0]
                    for r in db.execute(
                        r"""
                    SELECT DISTINCT
                      house
                    FROM
                      buildings
                    WHERE
                      city_key = ?
                      AND street_key = ?
                    ORDER BY
                      house
                    """,
                        (row["city_key"], row["street_key"]),
                    )
                ]
            document = {"id": row["id"], "words": row["words"], "house": houses}
            if structured:
                document.update(
                    city=row["city_key"],
                    street=row["street_key"],
                    coded_house=(
                        [
                            r[0]
                            for r in db.execute(
                                r"""
                    SELECT DISTINCT
                      house
                    FROM
                      buildings
                    WHERE
                      city_key = ?
                      AND street_key = ?
                      AND EXISTS
                      (
                        SELECT
                          1
                        FROM
                          codes AS c
                        WHERE
                          c.building_id = buildings.id
                      )
                    """,
                                (row["city_key"], row["street_key"]),
                            )
                        ]
                        if not compact or row["has_codes"]
                        else []
                    ),
                )
            yield document
