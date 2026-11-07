import hashlib
import sqlite3

import pytest

from src.search.engine import Catalog
from src.search.normalize import address_words, parse_query
from tools.build_catalog import SCHEMA, index_streets
from tools.refresh_search_terms import rebuild


@pytest.mark.parametrize("prefix", ["", "Российская Федерация, ", "Российская Федерация "])
def test_russian_street_is_preserved_after_country_prefix(prefix):
    query = parse_query(prefix + "Уфа, улица Российская, дом 43/7")
    assert (query.words, query.house, query.kind) == ("уфа российская", "43/7", "ул")
    assert parse_query("Российская 153").words == "российская"


def test_search_terms_rebuild_keeps_codes_houses_and_source_immutable(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO metadata VALUES('format','compact')")
        for bid, street, number in [("russian", "Российская", "153"), ("other", "Рязанская", "153")]:
            db.execute(
                "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    bid,
                    bid,
                    f"Уфа, улица {street}, дом {number}",
                    "Уфа",
                    street,
                    number,
                    street.lower(),
                    "уфа",
                    "02",
                    "уфа",
                ),
            )
            db.execute("INSERT INTO codes VALUES(?,?,?,?)", (bid, "1", "test", "CODE-" + bid))
            db.execute("INSERT INTO aliases VALUES(?,?,?,?)", (bid, "уфа", street.lower(), number))
        index_streets(db)
        db.execute("ALTER TABLE streets ADD COLUMN has_codes INTEGER DEFAULT 1")
        db.execute("CREATE TABLE street_houses(id TEXT PRIMARY KEY,data BLOB)")
        # Reproduce the previously built index where the street name was a stop word.
        db.execute("UPDATE streets SET words='уфа' WHERE street_key='российская'")
        db.execute("UPDATE search SET words='уфа' WHERE words=?", (address_words("Уфа Российская"),))
        before = db.execute("SELECT * FROM codes ORDER BY building_id").fetchall()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert rebuild(source, target)["changed_streets"] == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT * FROM codes ORDER BY building_id").fetchall() == before
    assert Catalog(str(target)).resolve("Уфа, Российская 153")["building_id"] == "russian"
    assert Catalog(str(target)).resolve("Уфа, Рязанская 153")["building_id"] == "other"
    assert Catalog(str(target)).resolve("Уфа, Российская 153к1")["status"] == "not_found"
    with pytest.raises(FileExistsError):
        rebuild(source, target)
