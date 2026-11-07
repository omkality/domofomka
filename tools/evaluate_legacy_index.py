"""Сравнить индекс v1 с прежним выбором адреса на всём закрытом корпусе."""

import argparse
import concurrent.futures
import json
import sqlite3
import time
from pathlib import Path

from src.services.source_index import lookup
from tools import evaluate_search as reference

paths = []


def initialize(source, index):
    paths[:] = [source, index]
    with sqlite3.connect(source) as db:
        reference.legacy_groups[:] = db.execute(
            "SELECT min(id),city,street_type,street,house FROM codes GROUP BY city,street_type,street,house ORDER BY min(id)"
        ).fetchall()


def evaluate(query):
    expected = reference.legacy_lookup(query)
    start = time.perf_counter()
    result = lookup(query, *paths)
    return {
        "query": query,
        "match": result is not None and result.get("address") == expected,
        "ms": (time.perf_counter() - start) * 1000,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", default="codes.db")
    p.add_argument("--index", default="artifacts/search/legacy-index.db")
    p.add_argument("--corpus", type=Path, default=Path("artifacts/private/audit"))
    p.add_argument("--output", type=Path, default=Path("artifacts/private/legacy-index-check"))
    p.add_argument("--workers", type=int, default=6)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    queries = sorted(
        {
            json.loads(line)["query"]
            for name in ("successful-queries", "correction-hypotheses")
            for line in (a.corpus / (name + ".jsonl")).read_text(encoding="utf8").splitlines()
        }
    )
    times = []
    bad = []
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=a.workers, initializer=initialize, initargs=(a.source, a.index)
    ) as pool:
        for i, result in enumerate(pool.map(evaluate, queries, chunksize=30)):
            times.append(result["ms"])
            if not result["match"]:
                bad.append(result)
            if i % 1000 == 0:
                print(json.dumps({"evaluated": i + 1, "total": len(queries)}), flush=True)
    times.sort()
    summary = {
        "queries": len(queries),
        "address_disagreements": len(bad),
        "p50_ms": round(times[len(times) // 2], 3),
        "p95_ms": round(times[int(len(times) * 0.95)], 3),
    }
    (a.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf8")
    (a.output / "disagreements.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in bad), encoding="utf8"
    )
    print(json.dumps(summary), flush=True)
