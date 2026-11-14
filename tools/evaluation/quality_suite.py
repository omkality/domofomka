"""Независимые от выдачи поисковика контрольные адреса и очередь разметки истории."""

import argparse
import collections
import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tools.catalog.async_reference import AsyncReferenceReader
from tools.catalog.sqlite_catalog import SqliteCatalog


def prepare(catalog: Path, output: Path) -> dict:
    """Подготовить детерминированные входные случаи проверки качества."""
    output.mkdir(parents=True, exist_ok=True)
    if (output / "controlled.jsonl").exists():
        raise FileExistsError("Use a new output directory to preserve the frozen controls")
    chosen = []
    cities = collections.Counter()
    with closing(sqlite3.connect(f"file:{catalog.resolve().as_posix()}?mode=ro", uri=True)) as db:
        rows = db.execute(r"""
            SELECT
              b.id,
              b.display,
              b.city,
              b.street,
              b.house,
              b.city_key,
              b.street_key
            FROM
              buildings AS b
            INNER JOIN
              (
                SELECT DISTINCT
                  building_id
                FROM
                  codes
              ) AS c
              ON c.building_id = b.id
            WHERE
              b.guid IS NOT NULL
            ORDER BY
              b.id
            """)
        for bid, display, city, street, house, ck, sk in rows:
            if cities[city] >= 2:
                continue
            identities = db.execute(
                r"""
                SELECT
                  count(DISTINCT id)
                FROM
                  buildings
                WHERE
                  city_key = ?
                  AND street_key = ?
                  AND house = ?
                  AND guid IS NOT NULL
                """,
                (ck, sk, house),
            ).fetchone()[0]
            if identities != 1:
                continue
            chosen.append((bid, display, city, street, house))
            cities[city] += 1
            if len(chosen) == 30:
                break
    cases = []
    for bid, display, city, street, house in chosen:
        variants = [
            display,
            display.lower(),
            display.upper(),
            display + " подъезд 2",
            display + " квартира 12",
            "Пожалуйста, " + display,
            display.replace(",", ";"),
            f"{city}, {street}, дом {house}",
            f"город {city}, {street}, дом {house}",
            f"{city} {street} {house}",
        ]
        for i, q in enumerate(variants):
            cases.append(
                {
                    "id": f"{bid}:{i}",
                    "query": q,
                    "expected_building_id": bid,
                    "evidence": "controlled_GAR_identity",
                    "address": display,
                }
            )
    (output / "controlled.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in cases), encoding="utf8"
    )
    return {"controlled_queries": len(cases), "anchor_identities": len(chosen), "cities": len(cities)}


