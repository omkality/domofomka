"""Разобрать сохранённые ответы без угадывания намерения и без объединения адресов."""

import argparse
import collections
import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from src.code_service.services.v2.normalize import legacy_house_matches
from tools.catalog.sqlite_catalog import SqliteCatalog
from tools.evaluation.quality_suite import address_fields, literal_words


def context_score(label, result, identity):
    """Исходные ограничения обязательны для всех вариантов; поздняя коррекция — целевой вариант."""
    options = [result] if result["status"] in {"found", "no_codes"} else result.get("candidates", [])

    def matches(option, constraints):
        """Проверить выполнение текущих ограничений адресного случая."""
        building = identity(option["building_id"]) if option.get("building_id") else None
        if not building and option.get("address"):
            fields = address_fields(option["address"])
            building = dict(fields, display=option["address"], lineage="", aliases=[])
        if not building:
            return False
        if constraints.get("house") and folded(building["house"]) != folded(constraints["house"]):
            return False
        names = [building["street"], *building.get("aliases", [])]
        if constraints.get("street"):
            if literal_words(constraints["street"]) not in [literal_words(re.sub(r"\([^()]*\)", "", n)) for n in names]:
                return False
        if constraints.get("street_tokens"):
            required = literal_words(" ".join(constraints["street_tokens"]))
            if not any(required <= literal_words(name) for name in names):
                return False
        if constraints.get("city") and not literal_words(constraints["city"]) <= literal_words(
            building["city"] + " " + building.get("lineage", "")
        ):
            return False
        return not constraints.get("kind") or address_fields(building["display"])["kind"] == constraints["kind"]

    literal_conflict = any(not matches(option, label.get("literal", {})) for option in options)
    target = label.get("observed_target")
    offered = any(matches(option, target) for option in options) if target else None
    return {
        "literal_conflict": literal_conflict,
        "target_offered": offered,
        "target_code_offered": bool(offered and result["status"] in {"found", "ambiguous"}) if target else None,
        "other_automatic_target": bool(target and result["status"] in {"found", "no_codes"} and not offered),
    }


