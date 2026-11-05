"""Build the source-address lookup without changing building identities or code rows."""

import argparse
import sqlite3
from pathlib import Path

from src.search.normalize import address_words


def refresh(db: sqlite3.Connection) -> int:
    db.execute("DROP TABLE IF EXISTS alias_search")
    db.execute("CREATE VIRTUAL TABLE alias_search USING fts5(building_id UNINDEXED,house UNINDEXED,words)")
    count = 0
    for building, city, street, house in db.execute("SELECT * FROM aliases"):
        db.execute("INSERT INTO alias_search VALUES(?,?,?)", (building, house, address_words(city + " " + street)))
        count += 1
    return count


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("catalog", type=Path)
    args = p.parse_args()
    with sqlite3.connect(args.catalog) as connection:
        print({"aliases": refresh(connection)})
