import json
import sqlite3

import pytest

from src.code_service.v2.normalize import text
from tools.catalog.build_catalog import SCHEMA, index_streets
from tools.catalog.reconcile_source_fields import mappings
from tools.catalog.reconcile_street_order import apply


@pytest.fixture
def source_fields(tmp_path):
    source = tmp_path / "full.db"
    legacy = sqlite3.connect(":memory:")
    legacy.execute("CREATE TABLE codes(id INTEGER PRIMARY KEY,city,street_type,street,house,entrance,code_type,code)")
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO metadata VALUES('build',?)",
            (
                json.dumps(
                    {
                        "normalization_schema": 7,
                        "source_syntax_linked_groups": 17,
                        "source_syntax_linked_source_rows": 21,
                    }
                ),
            ),
        )
        values = [
            ("gar", "guid", "Тестовый", "Парковая Территория", "12к1"),
            ("opaque", None, "Тестовый", "Парковая", "Территория12к1"),
            ("shifted", None, "Тестовый, улица Парковая", "Территория", "12к1"),
            ("other-city", None, "Другой", "Парковая", "Территория12к1"),
            ("other-corpus", None, "Тестовый", "Парковая", "Территория12к2"),
        ]
        for i, (identifier, guid, city, street, house) in enumerate(values):
            db.execute(
                "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    guid,
                    f"{city}, улица {street}, дом {house}",
                    city,
                    street,
                    house.casefold(),
                    text(street),
                    text(city),
                    "77",
                    text(city),
                ),
            )
            db.execute("INSERT INTO codes VALUES(?,?,?,?)", (identifier, "1", "test", "KEEP-" + identifier))
            db.execute("INSERT INTO provenance VALUES(?,?,?)", (i, identifier, "unresolved"))
            db.execute("INSERT INTO aliases VALUES(?,?,?,?)", (identifier, text(city), text(street), house.casefold()))
            legacy.execute(
                "INSERT INTO codes VALUES(?,?,?,?,?,?,?,?)",
                (i, city, "улица", street, house, "1", "test", "KEEP-" + identifier),
            )
        index_streets(db)
    yield source, legacy
    legacy.close()


@pytest.mark.parametrize("target_has_codes", [True, False])
def test_field_moves_preserve_all_code_sources_and_old_building_ids(source_fields, tmp_path, target_has_codes):
    source, legacy = source_fields
    with sqlite3.connect(source) as db:
        if not target_has_codes:
            db.execute("DELETE FROM codes WHERE building_id='gar'")
            db.execute("DELETE FROM provenance WHERE building_id='gar'")
            legacy.execute("DELETE FROM codes WHERE id=0")
        links, summary = mappings(db, legacy)
    assert {link["source"] for link in links} == {"opaque", "shifted"}
    assert summary["source_rows"] == 2
    output = tmp_path / "aligned.db"
    apply(source, output, links, resolution="gar_source_field_alignment", schema_version=8)
    from tools.catalog.repair_house_identity import repair

    with pytest.raises(ValueError, match="before source field alignment"):
        repair(output, tmp_path / "not-used.db", tmp_path / "must-not-be-created.db")
    assert not (tmp_path / "must-not-be-created.db").exists()
    with sqlite3.connect(output) as db:
        expected_codes = {"KEEP-opaque", "KEEP-shifted"} | ({"KEEP-gar"} if target_has_codes else set())
        assert {r[0] for r in db.execute("SELECT code FROM codes WHERE building_id='gar'")} == expected_codes
        assert db.execute("SELECT count(*) FROM provenance").fetchone()[0] == (5 if target_has_codes else 4)
        redirects = json.loads(db.execute("SELECT value FROM metadata WHERE key='building_redirects'").fetchone()[0])
        assert redirects == {"opaque": "gar", "shifted": "gar"}
        stats = json.loads(db.execute("SELECT value FROM metadata WHERE key='build'").fetchone()[0])
        assert stats["source_syntax_linked_groups"] == 17
        assert stats["source_syntax_linked_source_rows"] == 21
        assert stats["source_fields_linked_groups"] == 2
        assert stats["source_fields_linked_source_rows"] == 2
        assert stats["street_documents"] == db.execute("SELECT count(*) FROM streets").fetchone()[0]
        assert db.execute("SELECT building_id FROM provenance WHERE source_id=3").fetchone()[0] == "other-city"
        assert db.execute("SELECT building_id FROM provenance WHERE source_id=4").fetchone()[0] == "other-corpus"


