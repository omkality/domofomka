import hashlib
import json
import sqlite3

import pytest

from tools.catalog.build_catalog import SCHEMA, index_streets
from tools.catalog.repair_house_identity import repair
from tools.catalog.sqlite_catalog import SqliteCatalog


@pytest.mark.parametrize(
    ("houses", "collapsed"),
    [(("24", "2-4"), "24"), (("71к4Г", "71к4Г-Д"), "71к4г"), (("10", "10-Д"), "10")],
)
@pytest.mark.parametrize("representative_index", [0, 1])
@pytest.mark.parametrize("official", [False, True])
def test_repair_splits_source_codes_and_keeps_the_old_physical_identity(
    tmp_path, houses, collapsed, representative_index, official
):
    representative = houses[representative_index]
    legacy, previous, output = (
        tmp_path / "legacy.db",
        tmp_path / "previous.db",
        tmp_path / "repaired.db",
    )
    with sqlite3.connect(legacy) as db:
        db.execute(
            "CREATE TABLE codes(id INTEGER,city TEXT,street_type TEXT,street TEXT,house TEXT,"
            "entrance TEXT,code_type TEXT,code TEXT)"
        )
        db.executemany(
            "INSERT INTO codes VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    1,
                    "Санкт-Петербург",
                    "улица",
                    "Гражданская",
                    houses[0],
                    "1",
                    "test",
                    "FLAT",
                ),
                (
                    2,
                    "Санкт-Петербург",
                    "улица",
                    "Гражданская",
                    houses[0],
                    "1",
                    "test",
                    "COMMON",
                ),
                (
                    3,
                    "Санкт-Петербург",
                    "улица",
                    "Гражданская",
                    houses[1],
                    "1",
                    "test",
                    "RANGE",
                ),
                (
                    4,
                    "Санкт-Петербург",
                    "улица",
                    "Гражданская",
                    houses[1],
                    "1",
                    "test",
                    "COMMON",
                ),
            ],
        )
    source_hash = hashlib.sha256(legacy.read_bytes()).hexdigest()
    legacy_key = "\0".join(["санкт петербург", "гражданская", collapsed, "ул"]).encode()
    old_id = "legacy:" + hashlib.sha256(legacy_key).hexdigest()[:24]
    if official:
        old_id = "gar:physical"
    with sqlite3.connect(previous) as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                old_id,
                "physical" if official else None,
                "Санкт-Петербург, улица Гражданская, дом " + representative,
                "Санкт-Петербург",
                "Гражданская",
                collapsed,
                "гражданская",
                "санкт петербург",
                "78",
                "санкт петербург",
            ),
        )
        db.executemany(
            "INSERT INTO codes VALUES(?,?,?,?)",
            [(old_id, "1", "test", c) for c in ["FLAT", "RANGE", "COMMON"]],
        )
        db.executemany(
            "INSERT INTO provenance VALUES(?,?,?)",
            [(i, old_id, "unresolved") for i in range(1, 5)],
        )
        db.execute(
            "INSERT INTO aliases VALUES(?,?,?,?)",
            (old_id, "санкт петербург", "гражданская", collapsed),
        )
        db.execute(
            "INSERT INTO metadata VALUES('build',?)",
            (json.dumps({"source_sha256": source_hash}),),
        )
        index_streets(db)
    before_hash = hashlib.sha256(previous.read_bytes()).hexdigest()
    report = repair(previous, legacy, output)
    assert report["source_rows"] == 4
    assert report["source_house_conflicts"] == 0
    assert report["moved_source_rows"] == 2
    engine = SqliteCatalog(str(output))
    for house, code in [(houses[0], "FLAT"), (houses[1], "RANGE")]:
        result = engine.resolve("Санкт-Петербург Гражданская " + house)
        assert result["status"] == "found"
        assert result["address"].endswith(", дом " + house)
        assert {value for value, _ in result["data"]["1"]} == {code, "COMMON"}
    assert engine.building(old_id)["display"].endswith(", дом " + representative)
    assert hashlib.sha256(previous.read_bytes()).hexdigest() == before_hash
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == source_hash
