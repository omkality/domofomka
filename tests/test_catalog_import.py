import sqlite3

from src.search.engine import Catalog
from tools.build_catalog import build, relink


def test_street_types_never_merge_code_rows(tmp_path):
    legacy = tmp_path / "legacy.db"
    regions = tmp_path / "regions"
    regions.mkdir()
    with sqlite3.connect(legacy) as db:
        db.execute(
            "CREATE TABLE codes(id INTEGER,city TEXT,street_type TEXT,street TEXT,house TEXT,entrance "
            "TEXT,code_type TEXT,code TEXT)"
        )
        db.executemany(
            "INSERT INTO codes VALUES(?,?,?,?,?,?,?,?)",
            [
                (1, "Москва", "улица", "Мира", "1", "1", "test", "A"),
                (2, "Москва", "проспект", "Мира", "1", "1", "test", "B"),
            ],
        )
    with sqlite3.connect(regions / "gar-77.db") as db:
        db.execute(
            "CREATE TABLE buildings(id TEXT PRIMARY KEY,guid TEXT,display TEXT,city TEXT,street "
            "TEXT,house TEXT,street_key TEXT,city_key TEXT,region TEXT,lineage TEXT)"
        )
        for id, kind in [("a", "ул."), ("b", "пр-кт")]:
            db.execute(
                "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (id, id, f"Москва, {kind} Мира, дом 1", "Москва", "Мира", "1", "мира", "москва", "77", "москва"),
            )
        db.execute(
            "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("empty", "empty", "Москва, ул. Мира, дом 2", "Москва", "Мира", "2", "мира", "москва", "77", "москва"),
        )
    output = tmp_path / "catalog.db"
    build(legacy, regions, output)
    catalog = Catalog(str(output))
    street = catalog.resolve("Москва улица Мира 1")
    avenue = catalog.resolve("Москва проспект Мира 1")
    assert street["building_id"] == "a"
    assert avenue["building_id"] == "b"
    assert street["data"]["1"] == [["A", "test"]]
    assert avenue["data"]["1"] == [["B", "test"]]

    # Re-linking must restore its retrieval indexes, not only the code tables.
    with sqlite3.connect(output) as db:
        db.execute("DELETE FROM search")
    db.close()
    relink(legacy, output)
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT count(*) FROM search").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM alias_search").fetchone()[0] == 2

    from tools.compact_catalog import compact

    serving = tmp_path / "serving.db"
    compact(output, serving)
    small = Catalog(str(serving))
    assert small.resolve("Москва улица Мира 1")["data"] == street["data"]
    assert small.resolve("Москва проспект Мира 1")["data"] == avenue["data"]

    empty = small.resolve("Москва улица Мира 2")
    assert empty["status"] == "no_codes"
    assert empty["identity"] == "gar_address"
    assert empty["fias_id"] is None
    assert small.building(empty["building_id"])["display"] == empty["address"]

    from src.search.engine import documents

    assert list(documents(str(serving))) == list(documents(str(output)))
