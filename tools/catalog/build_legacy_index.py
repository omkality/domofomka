"""Создать отдельный индекс v1 без изменения исходной codes.db."""

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def build(source: Path, output: Path) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    working = output.with_suffix(".building.db")
    if working.exists():
        working.unlink()
    groups = {}
    with (
        closing(sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)) as origin,
        closing(sqlite3.connect(working)) as db,
    ):
        db.executescript("""PRAGMA journal_mode=OFF;PRAGMA synchronous=OFF;
        CREATE TABLE groups(id INTEGER PRIMARY KEY,city TEXT,kind TEXT,street TEXT,house TEXT,words TEXT);
        CREATE TABLE source_rows(group_id INTEGER,source_id INTEGER PRIMARY KEY);
        CREATE INDEX source_group ON source_rows(group_id);
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
        CREATE VIRTUAL TABLE legacy_search USING fts5(words,content='groups',content_rowid='id',tokenize='trigram');""")
        count = 0
        for row in origin.execute("SELECT id,city,street_type,street,house FROM codes ORDER BY id"):
            sid, *fields = row
            key = tuple(fields)
            if key not in groups:
                groups[key] = sid
                db.execute(
                    "INSERT INTO groups VALUES(?,?,?,?,?,?)",
                    (sid, *fields, fields[0].lower() + " " + fields[2].lower()),
                )
            db.execute("INSERT INTO source_rows VALUES(?,?)", (groups[key], sid))
            count += 1
        db.execute("INSERT INTO legacy_search(legacy_search) VALUES('rebuild')")
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        summary = {"sha256": digest, "source_rows": count, "groups": len(groups)}
        db.execute("INSERT INTO metadata VALUES(?,?)", ("source", json.dumps(summary)))
        db.commit()
    working.replace(output)
    return dict(summary, bytes=output.stat().st_size)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=Path("codes.db"))
    p.add_argument("--output", type=Path, default=Path("artifacts/search/legacy-index.db"))
    a = p.parse_args()
    print(json.dumps(build(a.source, a.output)), flush=True)
