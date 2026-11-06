"""Replay a dialog-split corpus against an exact in-memory v1 snapshot and v2.

Snapshot selection mirrors the unchanged legacy matcher, without scanning one
million identical street/code rows for every query. Only aggregate metrics publish.
"""

import argparse
import collections
import concurrent.futures
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

import httpx

from src.search.engine import Catalog, search_body
from src.search.normalize import address_words, legacy_house_matches, parse_query
from src.services.codes import address_exists, street_types

legacy_groups = []
legacy_cache = {}
expected_ids = {}
catalogs = []
search_clients = []
search_config = {}


def source_fingerprints() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in [
            "tools/evaluate_search.py",
            "src/search/normalize.py",
            "src/search/engine.py",
            "src/services/codes.py",
        ]
    }


def initialize(legacy: str, catalog_path: str, opensearch_url: str = "", query_config: dict | None = None):
    search_config.clear()
    search_config.update(query_config or {})
    with sqlite3.connect(legacy) as db:
        legacy_groups[:] = db.execute(
            "SELECT min(id),city,street_type,street,house FROM codes GROUP BY city,street_type,street,house ORDER BY min(id)"
        ).fetchall()
    catalogs[:] = [Catalog(catalog_path)]
    if opensearch_url:
        search_clients[:] = [(opensearch_url, httpx.Client(trust_env=False, timeout=2))]
    with catalogs[0].connect() as db:
        for r in db.execute("SELECT building_id,city_key,street_key,house FROM aliases"):
            expected_ids.setdefault((address_words(r["city_key"] + " " + r["street_key"]), r["house"]), set()).add(
                r["building_id"]
            )


def legacy_lookup(message: str):
    if not message:
        return None
    parts = re.split("[^а-я]", message)
    for kind in street_types:
        if kind in parts:
            parts.remove(kind)
    if not parts:
        return None
    word = max(parts, key=len).lower()
    if word not in legacy_cache:
        legacy_cache[word] = [r for r in legacy_groups if word in r[1].lower() or word in r[3].lower()]
    rows = [r for r in legacy_cache[word] if address_exists(message, r[1], r[3], r[4], r[2])]
    if not rows:
        return None
    city = min((r[1] for r in rows), key=len)
    r = next(r for r in rows if city in r[1])
    return f"{city}, {r[2]} {r[3]}, дом {r[4]}"


