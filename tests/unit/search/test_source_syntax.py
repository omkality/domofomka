import hashlib
import json
import sqlite3

from src.code_service.services.v2.normalize import text
from tools.catalog.build_catalog import SCHEMA, index_streets
from tools.catalog.build_region_facts import region_facts
from tools.catalog.reconcile_source_syntax import explicit_region_city, mappings, region_forms
from tools.catalog.reconcile_street_order import apply
from tools.catalog.sqlite_catalog import SqliteCatalog

FACTS = [{"region": "16", "name": "Татарстан", "kind": "респ."}]


def catalog(path, rows):
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for index, (identity, guid, city, street, house, kind, region) in enumerate(rows):
            db.execute(
                "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    guid,
                    f"{city}, {kind} {street}, дом {house}",
                    city,
                    street,
                    house,
                    text(street),
                    text(city),
                    region,
                    text(city),
                ),
            )
            db.execute("INSERT INTO codes VALUES(?,?,?,?)", (identity, "1", "test", "CODE-" + identity))
            db.execute("INSERT INTO aliases VALUES(?,?,?,?)", (identity, text(city), text(street), house))
            db.execute(
                "INSERT INTO provenance VALUES(?,?,?)", (index, identity, "unresolved" if not guid else "gar_exact")
            )
        db.execute("INSERT INTO metadata VALUES('build',?)", (json.dumps({"normalization_schema": 5}),))
        index_streets(db)


def test_embedded_type_merges_only_reviewed_identity_and_preserves_old_redirects(tmp_path):
    source, output = tmp_path / "source.db", tmp_path / "candidate.db"
    catalog(
        source,
        [
            ("gar:one", "one", "Москва", "Мира", "180", "проспект", "77"),
            ("legacy:one", None, "Москва", "Проспект Мира", "180", "улица", ""),
        ],
    )
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO metadata VALUES('building_redirects',?)", (json.dumps({"older": "legacy:one"}),))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with sqlite3.connect(source) as db:
        links, summary = mappings(db, FACTS)
    assert summary["reasons"] == {"embedded_type": 1}
    apply(source, output, links, resolution="gar_source_syntax", schema_version=6)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT count(*) FROM codes WHERE building_id='gar:one'").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM provenance").fetchone()[0] == 2
        assert (
            db.execute("SELECT resolution FROM provenance WHERE source_id=1").fetchone()[0]
            == "gar_source_syntax_embedded_type"
        )
    assert SqliteCatalog(str(output)).building("older")["id"] == "gar:one"


def test_region_root_restricts_identical_cities_in_different_regions(tmp_path):
    source = tmp_path / "source.db"
    catalog(
        source,
        [
            ("gar:tatarstan", "one", "Казань", "Пржевальского", "2", "улица", "16"),
            ("gar:other", "two", "Казань", "Пржевальского", "2", "улица", "50"),
            ("legacy:one", None, "Республика Татарстан, Казань", "Пржевальского", "2", "улица", ""),
        ],
    )
    with sqlite3.connect(source) as db:
        links, _ = mappings(db, FACTS)
    assert [(r["source"], r["target"]) for r in links] == [("legacy:one", "gar:tatarstan")]


def test_wrong_region_unmatched_components_and_conflicting_types_are_not_dropped(tmp_path):
    source = tmp_path / "source.db"
    catalog(
        source,
        [
            ("gar:one", "one", "Казань", "Мира", "180", "проспект", "50"),
            ("gar:moscow", "two", "Москва", "Мира", "180", "проспект", "77"),
            ("legacy:region", None, "Республика Татарстан, Казань", "Мира", "180", "проспект", ""),
            ("legacy:house", None, "Москва", "Проспект Мира", "180к1", "улица", ""),
            ("legacy:type", None, "Москва", "Проспект Мира", "180", "шоссе", ""),
            ("legacy:city", None, "Другой город", "Проспект Мира", "180", "улица", ""),
            ("legacy:district", None, "Республика Татарстан, район Неверный, Казань", "Мира", "180", "проспект", ""),
        ],
    )
    with sqlite3.connect(source) as db:
        links, _ = mappings(db, FACTS)
    assert links == []


def test_literal_street_name_has_priority_over_embedded_type_interpretation(tmp_path):
    source = tmp_path / "source.db"
    catalog(
        source,
        [
            ("gar:literal", "literal", "Мурино", "Шоссе в Лаврики", "59", "улица", "47"),
            ("gar:highway", "highway", "Мурино", "в Лаврики", "59", "шоссе", "47"),
            ("legacy:one", None, "Мурино", "Шоссе в Лаврики", "59", "улица", ""),
        ],
    )
    with sqlite3.connect(source) as db:
        links, _ = mappings(db, FACTS)
    assert links == []


def test_non_unique_official_house_is_not_selected(tmp_path):
    source = tmp_path / "source.db"
    catalog(
        source,
        [
            ("gar:a", "a", "Москва", "Мира", "180", "проспект", "77"),
            ("gar:b", "b", "Москва", "Мира", "180", "проспект", "77"),
            ("legacy:a", None, "Москва", "Проспект Мира", "180", "улица", ""),
        ],
    )
    with sqlite3.connect(source) as db:
        assert mappings(db, FACTS)[0] == []


def test_missing_literal_house_does_not_allow_switching_to_another_street(tmp_path):
    source = tmp_path / "source.db"
    catalog(
        source,
        [
            ("gar:literal", "literal", "Мурино", "Шоссе в Лаврики", "59", "улица", "47"),
            ("gar:highway", "highway", "Мурино", "в Лаврики", "61", "шоссе", "47"),
            ("legacy:one", None, "Мурино", "Шоссе в Лаврики", "61", "улица", ""),
        ],
    )
    with sqlite3.connect(source) as db:
        links, summary = mappings(db, FACTS)
    assert links == []
    assert summary["skipped"]["literal_street_without_house"] == 1


def test_explicit_region_syntax_requires_official_name_and_retains_other_constraints():
    forms = region_forms(FACTS)
    assert explicit_region_city("Россия, Республика Татарстан, г. Казань", forms) == ("16", "казань")
    assert explicit_region_city("Татарстан Республика, Казань", forms) == ("16", "казань")
    assert explicit_region_city("Татарстан, Казань", forms) is None
    assert explicit_region_city("Республика Татарстан, Вахитовский район, Казань", forms) is None
    assert explicit_region_city("Республика Другая, Казань", forms) is None


def test_region_facts_uses_only_current_root_and_records_missing_roots(tmp_path):
    directory = tmp_path / "16"
    directory.mkdir()
    path = directory / "AS_ADDR_OBJ_20261102_TEST.XML"
    path.write_text(
        """<ADDRESSOBJECTS>
        <OBJECT OBJECTID="1" OBJECTGUID="old" LEVEL="1" NAME="Старое" TYPENAME="Респ" ISACTUAL="0" ISACTIVE="0" />
        <OBJECT OBJECTID="2" OBJECTGUID="current" LEVEL="1" NAME="Татарстан" TYPENAME="респ."
            ISACTUAL="1" ISACTIVE="1" />
        <OBJECT OBJECTID="3" OBJECTGUID="city" LEVEL="5" NAME="Казань" TYPENAME="г." ISACTUAL="1" ISACTIVE="1" />
        </ADDRESSOBJECTS>""",
        encoding="utf-8",
    )
    row = region_facts(directory)
    assert (row["region"], row["name"], row["guid"]) == ("16", "Татарстан", "current")
    path.write_text("<ADDRESSOBJECTS></ADDRESSOBJECTS>", encoding="utf-8")
    assert region_facts(directory)["root_count"] == 0
