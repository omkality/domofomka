"""Каталог v2: одно ограниченное соединение на процесс, общий алгоритм ранжирования."""

import threading
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from functools import cached_property
from typing import Any

import psycopg
from rapidfuzz.fuzz import ratio
from rapidfuzz.process import extract

from src.code_service.v2.engine import Catalog
from src.code_service.v2.normalize import Query
from src.infrastructure.telemetry import measured


class Record(dict):
    def __getitem__(self, key: str | int) -> Any:
        """Сохранить именованный и позиционный доступ общего читателя."""
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def record_factory(cursor: Any) -> Callable[[Sequence[Any]], Record]:
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Record(zip(names, values, strict=True))


class BoundedConnection(psycopg.Connection):
    deadline: float | None = None

    def wait(self, gen: Any, interval: float = 0.1, timeout: float | None = None) -> Any:
        """Ограничить ожидание клиента, даже если сервер не исполняет statement_timeout."""
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise TimeoutError("Catalog operation deadline exceeded")
            timeout = remaining if timeout is None else min(timeout, remaining)
        try:
            return super().wait(gen, interval=interval, timeout=timeout)
        except psycopg.OperationalError:
            self.close()
            raise


class SharedReader:
    def __init__(self, connection: BoundedConnection):
        self.connection = connection

    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> psycopg.Cursor:
        # Only the shared ranker's fixed SQL uses this adapter. Values remain bound parameters.
        return self.connection.execute(statement.replace("?", "%s"), parameters)


