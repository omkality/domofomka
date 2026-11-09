"""Классификация непривязанных адресов без автоматической подмены города."""

import argparse
import collections
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def audit(path: Path, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    totals = collections.defaultdict(collections.Counter)
    with (
        closing(sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)) as db,
        (output / "review.jsonl").open("w", encoding="utf8") as review,
    ):
        rows = db.execute("""SELECT b.id,b.city_key,b.street_key,b.house,b.display,p.resolution,p.n FROM
            (SELECT building_id,resolution,count(*) n FROM provenance WHERE resolution<>'gar_exact' GROUP BY building_id,resolution) p
            JOIN buildings b ON b.id=p.building_id""")
        for i, (bid, city, street, house, display, resolution, count) in enumerate(rows):
            candidates = db.execute(
                "SELECT id,city_key,display,lineage FROM buildings WHERE street_key=? AND house=? AND guid IS NOT NULL",
                (street, house),
            ).fetchall()
            if resolution == "ambiguous":
                reason = "multiple_gar_identities"
            elif not house or not any(c.isdigit() for c in house):
                reason = "missing_or_non_numeric_house"
            elif candidates:
                exact = [c for c in candidates if c[1] == city]
                reason = "street_type_or_locality_conflict" if exact else "city_or_locality_conflict"
            elif db.execute(
                "SELECT 1 FROM buildings WHERE city_key=? AND street_key=? AND guid IS NOT NULL LIMIT 1", (city, street)
            ).fetchone():
                reason = "house_missing_on_known_street"
            else:
                reason = "street_or_locality_not_matched"
            totals[reason]["identities"] += 1
            totals[reason]["source_rows"] += count
            review.write(
                json.dumps(
                    {
                        "building_id": bid,
                        "address": display,
                        "reason": reason,
                        "source_rows": count,
                        "candidate_count": len(candidates),
                        "candidate_addresses": [c[2] for c in candidates[:5]],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            if i % 2000 == 0:
                print(json.dumps({"reviewed": i + 1}), flush=True)
    report = {
        "categories": dict(totals),
        "note": "Причины получены по точным ключам; кандидаты другого города не являются разрешёнными исправлениями.",
    }
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--catalog", type=Path, default=Path("artifacts/search/catalog.db"))
    p.add_argument("--output", type=Path, default=Path("artifacts/private/catalog-link-audit"))
    a = p.parse_args()
    print(json.dumps(audit(a.catalog, a.output)), flush=True)
