import hashlib
import json
import sqlite3

from src.search.localities import clean_structured_prefix, match, urban_city
from src.search.normalize import parse_query
from tools.build_catalog import SCHEMA, link_codes, relink
from tools.build_locality_facts import build_region

FACT = {
    "region": "50",
    "object_id": 4,
    "city_key": "дрожжино",
    "kind": "пгт.",
    "admin_words": ["видное", "дрожжино", "московская"],
    "municipal_words": ["дрожжино", "ленинский", "московская"],
}


def test_official_municipality_preserves_county_region_and_place_identity():
    facts = {("50", "дрожжино"): [FACT]}
    valid = urban_city("Московская область, Ленинский городской округ, рабочий поселок Дрожжино")
    assert match(valid, "дрожжино", "50", "дрожжино видное московская", facts)
    assert not match(
        urban_city("Тульская область, Ленинский городской округ, рабочий поселок Дрожжино"),
        "дрожжино",
        "50",
        "дрожжино видное московская",
        facts,
    )
    assert not match(
        urban_city("Московская область, Дмитровский городской округ, рабочий поселок Дрожжино"),
        "дрожжино",
        "50",
        "дрожжино видное московская",
        facts,
    )
    assert not match(valid, "дрожжино", "71", "дрожжино видное московская", facts)
    assert not match(valid, "дрожжино", "50", "дрожжино другое московская", facts)
    assert not match(
        valid, "дрожжино", "50", "дрожжино видное московская", {("50", "дрожжино"): [FACT, dict(FACT, object_id=5)]}
    )
    assert not match(
        valid, "дрожжино", "50", "дрожжино видное московская", {("50", "дрожжино"): [dict(FACT, kind="д.")]}
    )


def test_structured_query_removes_types_but_keeps_all_proper_names():
    query = parse_query(
        "Московская область, Ленинский городской округ, посёлок городского типа Дрожжино, Южная улица, 21"
    )
    assert query.words == "московская ленинский дрожжино южная"
    assert query.house == "21"
    assert query.kind == "ул"
    street = "улица Рабочий поселок Мельничный, дом 3"
    assert clean_structured_prefix(street, 0) == street
    assert "рабочий" in parse_query(street).words
    assert "городской округ" in parse_query("улица Ленинский городской округ, дом 3").words
    assert urban_city("Московская область, рабочий поселок Дрожжино")[0] == "дрожжино"
    assert urban_city("Рполье") is None


def test_registry_reads_both_hierarchies_without_house_or_room_xml(tmp_path):
    directory = tmp_path / "50"
    directory.mkdir()

    def xml(family, tag, rows):
        content = (
            "<ROOT>"
            + "".join(
                "<" + tag + " " + " ".join(f'{key}="{value}"' for key, value in row.items()) + "/>" for row in rows
            )
            + "</ROOT>"
        )
        (directory / ("AS_" + family + "_20261105.XML")).write_text(content, encoding="utf8")

    xml(
        "ADDR_OBJ",
        "OBJECT",
        [
            {"OBJECTID": oid, "NAME": name, "TYPENAME": kind, "LEVEL": level, "ISACTIVE": 1, "ISACTUAL": 1}
            for oid, name, kind, level in [
                (1, "Московская", "обл.", 1),
                (2, "Видное", "г.", 2),
                (3, "Ленинский", "г.о.", 3),
                (4, "Дрожжино", "пгт.", 6),
            ]
        ],
    )
    xml(
        "ADM_HIERARCHY",
        "ITEM",
        [{"OBJECTID": oid, "PARENTOBJID": parent, "ISACTIVE": 1} for oid, parent in [(1, 0), (2, 1), (4, 2)]],
    )
    xml("MUN_HIERARCHY", "ITEM", [{"OBJECTID": 4, "PATH": "1.3.4", "ISACTIVE": 1}])
    rows = build_region(tmp_path, "50")
    assert rows == [FACT]


def test_relink_candidate_merges_verified_copies_without_modifying_source(tmp_path):
    legacy = tmp_path / "legacy.db"
    cities = [
        "рабочий поселок Дрожжино",
        "Московская область, Ленинский городской округ, рабочий поселок Дрожжино",
        "Московская область, Дмитровский городской округ, рабочий поселок Дрожжино",
    ]
    with sqlite3.connect(legacy) as db:
        db.execute(
            "CREATE TABLE codes(id INTEGER,city TEXT,street_type TEXT,street TEXT,house TEXT,"
            "entrance TEXT,code_type TEXT,code TEXT)"
        )
        db.executemany(
            "INSERT INTO codes VALUES(?,?,?,?,?,?,?,?)",
            [(i + 1, city, "улица", "Южная", "21", "1", "source", "CODE") for i, city in enumerate(cities)],
        )
    source = tmp_path / "original.db"
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "gar:1",
                "1",
                "Дрожжино, ул. Южная, дом 21",
                "Дрожжино",
                "Южная",
                "21",
                "южная",
                "дрожжино",
                "50",
                "дрожжино видное московская",
            ),
        )
        stats = {
            "source_sha256": hashlib.sha256(legacy.read_bytes()).hexdigest(),
            "legacy_rows": 0,
            "linked_groups": 0,
            "unresolved_groups": 0,
            "ambiguous_groups": 0,
        }
        link_codes(db, legacy, stats)
        db.execute("INSERT INTO metadata VALUES(?,?)", ("build", json.dumps(stats)))
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    facts = tmp_path / "facts.jsonl"
    facts.write_text(json.dumps(FACT, ensure_ascii=False) + "\n", encoding="utf8")
    candidate = tmp_path / "candidate.db"
    relink(legacy, candidate, facts, source)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    with sqlite3.connect(candidate) as db:
        assert db.execute(
            "SELECT source_id FROM provenance WHERE building_id='gar:1' ORDER BY source_id"
        ).fetchall() == [(1,), (2,)]
        assert db.execute("SELECT DISTINCT resolution FROM provenance WHERE building_id='gar:1'").fetchall() == [
            ("gar_locality_alias",)
        ]
        assert db.execute("SELECT resolution FROM provenance WHERE source_id=3").fetchone() == ("unresolved",)
        assert db.execute("SELECT count(*) FROM codes WHERE building_id='gar:1'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM provenance").fetchone()[0] == 3