class PostgresCatalog(Catalog):
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
        super().__init__("postgres", minimum_similarity, ambiguity_margin, candidate_limit=candidate_limit)
        self._dsn = dsn
        self._connect_timeout = connect_timeout_seconds
        self._statement_timeout = statement_timeout_ms
        self._lock_timeout = lock_timeout_seconds
        self._operation_timeout = operation_timeout_seconds
        self._revision = revision
        self._lock = threading.RLock()
        self._local = threading.local()
        self._connection = None

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    @contextmanager
    def connect(self) -> Generator[SharedReader, None, None]:
        outer = not getattr(self._local, "depth", 0)
        started = time.monotonic()
        if not self._lock.acquire(timeout=min(self._lock_timeout, self._operation_timeout)):
            raise TimeoutError("Catalog reader busy")
        try:
            if outer:
                deadline = started + self._operation_timeout
                fresh = self._connection is None or self._connection.closed
                if fresh:
                    self._connection = BoundedConnection.connect(
                        self._dsn,
                        autocommit=True,
                        row_factory=record_factory,
                        connect_timeout=self._connect_timeout,
                        application_name="domofomka-v2-reader",
                        options=f"-c default_transaction_read_only=on -c statement_timeout={self._statement_timeout}",
                    )
                self._connection.deadline = deadline
                if fresh:
                    metadata = {
                        r["key"]: r["value"]
                        for r in self._connection.execute(
                            "SELECT key,value FROM metadata WHERE key IN "
                            "('catalog_schema_version','publication_state','source_catalog_sha256')"
                        )
                    }
                    if metadata.get("catalog_schema_version") != "1" or metadata.get("publication_state") != "ready":
                        raise ValueError("Incompatible or incomplete catalog")
                    if self._revision != "unknown" and metadata.get("source_catalog_sha256") != self._revision:
                        raise ValueError("Catalog revision differs from configuration")
            self._local.depth = getattr(self._local, "depth", 0) + 1
            try:
                if time.monotonic() >= self._connection.deadline:
                    raise TimeoutError("Catalog operation deadline exceeded")
                yield SharedReader(self._connection)
                if time.monotonic() >= self._connection.deadline:
                    raise TimeoutError("Catalog operation deadline exceeded")
            finally:
                self._local.depth -= 1
        except (psycopg.Error, TimeoutError, ValueError):
            self.close()
            raise
        finally:
            if outer and self._connection is not None:
                self._connection.deadline = None
            self._lock.release()

    def resolve(self, message: str, candidates: list[dict] | None = None) -> dict:
        # Keep all reads for one ranking operation on the same bounded connection.
        with self.connect():
            return super().resolve(message, candidates)

    @cached_property
    def vocabulary(self) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute('SELECT term FROM vocabulary ORDER BY term COLLATE "C"')]

    def vocabulary_terms(self) -> list[str]:
        return self.vocabulary

    def has_exact_locality_word(self, word: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM streets WHERE to_tsvector('simple',words) @@ to_tsquery('simple',%s) "
                    "AND strpos(' '||city_key||' ',' '||%s||' ')>0 LIMIT 1",
                    (self.token(word), word),
                ).fetchone()
                is not None
            )

    @staticmethod
    def token(word: str) -> str:
        return "'" + word.replace("'", "''") + "'"

    def query(self, words: list[str], prefix: bool) -> str:
        return " & ".join(self.token(word) + (":*" if prefix else "") for word in words)

    def fetch(self, match: str, terms: list[str], house: str, limit: int, coded: bool = False) -> list[dict]:
        clause = "(" + " OR ".join("strpos(' '||street_key||' ',' '||%s||' ')>0" for _ in terms) + ")"
        sql = "SELECT id FROM streets WHERE to_tsvector('simple',words) @@ to_tsquery('simple',%s) AND " + clause
        if coded:
            sql += " AND has_codes=1"
        sql += ' ORDER BY has_codes DESC,id COLLATE "C" LIMIT %s'
        with self.connect() as db:
            ids = [row[0] for row in db.execute(sql, (match, *terms, limit * 10))]
            return self._expand(db, ids, house, include_known=not coded)

    @measured("search.v2.coded_candidates")
    def coded_candidates(self, query: Query, limit: int | None = None) -> list[dict]:
        limit = self.candidate_limit if limit is None else limit
        words = query.words.split()
        return (
            self.fetch(self.query(words, True), words, query.house, limit, coded=True) if words and query.house else []
        )

    @measured("search.v2.candidates")
    def candidates(self, query: Query, limit: int | None = None) -> list[dict]:
        limit = self.candidate_limit if limit is None else limit
        words = query.words.split()
        if not words or not query.house:
            return []
        result = self.fetch(self.query(words, True), words, query.house, limit)
        if result:
            return result
        groups, terms = [], []
        for word in words:
            alternatives = (
                [word]
                if any(c.isdigit() for c in word)
                else [r[0] for r in extract(word, self.vocabulary, scorer=ratio, limit=3, score_cutoff=80)]
            )
            if not alternatives:
                return []
            terms.extend(alternatives)
            groups.append("(" + " | ".join(self.token(w) for w in alternatives) + ")")
        return self.fetch(" & ".join(groups), list(dict.fromkeys(terms)), query.house, limit)

    @measured("search.v2.alias_candidates")
    def alias_candidates(self, query: Query) -> list[dict]:
        if not query.words or not query.house:
            return []
        with self.connect() as db:
            rows = db.execute(
                "SELECT b.*,s.words AS alias_words,(SELECT a.street_key FROM aliases a WHERE a.building_id=b.id "
                "AND a.house=s.house AND a.city_key||' '||a.street_key=s.words LIMIT 1) AS alias_street "
                "FROM alias_search s JOIN buildings b ON b.id=s.building_id "
                "WHERE to_tsvector('simple',s.words) @@ to_tsquery('simple',%s) "
                "AND s.house=%s ORDER BY s.seq LIMIT %s",
                (self.query(query.words.split(), True), query.house, self.candidate_limit),
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

    def has_exact_location(self, query: Query) -> bool:
        from src.code_service.v2.constraints import whole_location_match

        words = query.words.split()
        if len(words) < 2:
            return False
        with self.connect() as db:
            rows = db.execute(
                "SELECT city_key,street_key FROM streets WHERE to_tsvector('simple',words) "
                "@@ to_tsquery('simple',%s) ORDER BY id LIMIT 1000",
                (self.query(words, False),),
            )
            return any(whole_location_match(query, row["city_key"], row["street_key"]) for row in rows)
