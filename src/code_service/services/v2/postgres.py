"""Асинхронный читатель: одно read-only соединение и общий CPU-ранжировщик."""

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from rapidfuzz.fuzz import ratio
from rapidfuzz.process import extract

from src.code_service.services.v2.constraints import (
    complete_name_match,
    has_misaligned_source_fields,
    locality_prefix_match,
    whole_location_match,
)
from src.code_service.services.v2.normalize import Query, building_kind_matches
from src.code_service.services.v2.ranking import Ranker
from src.infrastructure.telemetry import measured

EXACT_LOCALITY_SQL = """
SELECT
  1
FROM
  streets
WHERE
  to_tsvector('simple', words) @@ to_tsquery('simple', %s)
  AND strpos(' ' || city_key || ' ', ' ' || %s || ' ') > 0
LIMIT
  1
"""

EXACT_LOCATION_SQL = """
SELECT
  city_key,
  street_key
FROM
  streets
WHERE
  to_tsvector('simple', words) @@ to_tsquery('simple', %s)
ORDER BY
  id
LIMIT
  1000
"""


class PostgresCatalog(Ranker):
    """Читать SQL через asyncio с прежним пределом одновременно выполняемой работы."""

    def __init__(
        self,
        dsn: str,
        minimum_similarity: float = 0.86,
        ambiguity_margin: float = 0.06,
        *,
        connect_timeout_seconds: int = 3,
        statement_timeout_ms: int = 2000,
        lock_timeout_seconds: float = 2,
        operation_timeout_seconds: float = 5,
        revision: str = "unknown",
        candidate_limit: int = 100,
    ):
        """Сохранить прежние бюджеты, ревизию и предел кандидатов."""
        self.minimum_similarity = minimum_similarity
        self.ambiguity_margin = ambiguity_margin
        self.candidate_limit = candidate_limit
        self._dsn = dsn
        self._connect_timeout = connect_timeout_seconds
        self._statement_timeout = statement_timeout_ms
        self._lock_timeout = lock_timeout_seconds
        self._operation_timeout = operation_timeout_seconds
        self._revision = revision
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._connection: psycopg.AsyncConnection[dict] | None = None
        self._vocabulary: list[str] | None = None

    async def _disconnect(self) -> None:
        """Закрыть повреждённую или больше не используемую сессию."""
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()

    async def close(self) -> None:
        """Дождаться текущей операции и освободить соединение при остановке API."""
        async with self._lock:
            await self._disconnect()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[psycopg.AsyncConnection[dict]]:
        """Ограничить всю операцию, включая ожидание блокировки, SQL и вычисления."""
        task = asyncio.current_task()
        if self._owner is task:
            yield self._connection
            return
        deadline = asyncio.get_running_loop().time() + self._operation_timeout
        try:
            async with asyncio.timeout(min(self._lock_timeout, self._operation_timeout)):
                await self._lock.acquire()
        except TimeoutError as exc:
            raise TimeoutError("Catalog reader busy") from exc
        self._owner = task
        try:
            async with asyncio.timeout_at(deadline):
                if self._connection is None or self._connection.closed:
                    self._connection = await psycopg.AsyncConnection.connect(
                        self._dsn,
                        autocommit=True,
                        row_factory=dict_row,
                        connect_timeout=self._connect_timeout,
                        application_name="domofomka-v2-reader",
                        options=f"-c default_transaction_read_only=on -c statement_timeout={self._statement_timeout}",
                    )
                    rows = await self.rows(
                        self._connection,
                        """
                        SELECT
                          key,
                          value
                        FROM
                          metadata
                        WHERE
                          key IN ('catalog_schema_version', 'publication_state', 'source_catalog_sha256')
                        """,
                    )
                    metadata = {row["key"]: row["value"] for row in rows}
                    if metadata.get("catalog_schema_version") != "1" or metadata.get("publication_state") != "ready":
                        raise ValueError("Incompatible or incomplete catalog")
                    if self._revision != "unknown" and metadata.get("source_catalog_sha256") != self._revision:
                        raise ValueError("Catalog revision differs from configuration")
                yield self._connection
        except BaseException:
            await self._disconnect()
            raise
        finally:
            self._owner = None
            self._lock.release()

    @staticmethod
    async def rows(db: psycopg.AsyncConnection[dict], statement: str, parameters: Sequence[Any] = ()) -> list[dict]:
        """Прочитать SQL-результат и закрыть курсор до CPU-обработки."""
        async with await db.execute(statement, parameters) as cursor:
            return await cursor.fetchall()

    @staticmethod
    def token(word: str) -> str:
        """Подготовить лексему tsquery, оставляя значения параметрами SQL."""
        return "'" + word.replace("'", "''") + "'"

    def query(self, words: list[str], prefix: bool) -> str:
        """Собрать прежнее полнотекстовое выражение."""
        return " & ".join(self.token(word) + (":*" if prefix else "") for word in words)

    async def vocabulary_terms(self) -> list[str]:
        """Один раз загрузить словарь; сравнение слов выполняется отдельно от SQL."""
        if self._vocabulary is None:
            async with self.connect() as db:
                rows = await self.rows(
                    db,
                    """
                    SELECT
                      term
                    FROM
                      vocabulary
                    ORDER BY
                      term COLLATE "C"
                    """,
                )
                self._vocabulary = [row["term"] for row in rows]
        return self._vocabulary

    async def _expand(
        self, db: psycopg.AsyncConnection[dict], ids: list[str], house: str, include_known: bool = True
    ) -> list[dict]:
        """Пакетно прочитать дома, затем распаковать ГАР вне цикла событий."""
        if not ids:
            return []
        rows = await self.rows(
            db,
            """
            SELECT DISTINCT
              b.*
            FROM
              streets AS t
            INNER JOIN
              buildings AS b
              ON b.city_key = t.city_key AND b.street_key = t.street_key
            WHERE
              t.id = ANY(%s) AND b.house = %s
            """,
            (ids, house),
        )
        if not include_known:
            return rows
        packed = await self.rows(
            db,
            """
            SELECT
              t.id,
              h.data,
              t.city_key,
              t.street_key
            FROM
              streets AS t
            INNER JOIN
              street_houses AS h
              ON h.id = t.id
            WHERE
              t.id = ANY(%s)
            """,
            (ids,),
        )
        return await asyncio.to_thread(self.expand_houses, ids, house, rows, {row["id"]: row for row in packed})

    async def fetch(self, match: str, terms: list[str], house: str, limit: int, coded: bool = False) -> list[dict]:
        """Получить прежний ограниченный набор улиц через GIN."""
        clause = "(" + " OR ".join("strpos(' '||street_key||' ',' '||%s||' ')>0" for _ in terms) + ")"
        statement = """
            SELECT
              id
            FROM
              streets
            WHERE
              to_tsvector('simple', words) @@ to_tsquery('simple', %s)
              AND
            """ + clause
        if coded:
            statement += " AND has_codes = 1"
        statement += ' ORDER BY has_codes DESC, id COLLATE "C" LIMIT %s'
        async with self.connect() as db:
            rows = await self.rows(db, statement, (match, *terms, limit * 10))
            return await self._expand(db, [row["id"] for row in rows], house, include_known=not coded)

    @measured("search.v2.coded_candidates")
    async def coded_candidates(self, query: Query, limit: int | None = None) -> list[dict]:
        """Отобрать адреса с кодами по точному префиксному запросу."""
        words = query.words.split()
        if not words or not query.house:
            return []
        return await self.fetch(
            self.query(words, True), words, query.house, self.candidate_limit if limit is None else limit, coded=True
        )

    @measured("search.v2.candidates")
    async def candidates(self, query: Query, limit: int | None = None) -> list[dict]:
        """Выполнить точный поиск и прежнее исправление опечаток RapidFuzz."""
        words = query.words.split()
        if not words or not query.house:
            return []
        limit = self.candidate_limit if limit is None else limit
        result = await self.fetch(self.query(words, True), words, query.house, limit)
        if result:
            return result
        vocabulary = await self.vocabulary_terms()
        groups, terms = [], []
        for word in words:
            alternatives = (
                [word]
                if any(char.isdigit() for char in word)
                else [
                    row[0]
                    for row in await asyncio.to_thread(
                        extract, word, vocabulary, scorer=ratio, limit=3, score_cutoff=80
                    )
                ]
            )
            if not alternatives:
                return []
            terms.extend(alternatives)
            groups.append("(" + " | ".join(self.token(word) for word in alternatives) + ")")
        return await self.fetch(" & ".join(groups), list(dict.fromkeys(terms)), query.house, limit)

    @measured("search.v2.alias_candidates")
    async def alias_candidates(self, query: Query) -> list[dict]:
        """Прочитать исходные варианты написания с прежними пределом и порядком."""
        if not query.words or not query.house:
            return []
        async with self.connect() as db:
            rows = await self.rows(
                db,
                """
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
                  to_tsvector('simple', s.words) @@ to_tsquery('simple', %s)
                  AND s.house = %s
                ORDER BY
                  s.seq
                LIMIT
                  %s
                """,
                (self.query(query.words.split(), True), query.house, self.candidate_limit),
            )
        result: dict = {}
        for row in rows:
            item = result.setdefault(row["id"], dict(row))
            item.setdefault("_aliases", []).append(row["alias_words"])
            if row["alias_street"]:
                item.setdefault("_alias_streets", []).append(row["alias_street"])
                item.setdefault("_alias_names", []).append({"words": row["alias_words"], "street": row["alias_street"]})
        return list(result.values())

    async def has_exact_location(self, query: Query) -> bool:
        """Проверить точную местность перед отказом от отсутствующего дома."""
        words = query.words.split()
        if len(words) < 2:
            return False
        async with self.connect() as db:
            rows = await self.rows(db, EXACT_LOCATION_SQL, (self.query(words, False),))
        return any(whole_location_match(query, row["city_key"], row["street_key"]) for row in rows)

    async def has_exact_locality_word(self, word: str) -> bool:
        """Проверить полное слово населённого пункта перед уточнением по префиксу."""
        async with self.connect() as db:
            return bool(await self.rows(db, EXACT_LOCALITY_SQL, (self.token(word), word)))

    async def coded_building_ids(self, ids: list[str]) -> set[str]:
        """Прочитать наличие кодов без загрузки их содержимого."""
        if not ids:
            return set()
        async with self.connect() as db:
            rows = await self.rows(
                db,
                """
                SELECT DISTINCT
                  building_id
                FROM
                  codes
                WHERE
                  building_id = ANY(%s)
                """,
                (ids,),
            )
        return {row["building_id"] for row in rows}

    async def locality_prefix_choices(self, query: Query, candidates: list[dict]) -> dict | None:
        """Уточнить допустимый префикс города с обязательным подтверждением."""
        options, known = [], {}
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
                    known[word] = await self.has_exact_locality_word(word)
            if prefixes and not any(known[word] for word in prefixes):
                options.append(row)
        coded = await self.coded_building_ids([row["id"] for row in options])
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

    async def load_candidates(self, ids: list[str], house: str) -> list[dict]:
        """Прочитать документы необязательного внешнего отбора кандидатов."""
        async with self.connect() as db:
            return await self._expand(db, ids, house)

    async def building(self, id: str) -> dict | None:
        """Найти дом по ID, перенаправлению или компактному адресу ГАР."""
        async with self.connect() as db:
            rows = await self.rows(db, "SELECT * FROM buildings WHERE id = %s", (id,))
            if rows:
                return rows[0]
            redirects = await self.rows(db, "SELECT value FROM metadata WHERE key = 'building_redirects'")
            if redirects:
                target = json.loads(redirects[0]["value"]).get(id)
                if isinstance(target, str) and target != id:
                    rows = await self.rows(db, "SELECT * FROM buildings WHERE id = %s", (target,))
                    if rows:
                        return rows[0]
            if id.startswith("address:"):
                parts = id.split(":", 3)
                if len(parts) != 4:
                    return None
                try:
                    house = base64.urlsafe_b64decode(parts[3] + "=" * ((-len(parts[3])) % 4)).decode()
                except (ValueError, UnicodeError, binascii.Error):
                    return None
                return next((row for row in await self._expand(db, [parts[1]], house) if row["id"] == id), None)
        return None

    @measured("search.v2.read_codes")
    async def answer(self, building: dict, entrance: str | None = None) -> dict:
        """Прочитать отсортированные коды и применить общий формат ответа."""
        async with self.connect() as db:
            rows = await self.rows(
                db,
                """
                SELECT
                  entrance,
                  kind,
                  code
                FROM
                  codes
                WHERE
                  building_id = %s
                ORDER BY
                  entrance,
                  kind,
                  code
                """,
                (building["id"],),
            )
        return self.format_answer(building, rows, entrance)

    @measured("search.v2.rank")
    async def _resolve_candidates(self, query: Query, variants: list[Query], candidates: list[dict]) -> dict:
        """Ранжировать вне цикла событий и асинхронно прочитать выбранные коды."""
        scored = await asyncio.to_thread(self.score_candidates, query, variants, candidates)
        exact = await self.has_exact_location(query) if not any(score == 1 for score, _ in scored) else False
        close = self.shortlist_candidates(query, scored, exact)
        if isinstance(close, dict):
            return close
        coded = await self.coded_building_ids([row["id"] for row in close]) if len(close) > 1 else set()
        selected = self.choose_candidates(query, variants, close, coded)
        if "building" not in selected:
            return selected
        answer = await self.answer(selected["building"], query.entrance)
        if answer["status"] == "no_codes" and scored[0][0] < 1:
            return {"status": "no_codes_ambiguous", "data": {}, "engine": "v2"}
        return answer

    async def resolve(self, message: str, candidates: list[dict] | None = None) -> dict:
        """Выполнить прежние этапы поиска в пределах одного клиентского дедлайна."""
        query, variants, early = await asyncio.to_thread(self.prepare_query, message)
        if early is not None:
            return early
        async with self.connect():
            cheap = []
            for variant in variants:
                cheap.extend(await self.coded_candidates(variant))
                cheap.extend(await self.alias_candidates(variant))
            cheap_by_id = {row["id"]: row for row in cheap}
            fast = await self._resolve_candidates(
                query, variants, list({row["id"]: row for row in [*(candidates or []), *cheap]}.values())
            )
            selected = (
                [fast["building_id"]]
                if fast["status"] == "found"
                else [row["building_id"] for row in fast.get("candidates", [])]
            )
            if selected and all((row := cheap_by_id.get(id)) and complete_name_match(query, row) for id in selected):
                return fast
            if self.joined_pairs(query):
                variants.extend(
                    await asyncio.to_thread(
                        super().joined_variants, query, candidates or [], await self.vocabulary_terms()
                    )
                )
            indexed = []
            for variant in variants:
                indexed.extend(await self.candidates(variant))
                indexed.extend(await self.alias_candidates(variant))
            combined = list({row["id"]: row for row in [*(candidates or []), *indexed, *cheap]}.values())
            result = await self._resolve_candidates(query, variants, combined)
            if result["status"] == "not_found" and not result.get("fallback_rejected"):
                return await self.locality_prefix_choices(query, combined) or result
            return result