@pytest.mark.parametrize("conflict", ["second_guid", "raw_source_disagrees", "word_boundaries"])
def test_non_unique_or_conflicting_source_identity_is_never_relinked(source_fields, conflict):
    source, legacy = source_fields
    with sqlite3.connect(source) as db:
        if conflict == "second_guid":
            db.execute(
                "INSERT INTO buildings SELECT 'second','another-guid',display,city,street,house,"
                "street_key,city_key,region,lineage FROM buildings WHERE id='gar'"
            )
        elif conflict == "word_boundaries":
            db.execute(
                "INSERT INTO buildings SELECT 'second','another-guid',display,city,'ПарковаяТерритория',house,"
                "'парковаятерритория',city_key,region,lineage FROM buildings WHERE id='gar'"
            )
            index_streets(db)
        else:
            legacy.execute("UPDATE codes SET house='Территория12к2' WHERE id IN (1,2)")
        links, _ = mappings(db, legacy)
    assert not links


def add_repeated_fields(source, legacy, *, footer_city="Тестовый", footer_kind="улица", raw_house="12к1"):
    street = f"Парковая Территория {footer_kind},{footer_city}"
    with sqlite3.connect(source) as db:
        db.execute(
            "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "repeated",
                None,
                f"Тестовый, улица {street}, дом 12к1",
                "Тестовый",
                street,
                "12к1",
                text(street),
                "тестовый",
                "77",
                "тестовый",
            ),
        )
        db.execute("INSERT INTO codes VALUES('repeated','2','test','KEEP-repeated')")
        db.execute("INSERT INTO provenance VALUES(20,'repeated','unresolved')")
        db.execute("INSERT INTO aliases VALUES('repeated','тестовый',?,'12к1')", (text(street),))
        index_streets(db)
    legacy.execute(
        "INSERT INTO codes VALUES(20,'Тестовый','улица',?,?,'2','test','KEEP-repeated')", (street, raw_house)
    )


def test_repeated_city_and_trailing_type_need_the_same_unique_gar_house(source_fields, tmp_path):
    source, legacy = source_fields
    add_repeated_fields(source, legacy)
    with sqlite3.connect(source) as db:
        links, _ = mappings(db, legacy)
    assert next(link for link in links if link["source"] == "repeated")["target"] == "gar"
    target = tmp_path / "aligned-repeated.db"
    apply(source, target, links, resolution="gar_source_field_alignment", schema_version=8)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT building_id FROM provenance WHERE source_id=20").fetchone()[0] == "gar"
        assert db.execute("SELECT building_id FROM codes WHERE code='KEEP-repeated'").fetchone()[0] == "gar"
        redirects = json.loads(db.execute("SELECT value FROM metadata WHERE key='building_redirects'").fetchone()[0])
        assert redirects["repeated"] == "gar"


@pytest.mark.parametrize("conflict", ["city", "kind", "source_house", "literal_street", "second_guid"])
def test_repeated_fields_do_not_erase_literal_names_or_other_address_components(source_fields, conflict):
    source, legacy = source_fields
    add_repeated_fields(
        source,
        legacy,
        footer_city="Другой" if conflict == "city" else "Тестовый",
        footer_kind="проезд" if conflict == "kind" else "улица",
        raw_house="12к2" if conflict == "source_house" else "12к1",
    )
    with sqlite3.connect(source) as db:
        if conflict == "literal_street":
            db.execute(
                "INSERT INTO buildings SELECT 'literal','guid-literal',display,city,street,'99',street_key,"
                "city_key,region,lineage FROM buildings WHERE id='repeated'"
            )
        elif conflict == "second_guid":
            db.execute(
                "INSERT INTO buildings SELECT 'second','guid-second',display,city,street,house,street_key,"
                "city_key,region,lineage FROM buildings WHERE id='gar'"
            )
        index_streets(db)
        links, _ = mappings(db, legacy)
    assert "repeated" not in {link["source"] for link in links}
