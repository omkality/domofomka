"""Rebuild search text from structured names, without changing any address or code identity."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from src.code_service.v2.normalize import indexed_name_words
from tools.catalog.refresh_aliases import refresh


def repair(source: Path, output: Path) -> dict:
    if output.exists() or source.resolve() == output.resolve():
        raise FileExistsError("Preserve the frozen source and previous result")
    output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect("file:" + source.resolve().as_posix() + "?mode=ro", uri=True) as before:
        db = sqlite3.connect(output)
        try:
            before.backup(db)
            changed = 0
            cursor = db.execute("SELECT id,city_key,street_key,words FROM streets ORDER BY id")
            while rows := cursor.fetchmany(5000):
                edits = [
                    (indexed_name_words(city, street), sid)
                    for sid, city, street, words in rows
                    if words != indexed_name_words(city, street)
                ]
                db.executemany("UPDATE streets SET words=? WHERE id=?", edits)
                changed += len(edits)
            db.execute("DELETE FROM search")
            db.execute("INSERT INTO search(id,words) SELECT id,words FROM streets ORDER BY id")
            aliases = refresh(db)
            db.commit()
            db.execute("ANALYZE")
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            db.close()
    with output.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    result = {
        "changed_street_text": changed,
        "alias_rows": aliases,
        "sha256": digest,
        "identity_and_codes_changed": False,
        "bytes": output.stat().st_size,
    }
    output.with_suffix(".summary.json").write_text(json.dumps(result, indent=2), encoding="utf8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(repair(args.source, args.output)))
