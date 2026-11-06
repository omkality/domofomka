"""Read-only PostgreSQL candidate backend for a local, unchanged v2 ranker."""

from contextlib import contextmanager
from functools import cached_property

from rapidfuzz.fuzz import ratio
from rapidfuzz.process import extract

from src.search.engine import Catalog
from src.search.normalize import Query
from tools.postgres_catalog_experiment import connect as experiment_connection


class Record(dict):
    def __getitem__(self, key):
        """Expose sqlite.Row's positional access for the shared reader only."""
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def record_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Record(zip(names, values, strict=True))


class SharedReader:
    """Translate the shared ranker's small, portable set of parameterised reads."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, parameters=()):
        return self.connection.execute(statement.replace("?", "%s"), parameters)


class PostgresExperiment(Catalog):
    def __init__(self, minimum_similarity=0.86, ambiguity_margin=0.06):
        super().__init__("postgres-experiment", minimum_similarity, ambiguity_margin)
        self.connection = experiment_connection()
        self.connection.row_factory = record_factory
        self.connection.execute("SET default_transaction_read_only=on")
        self.connection.execute("SET statement_timeout='10s'")

    def close(self):
        self.connection.close()

    @contextmanager
    def connect(self):
        yield SharedReader(self.connection)

    @cached_property
    def vocabulary(self):
        return [row[0] for row in self.connection.execute('SELECT term FROM vocabulary ORDER BY term COLLATE "C"')]

    @staticmethod
    def token(word) -> str:
        return "'" + word.replace("'", "''") + "'"

    def query(self, words, prefix):
        return " & ".join(self.token(word) + (":*" if prefix else "") for word in words)

    def fetch(self, match, terms, house, limit, coded=False):
        clause = "(" + " OR ".join("strpos(' '||street_key||' ',' '||%s||' ')>0" for _ in terms) + ")"
        sql = "SELECT id FROM streets WHERE to_tsvector('simple',words) @@ to_tsquery('simple',%s) AND " + clause
        if coded:
            sql += " AND has_codes=1"
        sql += ' ORDER BY has_codes DESC,id COLLATE "C" LIMIT %s'
        ids = [row[0] for row in self.connection.execute(sql, (match, *terms, max(limit * 10, 1000)))]
        with self.connect() as db:
            return self._expand(db, ids, house, include_known=not coded)

    def coded_candidates(self, query: Query, limit: int = 100):
        words = query.words.split()
        return (
            self.fetch(self.query(words, True), words, query.house, limit, coded=True) if words and query.house else []
        )

    def candidates(self, query: Query, limit: int = 100):
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

    def alias_candidates(self, query: Query):
        if not query.words or not query.house:
            return []
        rows = self.connection.execute(
            "SELECT b.*,s.words AS alias_words FROM alias_search s JOIN buildings b ON b.id=s.building_id "
            "WHERE to_tsvector('simple',s.words) @@ to_tsquery('simple',%s) AND s.house=%s ORDER BY s.seq LIMIT 100",
            (self.query(query.words.split(), True), query.house),
        )
        result = {}
        for row in rows:
            item = result.setdefault(row["id"], dict(row))
            item.setdefault("_aliases", []).append(row["alias_words"])
        return list(result.values())
