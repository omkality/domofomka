"""Аудит упоминаний населённых пунктов во всех входящих текстах, без изменения поиска."""

import argparse
import concurrent.futures
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from tools.catalog.import_gar import records

ALIASES = {"мск": "Москва", "питер": "Санкт-Петербург", "спб": "Санкт-Петербург", "санктпетербург": "Санкт-Петербург"}
LOCALITY_MARKERS = {"г", "город", "д", "деревня", "с", "село", "п", "пос", "поселок", "пгт", "рп", "станица"}
STREET_MARKERS = {"ул", "улица", "проспект", "проезд", "шоссе", "бульвар", "переулок", "площадь", "наб", "набережная"}
URBAN_KINDS = {"г", "город", "пгт", "рп", "дп", "кп"}
EVIDENCE_ORDER = {
    "unmarked_other": 0,
    "street_or_locality": 1,
    "unmarked_urban": 2,
    "unmarked_city": 3,
    "city_alias": 4,
    "explicit_locality": 5,
}


def words(value: str) -> tuple[str, ...]:
    # Никаких stemming, исправлений опечаток или нормализатора проверяемого поиска.
    """Выделить нормализованные слова анализируемого сообщения."""
    return tuple(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")))


def kind_key(value: str) -> str:
    """Получить канонический ключ типа адресного объекта."""
    return "".join(words(value))


def query_key(value: str) -> str:
    """Сравнивать повторы текста, сохраняя дефисы, дроби, буквы и компоненты дома."""
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", value).strip()


def extract_region(directory: Path) -> dict:
    """Извлечь явно заданный регион из пользовательского текста."""
    files = list(directory.glob("AS_ADDR_OBJ_2*.XML"))
    if len(files) != 1:
        raise ValueError("Expected one address-object snapshot per region")
    path = files[0]
    places = []
    for row in records(path):
        if row.get("ISACTUAL") != "1" or row.get("ISACTIVE") != "1":
            continue
        level = int(row["LEVEL"])
        kind = row["TYPENAME"]
        if level in {5, 6} or (level in {1, 2} and kind_key(kind) in {"г", "город"}):
            places.append({"name": row["NAME"], "kind": kind, "level": level, "region": directory.name})
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"region": directory.name, "source": path.name, "source_sha256": digest, "places": places}


class LocalityMatcher:
    """Лексические признаки для аналитики; не геокодер и не выбор города пользователя."""

    def __init__(self, places: list[dict], street_names: set[str] | None = None):
        """Сохранить зависимости и настроить ресурсы экземпляра."""
        self.places = {}
        self.trie = {}
        self.street_names = {words(name) for name in street_names or set()}
        for row in places:
            key = words(row["name"])
            if not key:
                continue
            item = self.places.setdefault(key, {"names": set(), "regions": set(), "kinds": set()})
            item["names"].add(row["name"])
            item["regions"].add(row["region"])
            item["kinds"].add(kind_key(row["kind"]))
        for key in self.places:
            self._insert(key, key, False)
        for alias, canonical in ALIASES.items():
            key = words(canonical)
            if key in self.places:
                self._insert(words(alias), key, True)

    def _insert(self, spelling: tuple[str, ...], key: tuple[str, ...], alias: bool) -> None:
        """Добавить подготовленное название в индекс упоминаний местностей."""
        node = self.trie
        for token in spelling:
            node = node.setdefault(token, {})
        node[None] = (key, alias)

    def find(self, value: str) -> list[dict]:
        """Найти адресные упоминания в тексте с помощью подготовленного индекса."""
        normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
        spans = list(re.finditer(r"[^\W_]+", normalized))
        tokens = tuple(match[0] for match in spans)
        found = {}
        start = 0
        while start < len(tokens):
            node, best, end = self.trie, None, start
            while end < len(tokens) and tokens[end] in node:
                node = node[tokens[end]]
                end += 1
                if None in node:
                    best = (end, *node[None])
            if best is None:
                start += 1
                continue
            end, key, alias = best
            item = self.places[key]
            category = (
                "city"
                if item["kinds"] & {"г", "город"}
                else "urban_place" if item["kinds"] & URBAN_KINDS else "other_settlement"
            )
            prefix = tokens[start - 1] if start else ""
            # В ГАР есть числовые названия посёлков; «д. 3» обычно номер дома.
            if all(token.isdecimal() for token in key) and prefix not in LOCALITY_MARKERS - {"д", "с"}:
                start = end
                continue
            following = tokens[end] if end < len(tokens) else ""
            separator = normalized[spans[end - 1].end() : spans[end].start()] if end < len(tokens) else ""
            adjacent_street_type = following in STREET_MARKERS and not any(c in separator for c in ",;\n")
            if prefix in LOCALITY_MARKERS:
                evidence = "explicit_locality"
            elif prefix in STREET_MARKERS or (key in self.street_names and adjacent_street_type and not alias):
                evidence = "street_or_locality"
            elif alias:
                evidence = "city_alias"
            else:
                evidence = {
                    "city": "unmarked_city",
                    "urban_place": "unmarked_urban",
                    "other_settlement": "unmarked_other",
                }[category]
            if evidence == "unmarked_other":
                start = end
                continue
            match = {
                "key": " ".join(key),
                "name": min(item["names"]),
                "category": category,
                "regions": sorted(item["regions"]),
                "evidence": evidence,
                "street_name_overlap": key in self.street_names,
            }
            if key not in found or EVIDENCE_ORDER[evidence] > EVIDENCE_ORDER[found[key]["evidence"]]:
                found[key] = match
            start = end
        return list(found.values())