def score_context_run(args):
    """Сравнить результаты вариантов контекста на фиксированных случаях."""
    args.output.mkdir(parents=True, exist_ok=False)
    frozen = json.loads((args.run / "frozen-input.json").read_text(encoding="utf8"))
    catalog = SqliteCatalog(str(args.catalog))
    counts = collections.defaultdict(collections.Counter)

    @lru_cache(maxsize=4096)
    def identity(identifier):
        """Получить устойчивую идентичность сравниваемого адреса."""
        row = catalog.building(identifier)
        if row:
            with catalog.connect() as db:
                row["aliases"] = [
                    r[0]
                    for r in db.execute(
                        r"""
                    SELECT
                      street_key
                    FROM
                      aliases
                    WHERE
                      building_id = ?
                    """,
                        (identifier,),
                    )
                ]
        return row

    with (args.output / "scores.jsonl").open("w", encoding="utf8") as stream:
        for line in (args.run / "results.jsonl").open(encoding="utf8"):
            record = json.loads(line)
            case = frozen["cases"][record["case_id"]]
            label = case.get("context_label")
            if label is None:
                continue
            score = context_score(label, record["result"], identity)
            counter = counts[record["variant"]]
            counter["labeled"] += 1
            counter["literal_conflicts"] += score["literal_conflict"]
            counter["with_observed_target"] += score["target_offered"] is not None
            counter["target_offered"] += bool(score["target_offered"])
            counter["target_code_offered"] += bool(score["target_code_offered"])
            counter["other_automatic_target"] += score["other_automatic_target"]
            stream.write(
                json.dumps(
                    {"case_id": record["case_id"], "variant": record["variant"], "score": score}, ensure_ascii=False
                )
                + "\n"
            )
    summary = {
        "counts": dict(counts),
        "input_sha256": frozen["sha256"],
        "note": "Цель из последующего уточнения отделена от буквальных ограничений исходного запроса. Другие допустимые варианты в меню не считаются ошибкой.",
        "review_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(summary, ensure_ascii=False))


def folded(value):
    """Нормализовать строку для сопоставления форм написания."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "").casefold().replace("ё", "е")).strip()


def literal_identity(row):
    """Построить буквальную идентичность адреса для разбора расхождений."""
    try:
        kind = address_fields(row["display"])["kind"]
    except (ValueError, KeyError):
        return None
    return tuple(folded(row.get(key)) for key in ("city", "street", "house", "region", "lineage")) + (kind,)


def classify(case, baseline, challenger, identity):
    """Отнести расхождение к проверяемой категории адресного конфликта."""
    reference = case.get("expected_address") or case.get("historical_suggestion_not_truth")
    if reference and ", дом " in reference and not legacy_house_matches(case["query"], reference):
        return "reference_house_conflict"
    if "backend_error" in {baseline["status"], challenger["status"]}:
        return "backend_error"

    def signature(value):
        """Получить устойчивое представление результата без непостоянных измерений."""
        return (
            value["status"],
            value.get("building_id"),
            value.get("address"),
            sorted(c["building_id"] for c in value.get("candidates", [])),
        )

    if signature(baseline) == signature(challenger):
        return "same_result_on_current_code"
    selected = challenger.get("building_id")
    if challenger["status"] == "found" and selected:
        candidate_ids = [c["building_id"] for c in baseline.get("candidates", [])]
        comparison_ids = candidate_ids or ([baseline["building_id"]] if baseline.get("building_id") else [])
        rows = [identity(bid) for bid in comparison_ids]
        target = identity(selected)
        if target and rows and all(rows):
            if target.get("guid") and all(row.get("guid") == target["guid"] for row in rows):
                return "same_verified_guid"
            key = literal_identity(target)
            if key and all(literal_identity(row) == key for row in rows):
                return "same_literal_address_unverified_ids"
        if selected in candidate_ids:
            return "existing_candidate_selected"
        if target and baseline.get("building_id"):
            original = identity(baseline["building_id"])
            if original:
                if original["house"] != target["house"]:
                    return "different_house"
                if folded(original["street"]) == folded(target["street"]):
                    return "different_locality_or_metadata"
                return "different_street_or_address"
        return "new_automatic_candidate"
    return "different_status_or_candidate_set"


def context(root, case):
    """Получить значимый адресный контекст для сравнения вариантов."""
    path = root / (case.get("dialog", "missing") + ".json")
    if not path.is_file():
        return {"available": False}
    messages = sorted(
        json.loads(path.read_text(encoding="utf8"))["messages"],
        key=lambda r: (r["date"], r.get("conversation_message_id", r.get("id", 0))),
    )
    indexes = [
        i for i, message in enumerate(messages) if not message.get("out") and message.get("text") == case["query"]
    ]
    windows = []
    for i in indexes[-3:]:
        windows.append(
            [
                {
                    "date": row["date"],
                    "out": row["out"],
                    "text": row.get("text", "")[:1500],
                    "truncated": len(row.get("text", "")) > 1500,
                }
                for row in messages[max(0, i - 2) : i + 5]
            ]
        )
    return {
        "available": True,
        "matching_occurrences": len(indexes),
        "windows_truncated": len(indexes) > 3,
        "windows": windows,
    }


def review(args):
    """Подготовить агрегированный отчёт о спорных результатах поиска."""
    args.output.mkdir(parents=True, exist_ok=False)
    frozen = json.loads((args.run / "frozen-input.json").read_text(encoding="utf8"))
    cases = frozen["cases"]
    groups = collections.defaultdict(dict)
    for line in (args.run / "results.jsonl").open(encoding="utf8"):
        row = json.loads(line)
        groups[row["case_id"]][row["variant"]] = row
    if len(groups) != len(cases):
        raise ValueError("Review needs a completed paired run")
    catalog = SqliteCatalog(str(args.catalog))

    @lru_cache(maxsize=8192)
    def identity(identifier):
        """Получить устойчивую идентичность сравниваемого адреса."""
        row = catalog.building(identifier)
        return (
            {key: row.get(key) for key in ("id", "guid", "display", "city", "street", "house", "region", "lineage")}
            if row
            else None
        )

    counts = collections.Counter()
    with (args.output / "classified.jsonl").open("w", encoding="utf8") as stream:
        for i, case in enumerate(cases):
            pair = groups[i]
            baseline, challenger = pair[args.baseline], pair[args.challenger]
            category = classify(case, baseline["result"], challenger["result"], identity)
            counts[category] += 1
            ids = set()
            for value in (baseline["result"], challenger["result"]):
                if value.get("building_id"):
                    ids.add(value["building_id"])
                ids.update(c["building_id"] for c in value.get("candidates", []))
            value = {
                "case_id": i,
                "case": case,
                "category": category,
                "review_status": "unreviewed",
                "baseline": baseline,
                "challenger": challenger,
                "identities": {bid: identity(bid) for bid in sorted(ids)},
            }
            if args.dialogs:
                value["context"] = context(args.dialogs, case)
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
    summary = {
        "cases": len(cases),
        "categories": dict(counts),
        "input_sha256": frozen["sha256"],
        "note": "Категории описывают расхождение, а не правильный ответ. Равенство текста не подтверждает физический дубль.",
        "review_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", default="catalog")
    parser.add_argument("--challenger", default="lexical.parser-AUTO-r1.0")
    parser.add_argument("--dialogs", type=Path)
    parser.add_argument("--context-scores", action="store_true")
    options = parser.parse_args()
    score_context_run(options) if options.context_scores else review(options)
