"""Сравнить pg_trgm с текущим читателем только на отдельной локальной копии каталога."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import time
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from rapidfuzz.fuzz import ratio
from rapidfuzz.process import extract

from src.code_service.services.v2.normalize import Query
from src.code_service.services.v2.postgres import PostgresCatalog
from src.infrastructure.event_loop import run_async


class TrigramCatalog(PostgresCatalog):
    """Экспериментальный отбор улиц; точный дом и ранжирование остаются рабочими."""

    mode = "word"

    async def trigrams(self, query: Query, coded: bool) -> list[dict]:
        """Найти похожие улицы с прежним пределом и стабильным порядком."""
        if not query.words or not query.house:
            return []
        words = query.words.split()
        if self.mode == "whole":
            predicate, values = "words %% %s", [query.words]
        else:
            predicate, values = " AND ".join("%s <<%% words" for _ in words), words
        coded_filter = "AND has_codes = 1" if coded else ""
        statement = f"""
            SELECT
              id
            FROM
              streets
            WHERE
              {predicate}
              {coded_filter}
            ORDER BY
              has_codes DESC,
              id COLLATE "C"
            LIMIT
              %s
            """
        async with self.connect() as db:
            ids = [row["id"] for row in await self.rows(db, statement, (*values, self.candidate_limit * 10))]
            return await self._expand(db, ids, query.house, include_known=not coded)

    async def coded_candidates(self, query: Query, limit: int | None = None) -> list[dict]:
        """Отобрать дома с кодами через триграммы."""
        return await self.trigrams(query, True)

    async def candidates(self, query: Query, limit: int | None = None) -> list[dict]:
        """Отобрать известные дома через триграммы, сохранив конечные ограничения."""
        return await self.trigrams(query, False)


class VocabularyCatalog(PostgresCatalog):
    """Проверить pg_trgm как короткий список для существующего RapidFuzz."""

    async def candidates(self, query: Query, limit: int | None = None) -> list[dict]:
        """Сохранить точный поиск, заменив только извлечение словарных альтернатив."""
        words = query.words.split()
        if not words or not query.house:
            return []
        limit = self.candidate_limit if limit is None else limit
        result = await self.fetch(self.query(words, True), words, query.house, limit)
        if result:
            return result
        groups, terms = [], []
        for word in words:
            if any(char.isdigit() for char in word):
                alternatives = [word]
            else:
                async with self.connect() as db:
                    shortlist = [
                        row["term"]
                        for row in await self.rows(
                            db,
                            r"""
                            SELECT
                              term
                            FROM
                              vocabulary
                            WHERE
                              term %% %s
                            ORDER BY
                              similarity(term, %s) DESC,
                              term
                            LIMIT
                              200
                            """,
                            (word, word),
                        )
                    ]
                alternatives = [row[0] for row in extract(word, shortlist, scorer=ratio, limit=3, score_cutoff=80)]
            if not alternatives:
                return []
            terms.extend(alternatives)
            groups.append("(" + " | ".join(self.token(value) for value in alternatives) + ")")
        return await self.fetch(" & ".join(groups), list(dict.fromkeys(terms)), query.house, limit)


def summary(values: list[float]) -> dict[str, float]:
    """Свести длительности без публикации пользовательских запросов."""
    ordered = sorted(values)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[int(len(ordered) * 0.95)], 3),
        "max_ms": round(max(ordered), 3),
    }


async def main() -> None:
    """Измерить неизменяемый корпус и сохранить закрытые ответы и публичные агрегаты."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reuse-indexes", action="store_true")
    parser.add_argument("--reverse", action="store_true")
    args = parser.parse_args()
    dsn = os.environ["TRIGRAM_BENCHMARK_DSN"]
    connection = conninfo_to_dict(dsn)
    if connection.get("host") not in {"localhost", "127.0.0.1"} or connection["dbname"] != "catalog_benchmark":
        raise ValueError("Only the disposable local catalog_benchmark database is allowed")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"corpus_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(), "cases": len(cases), "profiles": {}}
    with psycopg.connect(dsn, autocommit=True) as admin:
        report["version"] = admin.execute("SHOW server_version").fetchone()[0]
        report["rows"] = admin.execute(r"""
            SELECT
              count(*),
              sum(has_codes)
            FROM
              streets
            """).fetchone()
        report["original_indexes"] = admin.execute(r"""
            SELECT
              indexrelname,
              pg_relation_size(indexrelid)
            FROM
              pg_stat_user_indexes
            ORDER BY
              indexrelname
            """).fetchall()
        admin.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        builds = {}
        for name, table, column, suffix in [
            ("bench_street_trigrams", "streets", "words", ""),
            ("bench_coded_trigrams", "streets", "words", " WHERE has_codes = 1"),
            ("bench_vocabulary_trigrams", "vocabulary", "term", ""),
        ]:
            start = time.perf_counter()
            if not args.reuse_indexes:
                admin.execute(rf"""
                    CREATE INDEX {name} ON {table} USING GIN ({column} gin_trgm_ops) WITH (fastupdate = off)
                    """ + suffix)
            builds[name] = {
                "reused": args.reuse_indexes,
                "seconds": None if args.reuse_indexes else round(time.perf_counter() - start, 3),
                "bytes": admin.execute("SELECT pg_relation_size(%s::regclass)", (name,)).fetchone()[0],
            }
        report["index_builds"] = builds
        admin.execute("ANALYZE")
    profiles = [
        ("current", PostgresCatalog),
        ("trigram_whole", TrigramCatalog),
        ("trigram_word", TrigramCatalog),
        ("trigram_vocabulary", VocabularyCatalog),
    ]
    if args.reverse:
        profiles.reverse()
    report["profile_order"] = [name for name, _ in profiles]
    outputs = {}
    for name, factory in profiles:
        reader = factory(dsn)
        if name == "trigram_whole":
            reader.mode = "whole"
        timings, output = [], []
        try:
            async with reader.connect() as db:
                # Подключение читателя только для чтения; настройка лишь текущей сессии.
                await db.execute(r"""
                    SELECT
                      set_config('pg_trgm.similarity_threshold', '0.2', false)
                    """)
                await db.execute(r"""
                    SELECT
                      set_config('pg_trgm.strict_word_similarity_threshold', '0.5', false)
                    """)
            for case in cases:
                start = time.perf_counter()
                try:
                    result = await reader.resolve(case["query"])
                except Exception as error:
                    result = {"status": "error", "error_type": type(error).__name__}
                elapsed = (time.perf_counter() - start) * 1000
                timings.append(elapsed)
                output.append({"result": result, "milliseconds": elapsed})
        finally:
            await reader.close()
        outputs[name] = output
        report["profiles"][name] = dict(
            summary(timings),
            errors=sum(row["result"]["status"] == "error" for row in output),
        )
        (args.output / (name + ".json")).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(name, json.dumps(report["profiles"][name]), flush=True)
    baseline = outputs["current"]
    for name, output in outputs.items():
        report["profiles"][name].update(
            differences=sum(old["result"] != new["result"] for old, new in zip(baseline, output, strict=True)),
            lost_found=sum(
                bool(old["result"].get("data")) and old["result"] != new["result"]
                for old, new in zip(baseline, output, strict=True)
            ),
        )
    (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    run_async(main())
