"""Aggregate actual failures; weak correction pairs are hypotheses, not truth labels."""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from src.code_service.v2.normalize import ordinals, parse_query


def audit(source: Path, output: Path):
    counts = Counter()
    failure_patterns = Counter()
    patterns = Counter()
    pairs = []
    success = []
    failures = []
    for path in source.glob("*.json"):
        if path.name == "conversations.json":
            continue
        data = json.loads(path.read_text(encoding="utf8"))
        counts["dialogs"] += 1
        messages = sorted(
            data["messages"], key=lambda m: (m.get("date", 0), m.get("conversation_message_id", m.get("id", 0)))
        )
        previous = None
        failed = []
        for message in messages:
            counts["messages"] += 1
            text = message.get("text", "").strip()
            if not message.get("out"):
                if not text:
                    counts["incoming_nontext"] += 1
                    previous = None
                    continue
                previous = message
                counts["incoming_text"] += 1
                patterns["with_entrance"] += bool(re.search(r"под[ъь]?езд|под\.", text, re.I))
                patterns["city_alias"] += bool(re.search(r"\b(мск|спб|питер)\b", text, re.I))
                patterns["written_ordinal"] += ordinals(text.lower()) != text.lower()
                patterns["numeric_street"] += bool(re.search(r"\b\d+[- ]?(я|й|ая|ый|ой)\b", text, re.I))
                patterns["punctuation"] += bool(re.search(r"[,;!?№]", text))
                patterns["polite_request"] += bool(re.search(r"пожалуйста|подскаж|нужен|нужна|код", text, re.I))
            elif previous:
                if text == "Нет результатов":
                    counts["explicit_no_results"] += 1
                    raw = previous["text"]
                    flags = {
                        "mentions_entrance": bool(re.search(r"под[ъь]?езд|под\.", raw, re.I)),
                        "mentions_apartment": bool(re.search(r"\b(?:кв|квартира|этаж)\b", raw, re.I)),
                        "written_ordinal": ordinals(raw.lower()) != raw.lower(),
                        "city_alias": bool(re.search(r"\b(?:мск|питер|спб)\b", raw, re.I)),
                        "no_house_number": not parse_query(raw).house,
                        "house_component_words": bool(re.search(r"корп|строен|\bстр\b", raw, re.I)),
                        "punctuation": bool(re.search(r"[,;!?№]", raw)),
                        "length_gt_500": len(raw) > 500,
                    }
                    for feature, present in flags.items():
                        failure_patterns[feature] += present
                    failures.append({"dialog": data["dialog"], "query": raw, "features": flags})
                    failed.append(previous)
                elif "дом " in text and ("," in text) and "Не удалось" not in text:
                    counts["address_responses"] += 1
                    address = text.split("\n")[0]
                    record = {
                        "dialog": data["dialog"],
                        "query": previous["text"],
                        "expected_address": address,
                        "evidence": "bot_address_response",
                        "split": "holdout"
                        if int(hashlib.sha256(data["dialog"].encode()).hexdigest()[:8], 16) % 5 == 0
                        else "train",
                    }
                    success.append(record)
                    good = parse_query(previous["text"])
                    for bad in failed:
                        if 0 <= previous["date"] - bad["date"] <= 1800 and bad["text"] != previous["text"]:
                            parsed = parse_query(bad["text"])
                            # No assert of intent: preserve candidates for review and separate metrics.
                            if parsed.house == good.house and good.house:
                                pairs.append(
                                    dict(
                                        record,
                                        query=bad["text"],
                                        correction=previous["text"],
                                        evidence="same_dialog_same_house_correction_hypothesis",
                                    )
                                )
                    failed = []
                previous = None
    output.mkdir(parents=True, exist_ok=True)
    for name, rows in [("successful-queries", success), ("correction-hypotheses", pairs), ("failed-queries", failures)]:
        (output / (name + ".jsonl")).write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf8"
        )
    summary = {
        "counts": dict(counts),
        "patterns": dict(patterns),
        "failed_message_features": dict(failure_patterns),
        "correction_hypotheses": len(pairs),
        "complete": False,
        "note": "Исходные переписки и тексты запросов остаются в игнорируемой папке artifacts. Ответы бота и пары исправлений — предварительная разметка, а не подтверждённая истина.",
    }
    manifest = source / "conversations.json"
    if manifest.exists():
        total = len(json.loads(manifest.read_text(encoding="utf8"))["peers"])
        summary.update(dialogs_total=total, complete=counts["dialogs"] == total)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, default=Path("artifacts/private/vk-prod"))
    p.add_argument("--output", type=Path, default=Path("artifacts/private/audit"))
    a = p.parse_args()
    audit(a.source, a.output)