def mentions(matches: list[dict]) -> list[dict]:
    # Без маркера названия деревень часто совпадают со словами улицы или обычной речи.
    """Найти упоминания известных местностей в сообщении."""
    return [m for m in matches if m["category"] != "other_settlement" or m["evidence"] == "explicit_locality"]


def reply_kind(value: str) -> str:
    """Определить вид фактически отправленного ответа бота."""
    value = value.strip()
    if value == "Нет результатов":
        return "no_results"
    if "дом " in value.split("\n")[0] and "," in value.split("\n")[0]:
        return "address_response"
    return "other_reply"


def audit(source: Path, output: Path, matcher: LocalityMatcher) -> dict:
    """Проанализировать исходные данные и сохранить агрегированный отчёт."""
    if output.exists():
        raise FileExistsError("Preserve frozen audit results; choose a new output directory")
    output.mkdir(parents=True)
    counts, evidence = Counter(), Counter()
    places: defaultdict[str, dict] = defaultdict(
        lambda: {
            "messages": 0,
            "dialogs": set(),
            "queries": set(),
            "next_reply": Counter(),
            "evidence": Counter(),
            "without_digits": 0,
            "street_name_overlap_messages": 0,
        }
    )
    source_hash = hashlib.sha256()
    earliest, latest = None, None
    manifest = source / "conversations.json"
    manifest_data = json.loads(manifest.read_text(encoding="utf8")) if manifest.exists() else {}

    with (output / "incoming.jsonl").open("w", encoding="utf8") as stream:

        def finish(row: dict | None, outcome: str) -> None:
            """Завершить построение индекса названий местностей."""
            if row is None:
                return
            row["next_reply"] = outcome
            counts["next_reply:" + outcome] += 1
            for match in row["mentions"]:
                group = places[match["key"]]
                group["messages"] += 1
                group["dialogs"].add(row["dialog"])
                group["queries"].add(hashlib.sha256(query_key(row["query"]).encode()).hexdigest())
                group["next_reply"][outcome] += 1
                group["evidence"][match["evidence"]] += 1
                group["without_digits"] += not bool(re.search(r"\d", row["query"]))
                group["street_name_overlap_messages"] += match["street_name_overlap"]
                group["place"] = {k: match[k] for k in ("name", "category", "regions")}
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

        for path in sorted(source.glob("*.json")):
            if path.name == "conversations.json":
                continue
            raw = path.read_bytes()
            source_hash.update(path.name.encode() + b"\0" + hashlib.sha256(raw).digest())
            data = json.loads(raw)
            counts["dialogs"] += 1
            pending, seen = None, set()
            for index, message in enumerate(
                sorted(
                    data["messages"], key=lambda m: (m.get("date", 0), m.get("conversation_message_id", m.get("id", 0)))
                )
            ):
                counts["message_rows"] += 1
                identity = (
                    ("cmid", message["conversation_message_id"])
                    if message.get("conversation_message_id")
                    else (("id", message["id"]) if message.get("id") else ("row", index))
                )
                if identity in seen:
                    counts["duplicate_events"] += 1
                    continue
                seen.add(identity)
                text = message.get("text", "").strip()
                if message.get("out"):
                    finish(pending, reply_kind(text))
                    pending = None
                    continue
                finish(pending, "no_immediate_reply")
                pending = None
                if not text:
                    counts["incoming_nontext"] += 1
                    continue
                counts["incoming_text"] += 1
                timestamp = message.get("date", 0)
                earliest = min(earliest, timestamp) if earliest is not None else timestamp
                latest = max(latest, timestamp) if latest is not None else timestamp
                matches = matcher.find(text)
                selected = mentions(matches)
                counts["with_locality_mention" if selected else "without_locality_mention"] += 1
                counts["multiple_locality_names"] += len(selected) > 1
                for match in selected:
                    evidence[match["evidence"]] += 1
                pending = {
                    "dialog": data["dialog"],
                    "message_id": identity,
                    "date": timestamp,
                    "query": text,
                    "mentions": selected,
                }
            finish(pending, "no_immediate_reply")
    summary = {
        "schema_version": 1,
        "counts": dict(counts),
        "source_sha256": source_hash.hexdigest(),
        "conversation_list_snapshot_at": manifest_data.get("snapshot_at"),
        "incoming_date_range": [earliest, latest],
        "complete_manifest_dialog_count": (
            len(manifest_data.get("peers", [])) == counts["dialogs"] if manifest_data else None
        ),
        "evidence_counts": dict(evidence),
        "places": [
            dict(
                group["place"],
                messages=group["messages"],
                dialogs=len(group["dialogs"]),
                distinct_queries=len(group["queries"]),
                next_reply=dict(group["next_reply"]),
                evidence=dict(group["evidence"]),
                without_digits=group["without_digits"],
                street_name_overlap_messages=group["street_name_overlap_messages"],
            )
            for _, group in sorted(places.items(), key=lambda item: (-item[1]["messages"], item[0]))
        ],
        "private_files": ["incoming.jsonl"],
        "notes": [
            "Упоминание названия не устанавливает местоположение пользователя или правильный адрес.",
            "Без названия город не выводится из ответа прежнего бота или популярности.",
            "Несколько названий считаются отдельно; строки городов не образуют непересекающиеся группы.",
            "Следующий ответ — эвристика порядка сообщений, не подтверждённая разметка намерения.",
        ],
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    return summary


def main() -> None:
    """Аудит упоминаний населённых пунктов во всех входящих текстах, без изменения поиска."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("artifacts/private/vk-prod"))
    parser.add_argument("--gar", type=Path, default=Path("artifacts/gar_xml"))
    parser.add_argument("--catalog", type=Path, default=Path("artifacts/search/serving.source-fields-final.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--facts", type=Path, help="Повторить аудит на уже извлечённом снимке официальных названий")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Choose a new output directory")
    directories = sorted(p for p in args.gar.iterdir() if p.is_dir() and p.name.isdigit())
    if args.facts:
        facts = json.loads(args.facts.read_text(encoding="utf8"))
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
            facts = list(pool.map(extract_region, directories))
    with sqlite3.connect("file:" + args.catalog.resolve().as_posix() + "?mode=ro", uri=True) as db:
        street_names = {r[0] for r in db.execute("SELECT DISTINCT street_key FROM streets")}
    matcher = LocalityMatcher([p for region in facts for p in region["places"]], street_names)
    summary = audit(args.source, args.output, matcher)
    summary.update(
        locality_source_files=len(facts),
        locality_entries=sum(len(r["places"]) for r in facts),
        locality_facts_sha256=hashlib.sha256(
            json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    )
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    (args.output / "locality-facts.json").write_text(json.dumps(facts, ensure_ascii=False), encoding="utf8")
    print(
        json.dumps(
            {
                "counts": summary["counts"],
                "observed_place_names": len(summary["places"]),
                "top_urban_mentions": [
                    {k: row[k] for k in ("name", "messages", "dialogs")}
                    for row in summary["places"]
                    if row["category"] != "other_settlement"
                ][:12],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