def evaluate(record: dict):
    q = record["query"]
    start = time.perf_counter()
    old = legacy_lookup(q)
    historical = record["expected_address"]
    reference = old or (legacy_lookup(record["correction"]) if record.get("correction") else None) or historical
    candidates = None
    backend = "catalog"
    if search_clients:
        try:
            url, client = search_clients[0]
            response = client.post(url + "/domofomka-buildings/_search", json=search_body(q, **search_config))
            if response.status_code != 200:
                raise RuntimeError("Index unavailable")
            ids = [r["_id"] for r in response.json()["hits"]["hits"]]
            candidates = catalogs[0].load_candidates(ids, parse_query(q).house) or None
            backend = "opensearch"
        except Exception:
            backend = "catalog_after_index_failure"
    result = catalogs[0].resolve(q, candidates) if len(q) <= 500 else {"status": "invalid_query", "engine": "v2"}
    if result["status"] == "not_found" and old and legacy_house_matches(q, old):
        result = {"status": "found", "address": old, "engine": "v1"}
    expected = parse_query(reference)
    ids = expected_ids.get((expected.words, expected.house), set())

    def key(value: str) -> tuple:
        return (parse_query(value).words, parse_query(value).house)

    matched = (result.get("building_id") in ids) or (
        bool(result.get("address")) and key(result["address"]) == key(reference)
    )
    options = result.get("candidates", [])
    offered = any(c["building_id"] in ids or key(c["address"]) == key(reference) for c in options)
    return {
        "split": record["split"],
        "evidence": record["evidence"],
        "v1_found": bool(old),
        "candidate_backend": backend,
        "historical_pair_uncertain": key(historical) != key(reference),
        "v2_status": result["status"],
        "matched": matched,
        "offered": offered,
        "v2_engine": result.get("engine", "v2"),
        "milliseconds": round((time.perf_counter() - start) * 1000, 3),
        "query": q,
        "expected": reference,
        "actual": result.get("address"),
        "dialog": record["dialog"],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--legacy", default="codes.db")
    p.add_argument("--catalog", default="artifacts/search/catalog.db")
    p.add_argument("--corpus", type=Path, default=Path("artifacts/private/audit"))
    p.add_argument("--limit", type=int, default=4000)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--opensearch-url", default="")
    p.add_argument("--fuzziness", choices=["AUTO", "0", "1", "2"], default="AUTO")
    p.add_argument("--prefix-length", type=int, choices=range(4), default=1)
    p.add_argument("--candidate-limit", type=int, default=100)
    p.add_argument("--output", type=Path, default=Path("artifacts/private/evaluation"))
    a = p.parse_args()
    sources = source_fingerprints()
    with Path(a.catalog).open("rb") as catalog_file:
        catalog_sha256 = hashlib.file_digest(catalog_file, "sha256").hexdigest()
    a.output.mkdir(parents=True, exist_ok=True)
    records = []
    seen = set()
    for name in ["correction-hypotheses", "successful-queries"]:
        for line in (a.corpus / (name + ".jsonl")).read_text(encoding="utf8").splitlines():
            row = json.loads(line)
            identity = (row["query"], row["expected_address"], row["split"], row["evidence"])
            if identity not in seen:
                records.append(row)
                seen.add(identity)
    records.sort(key=lambda r: hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest())
    records = records[: a.limit] if a.limit else records
    (a.output / "inputs.json").write_text(
        json.dumps(
            {
                "source_sha256": sources,
                "catalog_sha256": catalog_sha256,
                "records_sha256": hashlib.sha256(
                    json.dumps(records, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
                "queries": len(records),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf8",
    )
    counters = collections.Counter()
    times = []
    with (
        concurrent.futures.ProcessPoolExecutor(
            max_workers=a.workers,
            initializer=initialize,
            initargs=(
                a.legacy,
                a.catalog,
                a.opensearch_url,
                {"fuzziness": a.fuzziness, "prefix_length": a.prefix_length, "limit": a.candidate_limit},
            ),
        ) as pool,
        (a.output / "results.jsonl").open("w", encoding="utf8") as file,
    ):
        for i, result in enumerate(pool.map(evaluate, records, chunksize=25)):
            category = result["split"] + ":" + result["evidence"]
            counters[category + ":total"] += 1
            for field in ["v1_found", "matched", "offered"]:
                counters[category + ":" + field] += bool(result[field])
            counters[category + ":status:" + result["v2_status"]] += 1
            counters["backend:" + result["candidate_backend"]] += 1
            times.append(result["milliseconds"])
            file.write(json.dumps(result, ensure_ascii=False) + "\n")
            if i % 500 == 0:
                print(json.dumps({"evaluated": i + 1, "total": len(records)}), flush=True)
    times.sort()
    if source_fingerprints() != sources:
        raise RuntimeError("Source changed during replay; discard these results and rerun on a frozen revision")
    summary = {
        "queries": len(records),
        "query_configuration": (
            {"fuzziness": a.fuzziness, "prefix_length": a.prefix_length, "limit": a.candidate_limit}
            if a.opensearch_url
            else None
        ),
        "counts": dict(counters),
        "p50_ms": times[len(times) // 2] if times else 0,
        "p95_ms": times[int(len(times) * 0.95)] if times else 0,
        "note": "Прежние ответы и предполагаемые исправления — предварительная разметка. Варианты выбора считаются отдельно от правильных автоматических ответов.",
    }
    (a.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(summary), flush=True)