def evaluate(catalog: Path, output: Path) -> dict:
    """Выполнить контрольный запрос и собрать его результат для сравнения."""
    engine = SqliteCatalog(str(catalog))
    counts = collections.Counter()
    bad = []
    for line in (output / "controlled.jsonl").read_text(encoding="utf8").splitlines():
        case = json.loads(line)
        r = engine.resolve(case["query"])
        matched = r.get("building_id") == case["expected_building_id"]
        offered = any(c["building_id"] == case["expected_building_id"] for c in r.get("candidates", []))
        counts["queries"] += 1
        counts["automatic"] += matched
        counts["offered"] += offered
        counts["wrong_automatic"] += r["status"] == "found" and not matched
        if not (matched or offered):
            bad.append(dict(case, status=r["status"], actual=r.get("address")))
    (output / "controlled-disagreements.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in bad), encoding="utf8"
    )
    report = dict(
        counts,
        missing=len(bad),
        note="Контрольные варианты известных адресов ГАР; это не измерение точности на реальных ошибках пользователей.",
    )
    (output / "controlled-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    return report


def review_queue(corpus: Path, output: Path) -> dict:
    """Сохранить спорные расхождения для отдельного рассмотрения."""
    if (output / "history-review.jsonl").exists():
        raise FileExistsError("Do not overwrite existing review decisions")
    source = []
    for filename in ("successful-queries", "correction-hypotheses"):
        source.extend(
            json.loads(line) for line in (corpus / (filename + ".jsonl")).read_text(encoding="utf8").splitlines()
        )
    source.sort(key=lambda r: hashlib.sha256(r["query"].encode()).hexdigest())
    seen = set()
    rows = []
    for record in source:
        q = record["query"]
        if q in seen or len(q) > 160:
            continue
        seen.add(q)
        rows.append(
            {
                "id": hashlib.sha256(q.encode()).hexdigest()[:16],
                "query": q,
                "historical_suggestion_not_truth": record["expected_address"],
                "evidence": record["evidence"],
                "review_status": "unreviewed",
                "expected_address": None,
                "review_reason": None,
            }
        )
        if len(rows) == 300:
            break
    output.mkdir(parents=True, exist_ok=True)
    (output / "history-review.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf8"
    )
    return {"history_review_items": len(rows), "verified_labels": 0}


def literal_words(value: str) -> set[str]:
    """Выделить значимые буквальные слова ожидаемого адреса."""
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = re.sub(r"\b(\d+)[- ]?(?:я|й|ая|ый|ой)\b", r"\1", value)
    return set(re.findall(r"[а-яa-z0-9]+", value))


def address_fields(address: str) -> dict:
    """Разделить адрес на поля для проверки идентичности результата."""
    prefix, house = address.rsplit(", дом ", 1)
    markers = list(
        re.finditer(
            r", (улица|ул\.|проспект|пр-кт|проезд|пр-д|переулок|пер\.|шоссе|ш\.|бульвар|б-р|набережная|наб\.|аллея|ал\.|микрорайон|мкр\.|квартал|площадь|пл\.|тупик|туп\.) ",
            prefix,
        )
    )
    if not markers:
        raise ValueError("Unrecognized address type: " + address)
    m = markers[-1]
    kinds = {
        "ул.": "улица",
        "пр-кт": "проспект",
        "пр-д": "проезд",
        "пер.": "переулок",
        "ш.": "шоссе",
        "б-р": "бульвар",
        "наб.": "набережная",
        "ал.": "аллея",
        "мкр.": "микрорайон",
        "туп.": "тупик",
        "пл.": "площадь",
    }
    return {
        "city": prefix[: m.start()],
        "street": prefix[m.end() :],
        "house": house,
        "lineage": prefix[: m.start()],
        "kind": kinds.get(m[1], m[1]),
    }


def review_evaluate(catalog: Path, output: Path) -> dict:
    """Проверить запрос с учётом отдельно рассмотренных конфликтов."""
    import asyncio

    from src.code_service.config import settings
    from src.code_service.services.v2 import search

    settings.legacy_index_path = "artifacts/search/legacy-index.db"
    settings.db_name = "codes.db"
    settings.opensearch_url = None
    settings.search_cache_ttl_seconds = 0
    engine = SqliteCatalog(str(catalog))

    async def lookup_reference(query: str) -> dict:
        """Временно подставить офлайн-эталон только внутри инструмента оценки."""

        async def selected_catalog() -> AsyncReferenceReader:
            """Вернуть явно выбранный инструментом файловый эталон."""
            return AsyncReferenceReader(engine)

        with patch.object(search, "get_catalog", selected_catalog):
            return await search.lookup(query)

    counts = collections.Counter()
    details = []
    rows = [json.loads(line) for line in (output / "history-review.jsonl").read_text(encoding="utf8").splitlines()]
    for case in rows:
        if case["review_status"] in {"unreviewed", "unresolved_intent"}:
            counts["not_scored"] += 1
            continue
        result = asyncio.run(lookup_reference(case["query"]))
        counts["scored"] += 1
        counts["status_" + result["status"]] += 1
        checks = []
        for option in [result] if result["status"] in {"found", "no_codes"} else result.get("candidates", []):
            if option.get("building_id"):
                building = engine.building(option["building_id"])
                with engine.connect() as db:
                    streets = [
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
                            (building["id"],),
                        )
                    ]
            else:
                building = address_fields(option["address"])
                streets = []
            expected = literal_words(case["expected_street"])
            street_ok = any(
                expected == literal_words(re.sub(r"\([^()]*\)", "", s)) for s in [building["street"], *streets]
            )
            house_ok = building["house"].casefold().replace("ё", "е") == case["expected_house"]
            city_ok = not case["expected_city"] or literal_words(case["expected_city"]) <= literal_words(
                building["city"] + " " + building["lineage"]
            )
            if case.get("expected_locality"):
                city_ok = city_ok and literal_words(case["expected_locality"]) <= literal_words(
                    building["lineage"] + " " + building["city"]
                )
            actual_kind = address_fields(building.get("display", option["address"]))["kind"]
            type_ok = not case.get("expected_street_type") or case["expected_street_type"] == actual_kind
            checks.append({"house": house_ok, "street": street_ok, "city": city_ok, "type": type_ok})
        safe = all(all(check.values()) for check in checks)
        counts["automatic_literal_conflicts"] += result["status"] == "found" and not safe
        counts["choice_literal_conflicts"] += result["status"] == "ambiguous" and not safe
        if checks and not safe:
            details.append(
                {"case": case, "status": result["status"], "actual": result.get("address"), "checks": checks}
            )
    report = dict(
        counts,
        reviewed=len(rows),
        without_explicit_city=sum(not case.get("expected_city") for case in rows),
        note="Проверены буквальные ограничения размеченных запросов. Если город не указан, правильность угаданного города не оценивается. Неясное намерение исключено из оценки.",
    )
    (output / "review-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    (output / "review-conflicts.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in details), encoding="utf8"
    )
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["prepare", "evaluate", "review", "review-evaluate"])
    p.add_argument("--catalog", type=Path, default=Path("artifacts/search/catalog.db"))
    p.add_argument("--output", type=Path, default=Path("artifacts/private/quality"))
    p.add_argument("--corpus", type=Path, default=Path("artifacts/private/audit"))
    a = p.parse_args()
    if a.command == "review-evaluate":
        print(json.dumps(review_evaluate(a.catalog, a.output)), flush=True)
        raise SystemExit(0)
    print(
        json.dumps(
            review_queue(a.corpus, a.output) if a.command == "review" else globals()[a.command](a.catalog, a.output)
        ),
        flush=True,
    )
