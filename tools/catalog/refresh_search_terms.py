"""Обновить слова компактного каталога, сохраняя дома, коды и происхождение."""

import argparse
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from src.code_service.services.v2.normalize import address_words
from tools.catalog.refresh_aliases import refresh


def rebuild(source: Path, output: Path) -> dict:
    """Перестроить подготовленные поисковые данные отдельного каталога."""
    work = output.with_suffix(".building.db")
    if source.resolve() == output.resolve() or output.exists() or work.exists():
        raise FileExistsError("Use a fresh candidate; preserve the source")
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < source.stat().st_size + 1073741824:
        raise ValueError("Not enough space for an independent candidate")
    shutil.copyfile(source, work)
    with closing(sqlite3.connect(work)) as db:
        if db.execute("SELECT value FROM metadata WHERE key='format'").fetchone() != ("compact",):
            raise ValueError("Expected a compact build artifact")
        db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-131072;")
        changed = []
        for sid, city, street, words in db.execute("SELECT id,city_key,street_key,words FROM streets"):
            wanted = address_words(city + " " + street)
            if wanted != words:
                changed.append((wanted, sid))
        db.executemany("UPDATE streets SET words=? WHERE id=?", changed)
        db.execute("DELETE FROM search")
        db.execute("INSERT INTO search(id,words) SELECT id,words FROM streets")
        aliases = refresh(db)
        db.execute("INSERT OR REPLACE INTO metadata VALUES('search_terms_version','2')")
        db.commit()
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    work.replace(output)
    return {"changed_streets": len(changed), "alias_documents": aliases, "search_terms_version": 2}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(rebuild(args.catalog, args.output)), flush=True)
