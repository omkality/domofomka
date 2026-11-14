"""Сравнить весь ответ v2 с одинаковым fallback v1, без обращений к VK и DaData."""

import argparse
import collections
import hashlib
import json
import os
import time
from pathlib import Path

from src.code_service.config import settings
from src.code_service.services.v2 import search
from src.code_service.services.v2.engine import Catalog
from src.code_service.services.v2.postgres import PostgresCatalog
from src.infrastructure.event_loop import run_async
from tools.experiments.postgres_search_experiment import PostgresExperiment


def select(corpus: Path, limit: int):
    """Выбрать фиксированный набор случаев для сравнения хранилищ."""
    rows, seen = [], set()
    for name in ("correction-hypotheses", "successful-queries"):
        for line in (corpus / (name + ".jsonl")).read_text(encoding="utf8").splitlines():
            row = json.loads(line)
            key = (row["query"], row["expected_address"], row["split"], row["evidence"])
            if key not in seen:
                rows.append(row)
                seen.add(key)
    rows.sort(key=lambda r: hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest())
    return rows[:limit] if limit else rows


def signature(result: dict):
    """Получить устойчивое представление результата без непостоянных измерений."""
    return {
        key: result[key]
        for key in ("status", "engine", "address", "building_id", "candidates", "fallback_reason", "fallback_rejected")
        if key in result
    }


async def compare(args):
    """Сравнить результаты одинаковых запросов на проверяемых вариантах."""
    if args.output.exists():
        raise FileExistsError("Preserve completed results; use a new output directory")
    args.output.mkdir(parents=True)
    settings.search_catalog_path = str(args.catalog)
    settings.db_name = str(args.legacy)
    settings.legacy_index_path = str(args.legacy_index)
    settings.opensearch_url = None
    settings.search_cache_ttl_seconds = 0
    postgres = (
        PostgresCatalog(
            os.environ.get("CATALOG_BUILD_DSN") or os.environ["CATALOG_BENCHMARK_DSN"],
            settings.search_min_similarity,
            settings.search_ambiguity_margin,
        )
        if args.implementation == "production"
        else PostgresExperiment(settings.search_min_similarity, settings.search_ambiguity_margin)
    )
    baseline_factory = search.get_catalog
    sqlite = Catalog(str(args.catalog), settings.search_min_similarity, settings.search_ambiguity_margin)
    records = select(args.corpus, args.limit)
    times = {"sqlite": [], "postgres": []}
    counts = collections.Counter()
    fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()

    async def lookup(backend, query):
        """Получить результат текущего варианта поиска для контрольного запроса."""

        async def selected_catalog():
            """Выдать читатель текущего сравниваемого варианта."""
            return sqlite if backend == "sqlite" else postgres

        search.get_catalog = selected_catalog
        started = time.perf_counter()
        result = await search.lookup(query)
        elapsed = (time.perf_counter() - started) * 1000
        return result, elapsed

    try:
        # Both lexicons and the v1 sidecar are warmed before the paired samples.
        for backend in times:
            for q in ("мичурина 1а", "Москва Коломенская 2"):
                await lookup(backend, q)
        with (args.output / "results.jsonl").open("w", encoding="utf8") as stream:
            for i, record in enumerate(records):
                values = {}
                for backend in ["sqlite", "postgres"] if i % 2 else ["postgres", "sqlite"]:
                    result, elapsed = await lookup(backend, record["query"])
                    values[backend] = result
                    times[backend].append(elapsed)
                    counts[f"{backend}:{result.get('engine')}:{result['status']}"] += 1
                same = values["sqlite"] == values["postgres"]
                counts["same"] += same
                row = {
                    "query": record["query"],
                    "same_full_response": same,
                    "sqlite": signature(values["sqlite"]),
                    "postgres": signature(values["postgres"]),
                    "sqlite_ms": round(times["sqlite"][-1], 3),
                    "postgres_ms": round(times["postgres"][-1], 3),
                }
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                if i % 100 == 0:
                    stream.flush()
                    print(
                        json.dumps({"evaluated": i + 1, "total": len(records), "differences": i + 1 - counts["same"]}),
                        flush=True,
                    )
        summary = {
            "postgres_implementation": args.implementation,
            "queries": len(records),
            "fingerprint": fingerprint,
            "counts": dict(counts),
            "latency": {
                key: {
                    "p50_ms": round(sorted(values)[len(values) // 2], 3),
                    "p95_ms": round(sorted(values)[int(len(values) * 0.95)], 3),
                }
                for key, values in times.items()
            },
            "note": "Парные вызовы того же v2 и v1. Проверяется сохранность поведения, не намерение пользователя. PostgreSQL использует постоянное read-only соединение; кеш результатов выключен.",
        }
        (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        search.get_catalog = baseline_factory
        if isinstance(postgres, PostgresCatalog):
            await postgres.close()
        else:
            postgres.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--implementation", choices=["production", "experiment"], default="production")
    p.add_argument("--catalog", type=Path, default=Path("artifacts/search/serving.db"))
    p.add_argument("--legacy", type=Path, default=Path("codes.db"))
    p.add_argument("--legacy-index", type=Path, default=Path("artifacts/search/legacy-index.db"))
    p.add_argument("--corpus", type=Path, default=Path("artifacts/private/audit"))
    p.add_argument("--output", type=Path, default=Path("artifacts/private/postgres-benchmark/paired-4000"))
    p.add_argument("--limit", type=int, default=4000)
    run_async(compare(p.parse_args()))
