"""Replay every observed empty answer; finding a candidate does not prove user intent."""

import argparse
import collections
import concurrent.futures
import json
import time
from pathlib import Path

from tools.evaluate_search import catalogs, initialize, legacy_lookup


def evaluate(record):
    query = record["query"]
    old = bool(legacy_lookup(query))
    answer = catalogs[0].resolve(query) if len(query) <= 500 else {"status": "invalid_query"}
    candidates = [r["building_id"] for r in answer.get("candidates", [])]
    coded_choices = 0
    if candidates:
        with catalogs[0].connect() as db:
            coded_choices = db.execute(
                "SELECT count(DISTINCT building_id) FROM codes WHERE building_id IN ("
                + ",".join("?" for _ in candidates)
                + ")",
                candidates,
            ).fetchone()[0]
    return dict(
        record,
        v1_found=old,
        v2_status=answer["status"],
        v2_address=answer.get("address"),
        coded_choices=coded_choices,
        potential_recovery=bool(answer.get("data")) or bool(coded_choices),
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", default="artifacts/search/serving.db")
    p.add_argument("--legacy", default="codes.db")
    p.add_argument("--corpus", type=Path, default=Path("artifacts/private/audit/failed-queries.jsonl"))
    p.add_argument("--output", type=Path, default=Path("artifacts/private/failure-replay"))
    p.add_argument("--workers", type=int, default=5)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records = [json.loads(line) for line in args.corpus.read_text(encoding="utf8").splitlines()]
    counts = collections.Counter()
    patterns = collections.Counter()
    started = time.monotonic()
    with (
        concurrent.futures.ProcessPoolExecutor(
            max_workers=args.workers, initializer=initialize, initargs=(args.legacy, args.catalog)
        ) as pool,
        (args.output / "results.jsonl").open("w", encoding="utf8") as out,
    ):
        for i, result in enumerate(pool.map(evaluate, records, chunksize=25)):
            counts["total"] += 1
            counts["status:" + result["v2_status"]] += 1
            counts["v1_found"] += result["v1_found"]
            counts["with_coded_choices"] += bool(result["coded_choices"])
            counts["potential_recovery"] += result["potential_recovery"]
            if result["potential_recovery"] and not result["v1_found"]:
                counts["potential_recovery_v1_not_found"] += 1
                patterns.update(k for k, present in result["features"].items() if present)
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            if i % 1000 == 0:
                print(json.dumps({"evaluated_failures": i + 1, "total": len(records)}), flush=True)
    summary = {
        "counts": dict(counts),
        "potential_recovery_features": dict(patterns),
        "seconds": round(time.monotonic() - started, 2),
        "note": "Candidates are potential recoveries, not verified intended addresses. Repeated requests count separately.",
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf8")
    print(json.dumps(summary), flush=True)
