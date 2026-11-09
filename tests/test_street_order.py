import hashlib
import json
import sqlite3
from contextlib import closing

from src.code_service.v2.engine import Catalog
from tools.catalog.build_catalog import SCHEMA, index_streets
from tools.catalog.reconcile_street_order import apply, mappings


def catalog(path, rows):
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA)
        for index, (identity, guid, city, street, house, kind) in enumerate(rows):
            db.execute(
                "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    guid,
                    f"{city}, {kind} {street}, дом {house}",
                    city,
                    street,
                    house,
                    street,
                    city,
                    "77",
                    city,
                ),
            )
            db.execute("INSERT INTO codes VALUES(?,?,?,?)", (identity, "1", "source", "CODE"))
            db.execute("INSERT INTO aliases VALUES(?,?,?,?)", (identity, city, street, house))
            db.execute("INSERT INTO provenance VALUES(?,?,?)", (index, identity, "gar_exact" if guid else "unresolved"))
        db.execute("INSERT INTO metadata VALUES('build',?)", (json.dumps({"normalization_schema": 4}),))
        index_streets(db)
        db.commit()


def test_unique_official_street_merges_codes_and_preserves_provenance_and_source(tmp_path):
    source, output = tmp_path / "source.db", tmp_path / "candidate.db"
    catalog(
        source,
        [
            ("gar:one", "one", "москва", "большая очаковская", "15", "улица"),
            ("legacy:one", None, "москва", "очаковская большая", "15", "улица"),
        ],
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with closing(sqlite3.connect(source)) as db:
        links, summary = mappings(db)
    assert summary["confirmed_groups"] == 1
    apply(source, output, links)
    with closing(sqlite3.connect(output)) as db:
        assert db.execute("SELECT count(*) FROM provenance").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM codes").fetchone()[0] == 1
        assert db.execute("SELECT DISTINCT building_id FROM provenance").fetchall() == [("gar:one",)]
        assert db.execute("SELECT count(*) FROM buildings WHERE guid IS NULL").fetchone()[0] == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert Catalog(str(output)).building("legacy:one")["id"] == "gar:one"


def test_reordering_never_changes_city_house_number_or_street_type(tmp_path):
    source = tmp_path / "source.db"
    catalog(
        source,
        [
            ("gar:one", "one", "москва", "1 текстильщиков", "8", "улица"),
            ("legacy:city", None, "другой город", "текстильщиков 1", "8", "улица"),
            ("legacy:house", None, "москва", "текстильщиков 1", "8к1", "улица"),
            ("legacy:number", None, "москва", "текстильщиков 11", "8", "улица"),
            ("legacy:type", None, "москва", "текстильщиков 1", "8", "шоссе"),
        ],
    )
    with closing(sqlite3.connect(source)) as db:
        links, _ = mappings(db)
    assert links == []


def test_two_official_streets_with_the_same_words_are_not_merged(tmp_path):
    source = tmp_path / "source.db"
    catalog(
        source,
        [
            ("gar:a", "a", "москва", "большая старая почтовая", "1", "улица"),
            ("gar:b", "b", "москва", "старая большая почтовая", "1", "улица"),
            ("legacy:a", None, "москва", "почтовая старая большая", "1", "улица"),
        ],
    )
    with closing(sqlite3.connect(source)) as db:
        links, _ = mappings(db)
    assert links == []
