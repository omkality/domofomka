"""Rank candidates, but preserve explicit house and locality constraints."""

import base64
import binascii
import hashlib
import json
import re
import sqlite3
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from rapidfuzz.fuzz import ratio
from rapidfuzz.process import extract

from src.search.normalize import Query, address_words, parse_query, street_kind
from src.telemetry import measured, note


class Catalog:
    def __init__(self, path: str, minimum_similarity: float = 0.86, ambiguity_margin: float = 0.06):
        self.path = path
        self.minimum_similarity = minimum_similarity
        self.ambiguity_margin = ambiguity_margin

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect("file:" + Path(self.path).resolve().as_posix() + "?mode=ro", uri=True, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def _compact(self, db: sqlite3.Connection) -> bool:
        return db.execute("SELECT value FROM metadata WHERE key='format'").fetchone() is not None

    def _expand(self, db: sqlite3.Connection, ids: list[str], house: str, include_known: bool = True) -> list[dict]:
        if not ids:
            return []
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT DISTINCT b.* FROM streets t JOIN buildings b ON b.city_key=t.city_key AND "
                "b.street_key=t.street_key WHERE t.id IN (" + ",".join("?" for _ in ids) + ") AND b.house=?",
                [*ids, house],
            )
        ]
        if not include_known:
            return rows
        seen = {(r["display"].rsplit(", дом ", 1)[0], r["region"], r["lineage"]) for r in rows}
        for sid in ids:
            packed = db.execute("SELECT data FROM street_houses WHERE id=?", (sid,)).fetchone()
            if not packed:
                continue
            bundles = json.loads(zlib.decompress(packed[0]))
            street = db.execute("SELECT city_key,street_key FROM streets WHERE id=?", (sid,)).fetchone()
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
                        "city_key": street[0],
                        "street_key": street[1],
                        "region": b["region"],
                        "lineage": b["lineage"],
                    }
                )
        return rows

    @measured("search.v2.coded_candidates")
    def coded_candidates(self, query: Query, limit: int = 100) -> list[dict]:
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
                        "SELECT s.id FROM search s JOIN streets t ON t.id=s.id "
                        "WHERE search MATCH ? AND t.has_codes=1 AND " + clause + " ORDER BY t.id LIMIT ?",
                        (match, *words, max(limit * 10, 1000)),
                    )
                ]
                return self._expand(db, ids, query.house, include_known=False)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT DISTINCT b.* FROM search s JOIN streets t ON t.id=s.id "
                    "JOIN buildings b ON b.street_key=t.street_key AND b.city_key=t.city_key "
                    "WHERE search MATCH ? AND b.house=? AND "
                    + clause
                    + " AND EXISTS(SELECT 1 FROM codes c WHERE c.building_id=b.id) ORDER BY b.id LIMIT ?",
                    (match, query.house, *words, limit),
                )
            ]

    @measured("search.v2.candidates")
    def candidates(self, query: Query, limit: int = 100) -> list[dict]:
        words = query.words.split()
        if not words or not query.house:
            return []
        with self.connect() as db:
            compact = self._compact(db)

            def fetch(match: str, terms: list[str]) -> list[dict]:
                clause = "(" + " OR ".join("instr(' '||t.street_key||' ',' '||?||' ')>0" for _ in terms) + ")"
                if compact:
                    sql = (
                        "SELECT s.id FROM search s JOIN streets t ON t.id=s.id WHERE search MATCH ? AND "
                        + clause
                        + " ORDER BY t.has_codes DESC,t.id LIMIT ?"
                    )
                    ids = [r[0] for r in db.execute(sql, (match, *terms, max(limit * 10, 1000)))]
                    return self._expand(db, ids, query.house)
                sql = (
                    "SELECT DISTINCT b.* FROM search s JOIN streets t ON t.id=s.id "
                    "JOIN buildings b ON b.street_key=t.street_key AND b.city_key=t.city_key "
                    "WHERE search MATCH ? AND b.house=? AND "
                    + clause
                    + " ORDER BY EXISTS(SELECT 1 FROM codes c WHERE c.building_id=b.id) DESC,b.id LIMIT ?"
                )
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
        if not ids:
            return []
        with self.connect() as db:
            if self._compact(db):
                return self._expand(db, ids, house)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT DISTINCT b.* FROM streets t JOIN buildings b ON b.city_key=t.city_key AND "
                    "b.street_key=t.street_key WHERE t.id IN (" + ",".join("?" for _ in ids) + ") AND b.house=?",
                    [*ids, house],
                )
            ]

    @measured("search.v2.alias_candidates")
    def alias_candidates(self, query: Query) -> list[dict]:
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='alias_search'").fetchone():
                return []
            match = " AND ".join('"' + w + '"*' for w in query.words.split())
            rows = db.execute(
                "SELECT b.*,s.words AS alias_words FROM alias_search s JOIN buildings b ON b.id=s.building_id "
                "WHERE alias_search MATCH ? AND s.house=? LIMIT 100",
                (match, query.house),
            )
            result = {}
            for row in rows:
                item = result.setdefault(row["id"], dict(row))
                item.setdefault("_aliases", []).append(row["alias_words"])
            return list(result.values())

    def building(self, id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM buildings WHERE id=?", (id,)).fetchone()
            if row:
                return dict(row)
            redirects = db.execute("SELECT value FROM metadata WHERE key='building_redirects'").fetchone()
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

    @staticmethod
    def similarity(query: str, document: str) -> float:
        tokens = document.split()
        if any(any(c.isdigit() for c in word) and word not in tokens for word in query.split()):
            return 0
        return min((max((ratio(w, t) / 100 for t in tokens), default=0) for w in query.split()), default=0)

    @measured("search.v2.read_codes")
    def answer(self, building: dict, entrance: str | None = None) -> dict:
        with self.connect() as db:
            rows = db.execute(
                "SELECT entrance,kind,code FROM codes WHERE building_id=? ORDER BY entrance,kind,code",
                (building["id"],),
            ).fetchall()
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
            "identity": "gar"
            if building["guid"]
            else ("gar_address" if building["id"].startswith("address:") else "legacy_unverified"),
        }

    def resolve(self, message: str, candidates: list[dict] | None = None) -> dict:
        query = parse_query(message)
        note(
            normalized_query={
                "words": query.words,
                "house": query.house,
                "entrance": query.entrance,
                "kind": query.kind,
            }
        )
        if not message.strip():
            return {"status": "not_found", "data": {}, "engine": "v2"}
        if not query.house or not query.words:
            # Unsupported house formats and streets named like a type (e.g.
            # "Набережная") still get the unchanged v1 compatibility path.
            numeric_street = any(word.isdigit() for word in query.words.split())
            if (query.house and any(c.isalpha() for c in message)) or (
                not query.house and any(c.isdigit() for c in message) and not numeric_street
            ):
                return {"status": "not_found", "data": {}, "engine": "v2"}
            return {"status": "incomplete_query", "data": {}, "engine": "v2"}
        variants = [query]
        # Work before stop-word removal: "по" is also an ordinary preposition.
        for match in re.finditer(r"\b(ново|старо|по)\s+([а-я]+)\b", query.raw):
            joined = query.raw[: match.start()] + match[1] + match[2] + query.raw[match.end() :]
            alternate = parse_query(joined)
            if alternate.house == query.house:
                variants.append(Query(query.raw, alternate.words, query.house, query.entrance, query.kind))
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
        for bid in selected:
            row = cheap_by_id.get(bid)
            if row and any(
                set(v.words.split()) - set(address_words(row["city"]).split())
                == set(address_words(row["street"]).split())
                for v in variants
            ):
                return fast
        indexed = [row for variant in variants for row in self.candidates(variant)]
        return self._resolve_candidates(
            query, variants, list({r["id"]: r for r in [*(candidates or []), *indexed, *cheap]}.values())
        )

    @measured("search.v2.rank")
    def _resolve_candidates(self, query: Query, variants: list[Query], candidates: list[dict]) -> dict:
        # Backend scores never decide house identity or permit dropping a city token.
        scored = []
        for row in candidates:
            if row["house"] != query.house:
                continue
            if not row["guid"] and re.search(
                r"\b(?:ул|улица|проспект|пр-кт|шоссе|бульвар|набережная|переулок|проезд)\b", row["city"], re.I
            ):
                # Misaligned source columns are not a verified v2 address. Let
                # compatibility fallback preserve v1 until the row is reviewed.
                continue
            street_words = address_words(row["street"]).split()
            # A missing ordinal must not silently select another named street.
            if any(part.isdigit() and part not in query.words.split() for part in street_words):
                continue
            if not any(
                (word == part if any(c.isdigit() for c in word) else ratio(word, part) / 100 >= self.minimum_similarity)
                for variant in variants
                for word in variant.words.split()
                for part in street_words
            ):
                continue
            if (
                query.kind
                and street_kind(row["display"].removeprefix(row["city"] + ", ").split(" ", 1)[0]) != query.kind
                and query.kind not in {street_kind(word) for word in row["street"].split()}
            ):
                continue
            score = max(
                self.similarity(variant.words, document)
                for variant in variants
                for document in [address_words(row["city"] + " " + row["street"]), *row.get("_aliases", [])]
            )
            if score >= self.minimum_similarity:
                scored.append((score, row))
        scored.sort(key=lambda v: (-v[0], v[1]["id"]))
        if not scored:
            return {"status": "not_found", "data": {}, "engine": "v2"}
        best = scored[0][0]
        # Code availability must not replace an exact address with a fuzzy one.
        cutoff = 1 if best == 1 else best - self.ambiguity_margin
        close = [row for score, row in scored if score >= cutoff]
        if len(close) > 1:
            with self.connect() as db:
                ids = [r["id"] for r in close]
                coded = {
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT building_id FROM codes WHERE building_id IN ("
                        + ",".join("?" for _ in ids)
                        + ")",
                        ids,
                    )
                }
            if coded:
                close = [row for row in close if row["id"] in coded]
            else:
                return {"status": "no_codes_ambiguous", "data": {}, "engine": "v2"}
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
        return self.answer(close[0], query.entrance)


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
    with Catalog(path).connect() as db:
        return [r[0] for r in db.execute("SELECT term FROM vocabulary")]


def documents(path: str, *, structured: bool = False) -> Iterator[dict]:
    catalog = Catalog(path)
    with catalog.connect() as db:
        compact = catalog._compact(db)
        for row in db.execute("SELECT * FROM streets"):
            if compact:
                packed = db.execute("SELECT data FROM street_houses WHERE id=?", (row["id"],)).fetchone()[0]
                houses = sorted({house for b in json.loads(zlib.decompress(packed)) for house in b["houses"]})
            else:
                houses = [
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT house FROM buildings WHERE city_key=? AND street_key=? ORDER BY house",
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
                                "SELECT DISTINCT house FROM buildings WHERE city_key=? AND street_key=? "
                                "AND EXISTS(SELECT 1 FROM codes c WHERE c.building_id=buildings.id)",
                                (row["city_key"], row["street_key"]),
                            )
                        ]
                        if not compact or row["has_codes"]
                        else []
                    ),
                )
            yield document
