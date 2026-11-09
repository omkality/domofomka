"""Разрез полного прогона по упоминаниям и дополнительная равномерная выборка населённых пунктов."""

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from tools.evaluation.evaluate_search import source_fingerprints
from tools.history.audit_query_localities import LocalityMatcher, mentions, query_key, words


def select_cases(rows, *, excluded: set[str], per_place: int = 8) -> tuple[list[dict], dict]:
    if not 1 <= per_place <= 32:
        raise ValueError("Per-place sample must be between 1 and 32")
    counts = Counter()
    pools = defaultdict(dict)
    for row in rows:
        if row["next_reply"] not in {"no_results", "no_immediate_reply"}:
            continue
        selected = row["mentions"]
        if len(selected) != 1:
            counts["skipped:multiple_names" if selected else "skipped:no_name"] += 1
            continue
        place = selected[0]
        if place["evidence"] == "street_or_locality":
            counts["skipped:street_context"] += 1
            continue
        key = query_key(row["query"])
        if key in excluded or len(row["query"]) > 500:
            counts["skipped:existing_or_too_long"] += 1
            continue
        pool = pools[place["key"]]
        previous = pool.get(key)
        if previous is None or (row["dialog"], row["query"]) < (previous["dialog"], previous["query"]):
            pool[key] = row
    cases = []
    by_place = []
    for key, pool in sorted(pools.items()):
        ordered = sorted(pool.values(), key=lambda r: hashlib.sha256(query_key(r["query"]).encode()).hexdigest())
        numbered = [r for r in ordered if re.search(r"\d", r["query"])]
        without_number = [r for r in ordered if not re.search(r"\d", r["query"])]
        selected = numbered[:per_place]
        if without_number and (per_place > 1 or not numbered):
            selected = numbered[: per_place - 1] + without_number[:1]
        for row in selected:
            split = "holdout" if int(hashlib.sha256(row["dialog"].encode()).hexdigest()[:8], 16) % 5 == 0 else "train"
            cases.append(
                {
                    "query": row["query"],
                    "dialog": row["dialog"],
                    "split": split,
                    "cohort": "observed_unanswered_locality",
                    "evidence": "next_reply_observation_not_truth",
                    "source_locality": row["mentions"][0],
                    "source_next_reply": row["next_reply"],
                    "source_message_date": row.get("date"),
                }
            )
        by_place.append(
            {
                "key": key,
                "name": ordered[0]["mentions"][0]["name"],
                "eligible_distinct_queries": len(pool),
                "selected": len(selected),
            }
        )
    counts.update(eligible_places=len(pools), selected_cases=len(cases))
    return cases, {**dict(counts), "per_place": by_place}


def stratify(frozen: dict, result_path: Path, matcher: LocalityMatcher, observed: list[dict]) -> dict:
    cases = frozen["cases"]
    variants = {variant["name"] for variant in frozen["variants"]}
    labels = [mentions(matcher.find(case["query"])) for case in cases]
    total, by_place, seen = defaultdict(Counter), defaultdict(lambda: defaultdict(Counter)), set()
    for line in result_path.open(encoding="utf8"):
        row = json.loads(line)
        index, variant = row["case_id"], row["variant"]
        assert variant in variants
        assert 0 <= index < len(cases)
        assert row["query"] == cases[index]["query"]
        assert (index, variant) not in seen
        seen.add((index, variant))
        groups = [total[variant], *(by_place[label["key"]][variant] for label in labels[index])]
        for group in groups:
            group[row["cohort"] + ":cases"] += 1
            group[row["cohort"] + ":" + row["status"]] += 1
            if row.get("reference_retained") is not None:
                group[row["cohort"] + ":with_reference"] += 1
                group[row["cohort"] + ":reference_retained"] += bool(row["reference_retained"])
            group["backend_errors"] += bool(row.get("backend_error"))
            group["reviewed_conflicts"] += bool(row.get("literal_conflict"))
    assert len(seen) == len(cases) * len(variants)
    records = {
        " ".join(words(r["name"])): {k: r[k] for k in ("name", "category", "messages", "dialogs")} for r in observed
    }
    for matches in labels:
        for match in matches:
            records.setdefault(
                match["key"], {"name": match["name"], "category": match["category"], "messages": 0, "dialogs": 0}
            )
    return {
        "cases": len(cases),
        "responses": len(seen),
        "complete": True,
        "cases_without_locality_mention": sum(not matches for matches in labels),
        "cases_with_multiple_mentions": sum(len(matches) > 1 for matches in labels),
        "global_results": {name: dict(total[name]) for name in sorted(variants)},
        "by_place": [
            dict(records[key], variants={name: dict(by_place[key][name]) for name in sorted(variants)})
            for key in sorted(records)
        ],
    }


def build(audit_path: Path, frozen_path: Path, catalog: Path, output: Path, per_place: int) -> dict:
    if output.exists():
        raise FileExistsError("Preserve previous comparison; choose a new output")
    frozen = json.loads((frozen_path / "frozen-input.json").read_text(encoding="utf8"))
    if frozen["sources"] != source_fingerprints():
        raise ValueError("Frozen comparison uses different runtime sources; replay before slicing")
    with catalog.open("rb") as stream:
        catalog_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    if frozen["catalog_sha256"] != catalog_sha:
        raise ValueError("Catalog differs from the frozen comparison")
    facts = json.loads((audit_path / "locality-facts.json").read_text(encoding="utf8"))
    summary = json.loads((audit_path / "summary.json").read_text(encoding="utf8"))
    with sqlite3.connect("file:" + catalog.resolve().as_posix() + "?mode=ro", uri=True) as db:
        street_names = {r[0] for r in db.execute("SELECT DISTINCT street_key FROM streets")}
    matcher = LocalityMatcher([p for region in facts for p in region["places"]], street_names)
    excluded = {query_key(case["query"]) for case in frozen["cases"]}
    with (audit_path / "incoming.jsonl").open(encoding="utf8") as stream:
        cases, selection = select_cases(map(json.loads, stream), excluded=excluded, per_place=per_place)
    comparison = stratify(frozen, frozen_path / "results.jsonl", matcher, summary["places"])
    output.mkdir(parents=True)
    (output / "cases.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in cases), encoding="utf8"
    )
    (output / "baseline-slices.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf8")
    report = {
        "catalog_sha256": catalog_sha,
        "benchmark_sources_unchanged": True,
        "analysis_sources": {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in ("tools/history/audit_query_localities.py", "tools/history/build_locality_cohort.py")
        },
        "audit_source_sha256": summary["source_sha256"],
        "frozen_results_sha256": hashlib.sha256((frozen_path / "results.jsonl").read_bytes()).hexdigest(),
        "selection": selection,
        "cases_sha256": hashlib.sha256((output / "cases.jsonl").read_bytes()).hexdigest(),
        "notes": [
            "Равномерная выборка проверяет охват, а не распределение трафика.",
            "Отсутствие прежнего ответа не доказывает наличие кодов или ошибку поиска.",
            "Названия и диалоговый split не являются проверенной разметкой адресного намерения.",
        ],
    }
    (output / "selection.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=Path("artifacts/search/serving.source-fields-final.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-place", type=int, choices=range(1, 33), default=8)
    args = parser.parse_args()
    report = build(args.audit, args.frozen, args.catalog, args.output, args.per_place)
    print(json.dumps({k: report["selection"][k] for k in ("eligible_places", "selected_cases")}))


if __name__ == "__main__":
    main()
