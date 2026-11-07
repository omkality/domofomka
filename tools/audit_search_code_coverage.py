"""Сверить сохранность уникальных кодов v1 в замороженных результатах поиска.

Совпадение одного адреса не доказывает сохранность всех кодов: v1 мог собрать
несколько исходных групп. Отчёт не доказывает правильность старого ответа.
Запросы и построчные результаты разрешены только в artifacts/private.
"""

import argparse
import collections
import concurrent.futures
import hashlib
import json
import time
from functools import partial
from pathlib import Path

from src.search.constraints import legacy_source_matches
from src.search.engine import Catalog
from src.services.source_index import lookup

worker_context = {}


def code_values(answer: dict) -> set[tuple[str, str, str]]:
    return {
        (str(entrance), str(kind), str(code))
        for entrance, items in answer.get("data", {}).items()
        for code, kind in items
    }


def offered_codes(result: dict, legacy: dict, read_codes) -> set[tuple[str, str, str]]:
    if result.get("engine") == "v1" and result["status"] == "found":
        return code_values(legacy)
    options = [result] if result["status"] == "found" else result.get("candidates", [])
    return set().union(*(read_codes(option["building_id"]) for option in options if option.get("building_id")))


def initialize(catalog_path, legacy, legacy_index, baseline=None):
    codes = collections.defaultdict(set)
    with Catalog(str(catalog_path)).connect() as db:
        for building_id, entrance, kind, code in db.execute("SELECT building_id,entrance,kind,code FROM codes"):
            codes[building_id].add((str(entrance), str(kind), str(code)))
    worker_context.update(codes=codes, legacy=str(legacy), index=str(legacy_index), baseline=baseline)


def compare(item):
    query, rows = item
    legacy = lookup(query, worker_context["legacy"], worker_context["index"])
    if legacy is None:
        raise ValueError("A compatible immutable legacy index is required")
    expected = code_values(legacy)
    validated = {}
    available = []
    for row in rows:
        result = row["result"]
        scope = result.get("fallback_code_scope") if result.get("engine") == "v1" else None
        if scope and scope not in {"source_identity", "whole_location"}:
            raise ValueError("Unknown frozen fallback code scope")
        if scope and scope not in validated:
            validated[scope] = lookup(
                query,
                worker_context["legacy"],
                worker_context["index"],
                accept_source=partial(legacy_source_matches, query, whole_location=scope == "whole_location"),
            )
            if validated[scope] is None:
                raise ValueError("A compatible immutable legacy index is required")
        answer = validated[scope] if scope else legacy
        available.append((row, offered_codes(result, answer, lambda key: worker_context["codes"].get(key, set()))))
    baseline = worker_context.get("baseline")
    baseline_codes = [codes for row, codes in available if row["variant"] == baseline] if baseline else []
    if baseline and not baseline_codes:
        raise ValueError("Baseline variant is missing for a query")
    if baseline_codes and any(codes != baseline_codes[0] for codes in baseline_codes):
        raise ValueError("Baseline returns different code sets for the same query")
    outcomes = []
    for row, actual in available:
        outcomes.append(
            {
                "query": query,
                "case_id": row["case_id"],
                "variant": row["variant"],
                "status": row["status"],
                "reference_retained": row["reference_retained"],
                "legacy_has_codes": bool(expected),
                "legacy_unique_codes": len(expected),
                "offered_unique_codes": len(actual),
                "missing_unique_codes": len(expected - actual),
                "extra_unique_codes": len(actual - expected),
                "code_difference_with_retained_reference": bool(expected - actual) and bool(row["reference_retained"]),
                **(
                    {
                        "baseline_missing_unique_codes": len(baseline_codes[0] - actual),
                        "baseline_extra_unique_codes": len(actual - baseline_codes[0]),
                    }
                    if baseline
                    else {}
                ),
            }
        )
    return outcomes


def run(args):
    private = Path("artifacts/private").resolve()
    output = args.output.resolve()
    if not output.is_relative_to(private):
        raise ValueError("Query-level reports must stay in artifacts/private")
    output.mkdir(parents=True, exist_ok=False)
    with args.catalog.open("rb") as stream:
        revision = hashlib.file_digest(stream, "sha256").hexdigest()
    if len({path.name for path in args.results}) != len(args.results):
        raise ValueError("Search run names must be unique")
    for path in args.results:
        frozen = json.loads((path / "frozen-input.json").read_text(encoding="utf8"))
        if revision != frozen["catalog_sha256"]:
            raise ValueError("Catalog differs from the frozen search run")
    groups = collections.defaultdict(list)
    for path in args.results:
        with (path / "results.jsonl").open(encoding="utf8") as stream:
            for line in stream:
                row = json.loads(line)
                if row["cohort"] == "historical":
                    row["variant"] = path.name + "/" + row["variant"]
                    groups[row["query"]].append(row)
    items = list(groups.items())[: args.limit or None]

    counters = collections.defaultdict(collections.Counter)
    started = time.monotonic()
    with (
        (output / "results.jsonl").open("w", encoding="utf8") as stream,
        concurrent.futures.ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=initialize,
            initargs=(args.catalog, args.legacy, args.legacy_index, args.baseline),
        ) as pool,
    ):
        for index, batch in enumerate(pool.map(compare, items, chunksize=20), 1):
            for row in batch:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                counts = counters[row["variant"]]
                counts["cases"] += 1
                if args.baseline:
                    counts["baseline_code_loss_cases"] += row["baseline_missing_unique_codes"] > 0
                    counts["baseline_code_gain_cases"] += row["baseline_extra_unique_codes"] > 0
                if row["legacy_has_codes"]:
                    counts["legacy_has_codes"] += 1
                    counts["all_legacy_codes_offered"] += row["missing_unique_codes"] == 0
                    counts["some_legacy_codes_missing"] += row["missing_unique_codes"] > 0
                    counts["code_difference_with_retained_reference"] += row["code_difference_with_retained_reference"]
                    counts["missing_with_reference_rejected"] += (
                        row["missing_unique_codes"] > 0 and not row["reference_retained"]
                    )
            if index % 5000 == 0:
                print(json.dumps({"queries": index, "seconds": round(time.monotonic() - started, 1)}), flush=True)
    with args.legacy.open("rb") as stream:
        legacy_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {
        "catalog_sha256": revision,
        "legacy_sha256": legacy_sha,
        "result_sha256": {
            path.name: hashlib.sha256((path / "results.jsonl").read_bytes()).hexdigest() for path in args.results
        },
        "auditor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "unique_queries": len(items),
        "workers": args.workers,
        "baseline_variant": args.baseline,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "results": dict(counters),
        "interpretation": "Unique entrance/kind/code tuples; choice union counts as offered, not automatically sent. "
        "Old code coverage is compatibility evidence, not proof of correct user intent.",
    }
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--legacy", type=Path, default=Path("codes.db"))
    parser.add_argument("--legacy-index", type=Path, default=Path("artifacts/search/legacy-index.db"))
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=4)
    parser.add_argument("--baseline", help="Сравнить также с указанным <имя-прогона>/<вариант>, не только с v1")
    parser.add_argument("--limit", type=int, default=0)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
