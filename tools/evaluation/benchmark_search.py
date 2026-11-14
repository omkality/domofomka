"""Сравнение настроек OpenSearch на закрытом корпусе реальных запросов."""

import argparse
import collections
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from src.code_service.services.v2.normalize import ordinals

VARIANTS = [
    ("catalog", None),
    ("strict", {"fuzziness": "1", "prefix_length": 2, "limit": 50}),
    ("default", {"fuzziness": "AUTO", "prefix_length": 1, "limit": 100}),
    ("prefix_zero", {"fuzziness": "AUTO", "prefix_length": 0, "limit": 100}),
    ("expanded", {"fuzziness": "AUTO", "prefix_length": 0, "limit": 200}),
    ("two_edits", {"fuzziness": "2", "prefix_length": 1, "limit": 100}),
]


def patterns(query: str) -> dict[str, bool]:
    """Определить признаки пользовательских ошибок для стратификации выборки."""
    q = query.casefold().replace("ё", "е")
    return {
        "entrance": bool(re.search(r"под[ъь]?езд|под\.", q)),
        "written_ordinal": ordinals(q) != q,
        "numeric_street": bool(re.search(r"\d+[- ]?(?:я|й)\b", q)),
        "city_alias": bool(re.search(r"\b(?:мск|спб|питер)\b", q)),
        "joined_words_and_numbers": bool(re.search(r"[а-я]{3,}\d|\d[а-я]{3,}", q)),
        "house_component_words": bool(re.search(r"корп|строен", q)),
        "split_street_prefix": bool(re.search(r"\b(?:ново|старо|по)\s+[а-я]+", q)),
    }


def make_sample(source: Path, output: Path, limit: int) -> list[dict]:
    """Выбрать детерминированный набор запросов с фиксированным разделением."""
    selected = {}

    def key(row: dict) -> tuple:
        """Построить устойчивый ключ сравнения текущей записи."""
        return row["query"], row["expected_address"], row["split"], row["evidence"]

    def read(name: str) -> list[dict]:
        """Прочитать данные текущего контрольного или адресного результата."""
        values = [json.loads(line) for line in (source / (name + ".jsonl")).read_text(encoding="utf8").splitlines()]
        return sorted(values, key=lambda r: hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest())

    # Keep correction hypotheses prominent; they remain weak intent labels.
    for row in read("correction-hypotheses"):
        if len(selected) >= min(limit * 2 // 3, limit):
            break
        selected[key(row)] = row
    successful = read("successful-queries")
    for feature in patterns(""):
        count = 0
        for row in successful:
            if len(selected) >= limit or count >= 100:
                break
            if patterns(row["query"])[feature] and key(row) not in selected:
                selected[key(row)] = row
                count += 1
    for row in successful:
        if len(selected) >= limit:
            break
        selected.setdefault(key(row), row)
    output.mkdir(parents=True, exist_ok=True)
    for filename, evidence in [
        ("correction-hypotheses", "same_dialog_same_house_correction_hypothesis"),
        ("successful-queries", "bot_address_response"),
    ]:
        rows = [r for r in selected.values() if r["evidence"] == evidence]
        (output / (filename + ".jsonl")).write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf8"
        )
    return list(selected.values())


def summarize(path: Path) -> dict:
    """Подсчитать агрегированные показатели качества и задержки прогона."""
    counts = collections.Counter()
    by_pattern = collections.defaultdict(collections.Counter)
    for line in (path / "results.jsonl").read_text(encoding="utf8").splitlines():
        row = json.loads(line)
        retained = row["matched"] or row["offered"]
        baseline = row["evidence"] == "bot_address_response" and row["v1_found"]
        metrics = {
            "total": 1,
            "matched": row["matched"],
            "offered": row["offered"],
            "retained": retained,
            "baseline_total": baseline,
            "baseline_retained": baseline and retained,
            "baseline_automatic_disagreement": baseline and row["v2_status"] == "found" and not row["matched"],
            "index_failed": row["candidate_backend"] == "catalog_after_index_failure",
        }
        counts.update({k: int(v) for k, v in metrics.items()})
        for feature, present in patterns(row["query"]).items():
            if present:
                by_pattern[feature].update({k: int(v) for k, v in metrics.items()})
    return {"counts": dict(counts), "patterns": {k: dict(v) for k, v in by_pattern.items()}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("artifacts/private/audit"))
    parser.add_argument("--catalog", default="artifacts/search/serving.db")
    parser.add_argument("--output", type=Path, default=Path("artifacts/private/opensearch-matrix"))
    parser.add_argument("--url", default="http://127.0.0.1:19200")
    parser.add_argument("--limit", type=int, default=4000)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    sample_dir = args.output / "corpus"
    sample = make_sample(args.corpus, sample_dir, args.limit)
    fingerprint = hashlib.sha256("".join(json.dumps(r, sort_keys=True) for r in sample).encode()).hexdigest()
    report = {
        "sample_items": len(sample),
        "sample_sha256": fingerprint,
        "variants": [],
        "note": "Корпус включает реальные запросы и предполагаемые исправления. Это сравнение конфигураций, а не независимая оценка точности.",
    }
    source_files = [
        Path("tools/catalog/sqlite_catalog.py"),
        Path("src/code_service/services/v2/normalize.py"),
        Path("tools/evaluation/evaluate_search.py"),
    ]
    source_hash = hashlib.sha256(b"".join(p.read_bytes() for p in source_files)).hexdigest()
    for name, config in VARIANTS:
        assert hashlib.sha256(b"".join(p.read_bytes() for p in source_files)).hexdigest() == source_hash
        destination = args.output / name
        command = [
            sys.executable,
            "-m",
            "tools.evaluation.evaluate_search",
            "--corpus",
            str(sample_dir),
            "--catalog",
            args.catalog,
            "--output",
            str(destination),
            "--limit",
            "0",
            "--workers",
            str(args.workers),
        ]
        if config:
            command += [
                "--opensearch-url",
                args.url,
                "--fuzziness",
                config["fuzziness"],
                "--prefix-length",
                str(config["prefix_length"]),
                "--candidate-limit",
                str(config["limit"]),
            ]
        with (args.output / (name + ".log")).open("w", encoding="utf8") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        summary = json.loads((destination / "summary.json").read_text(encoding="utf8"))
        variant = {
            "name": name,
            "configuration": config,
            "p50_ms": summary["p50_ms"],
            "p95_ms": summary["p95_ms"],
            **summarize(destination),
        }
        report["variants"].append(variant)
        (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps({"completed": name, "counts": variant["counts"]}), flush=True)
